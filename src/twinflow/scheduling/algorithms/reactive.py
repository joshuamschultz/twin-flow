"""Reactive repair: rush insertion with relaxation, and reschedule from state (COMP-020,
REQ-112..REQ-114, D-081; Pinedo Sec 18.1 p.482, Sec 16.5 p.448-449, p.549-550).

Registers `rush_insert` and `reschedule` (family `reactive`, every `Feature`: every plan
goes through the decoder).

**rush_insert** (SDD "Rush insertion and relaxation"). Needs `context.previous_plan`, else
UNKNOWN `previous_plan_required`. The base: in-progress ops open their status machine; every
other op of the previous plan that is still scheduled keeps its previous machine, in
previous start order; an op whose previous machine is no longer eligible is merged at the
position its previous start gives (`_previous_layout`). A previous order that contradicts
the job links gives UNKNOWN `sequence_cycle`. Rush jobs are the jobs with an op absent from
the previous plan (problem order); their absent ops are the rush ops. For each rush job,
P = the jobs with a due date and a weight strictly above the rush job's; T_k(base) comes
from the decode of the plan before the job is inserted. Each rush op, in route order, tries
every eligible machine and position in (added setup, estimated completion, machine id,
position) order, where added = s(prev, o) + s(o, next) - s(prev, next) (no next:
s(prev, o); prev None = the machine state). The first candidate whose decode keeps
T_k <= T_k(base) + 1e-6 for every k in P under the current due dates and, for the last op,
C_rush <= D_rush is taken.

Speed (600x8): candidates are screened on the disjunctive graph of the partial plan, never
decoded one by one. The estimate starts o after its machine predecessor and its placed job
predecessors (graph heads), and bounds each protected job's new completion through the
follower: `end(o) - det'(next) + q_k(next) + delta s(next)`, q_k the graph's tail to the
job's sink (`sink_tails`). A candidate whose bound already breaks a protected job, or whose
estimated completion already makes the last op late, is skipped, and so is one that closes a
cycle on the graph (a path from an op after it to an op before it); the rest are decoded in
order until one is accepted. Measured on 600x8 with one 8-op rush job: 8 accepted decodes
plus the base and final ones, 0.8 s in the solver (68 cyclic candidates refused on the
graph; decoding them took 2.2 s). Later rush ops sit at the ends of machines while a decode
checks an earlier one: nothing follows them there, so they delay no base op.

Relaxation (D-081): when no candidate is accepted, pos* minimizes the estimated sum over
P and the rush job of w_k x increase in T_k (rush: its completion estimated as end(o) plus
its later shortest runs; tie: added setup), decoded; among the jobs violated at pos*, v =
min by (w_k, -(D_k - now), job id) is relaxed: `Relaxation(v, D_v, C_v(pos*), ...)`, D_v
becomes C_v(pos*), v leaves the constrained set (P, or the rush due check) and the op tries
again. Each relaxation shrinks that set, so there are at most |P| + 1. The reported
`new_due` is never below the job's completion in the returned plan [mine]: a job that left
the set may finish later in a later round, and the relaxation must cover the plan. Each
relaxation adds one `relaxed_due` issue naming the job, and each rush job one informational
`rush_added_setup` issue with its total added setup ("added setup <x> s"; the SDD names no
field for it).

**reschedule** warm-starts from the previous sequences, adds churn to the objective
(`objectives.effective_spec`, shared with the portfolio runner; a spec that already has a
churn term or level is kept as is) and runs `params.improver` (default `tabu`, by registry
name; unregistered gives UNKNOWN `subsolver_unavailable`) with that warm start and empty
params.
Without a previous plan: UNKNOWN `previous_plan_required`. When the improver finds no plan,
the warm start is returned.

Fallback chain [mine]: when the warm start's dispatch finds no plan, the warm start is
`rush_insert`'s construction instead (the previous sequences with the absent ops inserted;
its relaxations are not reported, reschedule keeps due dates), so reschedule answers
whenever one of its constructions decodes. Measured on the 208 reactive sweep variants
(seed % 5 == 1, frozen pins): UNKNOWN 61 before the kernel's pinned-window guard, 14 after
it, 4 with this fallback (the baseline fails on 14).

Warm start [mine; the SDD's "new ops by cheapest insertion" gave 2 to 3 times the scratch
weighted tardiness on the machine-down bench, and tabu did not recover it]: the
`unaffected_prefix` (the start-ordered part of the previous plan that no displaced or new
op reaches) is placed again, so its ops keep their starts, and the dispatch engine with
the `atcs` rule (`registry.rule`) schedules every other op, the new and the displaced ones
included. The reference is the same dispatch with no prefix (a fresh ATCS plan). Cuts of
the prefix at k/8 of its length (longest first, within half the remaining budget) replace
the incumbent when they move fewer ops and score at most `MAX_DEGRADATION` (Scorer delta
against the reference) worse than the reference.

Params (`SolveContext.params`): `churn_weight` (number >= 0) and `improver`; an unknown or
invalid param gives UNKNOWN `invalid_method_param` (DEV-15). The churn weight default is
the factory argument `churn_weight` (1.0): the seam hides the profile from solvers, so the
portfolio passes `profile.churn_weight` as `registry.get("reschedule", churn_weight=...)`;
the method param wins.

A leaf of `algorithms/` (D-069): it registers through `SOLVERS` and composes by name.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from twinflow.scheduling import registry
from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.dispatch import Dispatched, DispatchEngine, dispatched_schedule
from twinflow.scheduling.kernel.graph import DisjunctiveGraph, GraphCycle
from twinflow.scheduling.kernel.index import OpData, OpOption, ProblemIndex
from twinflow.scheduling.kernel.score import Scorer, fast_values
from twinflow.scheduling.model import (
    ObjectiveKind,
    ObjectiveSpec,
    PreviousAssignment,
    PreviousPlan,
    Problem,
    Relaxation,
    Schedule,
    ScheduleIssue,
)
from twinflow.scheduling.objectives import effective_spec
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature, ParamValue, SolveContext

RUSH_INSERT = "rush_insert"
RESCHEDULE = "reschedule"
DEFAULT_IMPROVER = "tabu"
DEFAULT_CHURN_WEIGHT = 1.0
TOLERANCE = 1e-6
"""Slack on 'no protected job gains tardiness' and 'the rush is on time' (verifier's 1e-6)."""
RELAX_REASON = "least valuable affected due date"
WARM_RULE = "atcs"
WARM_CUTS = 8
"""Prefix cuts the reschedule warm start tries [mine; 16 cuts did no better on training]."""
WARM_SHARE = 0.5
"""Share of the remaining budget the prefix cuts may use; the improver gets the rest."""
MAX_DEGRADATION = 0.05
"""How much worse than the fresh dispatch (Scorer delta) a kept prefix may make the plan
[mine; on the training machine-down scenarios 5 percent kept the plan within 10 percent of
atcs->tabu from scratch on 20 of 20 seeds and moved fewer ops on 19 of 20]."""


class InvalidParamError(ValueError):
    """A method param with an unknown name or an invalid value; the message names it."""


def _issue(code: str, path: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path=path, message=message)


# --- plan layout -----------------------------------------------------------------------------


@dataclass(slots=True)
class _Layout:
    """A partial or full plan: op -> machine and machine -> op ids in order."""

    choice: dict[str, str] = field(default_factory=dict)
    sequences: dict[str, list[str]] = field(default_factory=dict)

    def copy(self) -> _Layout:
        return _Layout(dict(self.choice), {m: list(s) for m, s in self.sequences.items()})

    def inserted(self, op_id: str, machine_id: str, position: int) -> _Layout:
        """A copy with `op_id` at `position` of `machine_id`."""
        copy = self.copy()
        copy.place(op_id, machine_id, position)
        return copy

    def place(self, op_id: str, machine_id: str, position: int) -> None:
        self.choice[op_id] = machine_id
        self.sequences.setdefault(machine_id, []).insert(position, op_id)

    def with_trailing(self, index: ProblemIndex, op_ids: Iterable[str]) -> _Layout:
        """A copy with each op appended to its first eligible machine (nothing follows it)."""
        copy = self.copy()
        for op_id in op_ids:
            machine_id = index.op(op_id).options[0].machine_id
            copy.place(op_id, machine_id, len(copy.sequences.get(machine_id, [])))
        return copy

    def dispatched(self, decoded: Decoded) -> Dispatched:
        sequences = {m: tuple(s) for m, s in self.sequences.items()}
        return Dispatched(choice=dict(self.choice), sequences=sequences, decoded=decoded)


def _decode_layout(index: ProblemIndex, layout: _Layout) -> Dispatched | ScheduleIssue:
    decoded = _decode(index, layout)
    return decoded if isinstance(decoded, ScheduleIssue) else layout.dispatched(decoded)


def _eligible(op: OpData, machine_id: str) -> bool:
    return any(option.machine_id == machine_id for option in op.options)


def _previous_assignments(index: ProblemIndex, previous: PreviousPlan) -> list[PreviousAssignment]:
    """Previous assignments of ops scheduled now, in (start, op id) order, one per op."""
    first: dict[str, PreviousAssignment] = {}
    for a in sorted(previous.assignments, key=lambda a: (a.start, a.operation_id)):
        if a.operation_id in index.pos:
            first.setdefault(a.operation_id, a)
    return list(first.values())


def _leading_running(index: ProblemIndex, sequence: Sequence[str]) -> int:
    """How many in-progress ops open the sequence (nothing may go before them)."""
    count = 0
    while count < len(sequence) and index.op(sequence[count]).in_progress is not None:
        count += 1
    return count


def _previous_layout(index: ProblemIndex, previous: PreviousPlan) -> tuple[_Layout, list[str]]:
    """The previous plan as a layout over the scheduled ops, and the ops absent from it.

    In-progress ops open their status machine. Other ops whose previous machine is still
    eligible keep it, in previous start order. The rest (their machine is gone or no longer
    eligible) are then merged in previous start order: on each eligible machine at the
    position their previous start gives, on the machine with the earliest estimated
    completion (ties: added setup, machine id). `GraphCycle` when the kept order already
    contradicts the job links.
    """
    layout = _Layout()
    for op in index.ops:
        if op.in_progress is not None:
            machine_id = op.options[0].machine_id
            layout.place(op.id, machine_id, len(layout.sequences.get(machine_id, [])))
    present = _previous_assignments(index, previous)
    displaced: list[PreviousAssignment] = []
    for a in present:
        op = index.op(a.operation_id)
        if op.in_progress is not None:
            continue
        if not _eligible(op, a.machine_id):
            displaced.append(a)
            continue
        layout.place(a.operation_id, a.machine_id, len(layout.sequences.get(a.machine_id, [])))
    starts = {a.operation_id: (a.start, a.operation_id) for a in present}
    for a in displaced:
        _merge(index, layout, index.op(a.operation_id), starts)
    absent = [op_id for op_id in index.op_ids if op_id not in layout.choice]
    return layout, absent


def _merge(
    index: ProblemIndex, layout: _Layout, op: OpData, starts: Mapping[str, tuple[float, str]]
) -> None:
    """Place a displaced op at its previous-start position on its best eligible machine."""
    timing = _Timing(index, layout)
    slots = []
    for option in op.options:
        sequence = layout.sequences.get(option.machine_id, [])
        first = _leading_running(index, sequence)
        k = bisect.bisect_right(sequence, starts[op.id], lo=first, key=starts.__getitem__)
        slots.append(_slot(index, timing, op, option, sequence, k))
    best = min(slots, key=lambda slot: (slot.est_end, slot.added, slot.machine_id))
    layout.place(op.id, best.machine_id, best.position)


def _decode(index: ProblemIndex, layout: _Layout) -> Decoded | ScheduleIssue:
    return decode(index, layout.choice, layout.sequences)


def _completion(decoded: Decoded, index: ProblemIndex, job_pos: int) -> float:
    return max(decoded.ends[p] for p in index.jobs[job_pos].ops)


# --- graph timing and candidate slots ---------------------------------------------------------


class _Timing:
    """Heads, setups and ends of a partial plan's graph (placed ops only)."""

    __slots__ = ("_heads", "_index", "_layout", "_prev", "graph")

    def __init__(self, index: ProblemIndex, layout: _Layout) -> None:
        self._index = index
        self._layout = layout
        self.graph = DisjunctiveGraph.build(index, layout.choice, layout.sequences)
        self._heads = self.graph.heads()
        self._prev = {
            op_id: sequence[k - 1] if k else None
            for sequence in layout.sequences.values()
            for k, op_id in enumerate(sequence)
        }

    def placed(self, op_id: str) -> bool:
        return op_id in self._heads

    def head(self, op_id: str) -> float:
        return self._heads[op_id]

    def setup_end(self, op_id: str) -> float:
        return self._heads[op_id] + self._setup(op_id)

    def end(self, op_id: str) -> float:
        op = self._index.op(op_id)
        if op.in_progress is not None:
            return self._heads[op_id] + (op.in_progress.remaining_seconds or 0.0)
        return self.setup_end(op_id) + _option(op, self._layout.choice[op_id]).run

    def _setup(self, op_id: str) -> float:
        if self._index.op(op_id).in_progress is not None:
            return 0.0
        machine_id = self._layout.choice[op_id]
        return self._index.setups.seconds(machine_id, self._prev[op_id], op_id)

    def job_ready(self, op: OpData) -> float:
        """Latest start bound from the placed job predecessors (graph job-arc weights)."""
        ready = -math.inf
        for link in op.predecessors:
            if not self.placed(link.pred_id):
                continue
            if link.overlap is None:
                ready = max(ready, self.end(link.pred_id) + link.min_lag)
                continue
            u = _option(self._index.ops[link.pred_pos], self._layout.choice[link.pred_id])
            transfer = u.fixed_seconds + link.overlap * u.unit_seconds + link.min_lag
            ready = max(ready, self.setup_end(link.pred_id) + transfer)
        return ready


def _option(op: OpData, machine_id: str) -> OpOption:
    return next(option for option in op.options if option.machine_id == machine_id)


@dataclass(frozen=True, slots=True)
class _Slot:
    """One insertion candidate with its graph estimate (see the module docstring)."""

    added: float
    est_end: float
    machine_id: str
    position: int
    prev_id: str | None
    next_id: str | None
    next_floor: float
    next_delta: float

    @property
    def key(self) -> tuple[float, float, str, int]:
        return (self.added, self.est_end, self.machine_id, self.position)


def _slots(index: ProblemIndex, timing: _Timing, layout: _Layout, op: OpData) -> list[_Slot]:
    """Every eligible machine and position for `op`, in key order (never before a running op)."""
    found: list[_Slot] = []
    for option in op.options:
        sequence = layout.sequences.get(option.machine_id, [])
        first = _leading_running(index, sequence)
        found.extend(
            _slot(index, timing, op, option, sequence, k) for k in range(first, len(sequence) + 1)
        )
    return sorted(found, key=lambda slot: slot.key)


def _slot(
    index: ProblemIndex,
    timing: _Timing,
    op: OpData,
    option: OpOption,
    sequence: Sequence[str],
    k: int,
) -> _Slot:
    seconds = index.setups.seconds
    machine_id = option.machine_id
    prev = sequence[k - 1] if k else None
    nxt = sequence[k] if k < len(sequence) else None
    setup = seconds(machine_id, prev, op.id)
    start = _earliest_start(index, timing, op, machine_id, setup, prev)
    end = start + setup + option.run
    delta = 0.0
    next_floor = end
    if nxt is not None:
        into_next = seconds(machine_id, op.id, nxt)
        delta = into_next - seconds(machine_id, prev, nxt)
        next_floor = end - (into_next if index.op(nxt).setup_detached else 0.0)
    return _Slot(setup + delta, end, machine_id, k, prev, nxt, next_floor, delta)


def _earliest_start(
    index: ProblemIndex,
    timing: _Timing,
    op: OpData,
    machine_id: str,
    setup: float,
    prev: str | None,
) -> float:
    """The graph head `op` gets at this slot: h0, placed job predecessors, machine predecessor."""
    machine = index.machines[index.machine_pos[machine_id]]
    det = setup if op.setup_detached else 0.0
    floor = max(index.now, machine.available_from)
    if op.pin is not None and op.pin.earliest_start is not None:
        floor = max(floor, op.pin.earliest_start)
    start = max(floor, op.release - det, timing.job_ready(op) - det)
    return start if prev is None else max(start, timing.end(prev))


def _closes_cycle(index: ProblemIndex, timing: _Timing, op: OpData, slot: _Slot) -> bool:
    """True when a path leads from an op after `op` at this slot to an op before it."""
    before = [link.pred_id for link in op.predecessors if timing.placed(link.pred_id)]
    after = [index.op_ids[s] for s in index.successors[index.pos[op.id]]]
    after = [s for s in after if timing.placed(s)]
    if slot.prev_id is not None:
        before.append(slot.prev_id)
    if slot.next_id is not None:
        after.append(slot.next_id)
    return any(_path(timing, x, y) for x in after for y in before)


def _path(timing: _Timing, x: str, y: str) -> bool:
    # heads never decrease along an arc, so a path x -> y needs head(y) >= head(x)
    return x == y or (timing.head(y) >= timing.head(x) and timing.graph.has_path(x, y))


# --- rush insertion ----------------------------------------------------------------------------


@dataclass(slots=True)
class _Guard:
    """The constrained jobs of one rush job: protected positions and the rush due check."""

    rush: int
    protected: set[int]
    rush_checked: bool
    base: dict[int, float]


@dataclass(frozen=True, slots=True)
class _Tails:
    """Protected sink tails per follower op: rows by op id, columns by protected job."""

    rows: Mapping[str, int]
    columns: tuple[int, ...]
    q: npt.NDArray[np.float64]


class _RushInsertion:
    """Mutable state of one rush insertion: plan, due dates, relaxations and issues."""

    def __init__(self, index: ProblemIndex, layout: _Layout, rush_ops: list[str]) -> None:
        self.index = index
        self.layout = layout
        self.unplaced = list(rush_ops)
        self.dues: dict[int, float | None] = {k: job.due for k, job in enumerate(index.jobs)}
        self.relaxations: list[tuple[int, float, float]] = []
        self.issues: list[ScheduleIssue] = []
        self.timing: _Timing | None = None

    def insert_job(self, job_pos: int) -> ScheduleIssue | None:
        """Insert every rush op of the job in route order (module docstring)."""
        index = self.index
        base = _decode(index, self.layout.with_trailing(index, self.unplaced))
        if isinstance(base, ScheduleIssue):
            return base
        weight = index.jobs[job_pos].weight
        protected = {
            k
            for k, job in enumerate(index.jobs)
            if k != job_pos and job.weight > weight and self.dues[k] is not None
        }
        completions = {k: _completion(base, index, k) for k in protected}
        guard = _Guard(job_pos, protected, self.dues[job_pos] is not None, completions)
        ops = [index.op_ids[p] for p in index.jobs[job_pos].ops if index.op_ids[p] in self.unplaced]
        added = 0.0
        for n, op_id in enumerate(ops):
            self.unplaced.remove(op_id)
            slot = self._insert_op(index.op(op_id), guard, last=n == len(ops) - 1)
            if isinstance(slot, ScheduleIssue):
                return slot
            added += slot.added
        job_id = index.jobs[job_pos].id
        message = f"rush job {job_id}: added setup {added:g} s"
        self.issues.append(_issue("rush_added_setup", f"jobs.{job_id}", message))
        return None

    def _insert_op(self, op: OpData, guard: _Guard, *, last: bool) -> _Slot | ScheduleIssue:
        timing = self.timing = _Timing(self.index, self.layout)
        slots = _slots(self.index, timing, self.layout, op)
        while True:
            tails = self._tails(timing, slots, guard)
            accepted: _Slot | ScheduleIssue | None
            accepted = self._first_acceptable(op, slots, guard, tails, last=last)
            if accepted is None:
                accepted = self._relax(op, slots, guard, tails, last=last)
            if accepted is not None:
                if not isinstance(accepted, ScheduleIssue):
                    self.layout.place(op.id, accepted.machine_id, accepted.position)
                return accepted

    # --- screening ---------------------------------------------------------------------------

    def _tails(self, timing: _Timing, slots: Sequence[_Slot], guard: _Guard) -> _Tails:
        columns = tuple(sorted(guard.protected))
        followers = sorted({s.next_id for s in slots if s.next_id is not None})
        if not columns or not followers:
            return _Tails({}, columns, np.empty((0, len(columns))))
        q = timing.graph.sink_tails(followers)[:, list(columns)]
        return _Tails({op_id: r for r, op_id in enumerate(followers)}, columns, q)

    def _tardiness_increase(
        self, slot: _Slot, guard: _Guard, tails: _Tails
    ) -> npt.NDArray[np.float64]:
        """Lower-bound tardiness increase of each protected job at `slot` (graph estimate)."""
        if slot.next_id is None or not tails.columns:
            return np.zeros(len(tails.columns))
        bound = slot.next_floor + tails.q[tails.rows[slot.next_id]] + slot.next_delta
        dues = np.array([self._due(k) for k in tails.columns])
        base = np.array([max(0.0, guard.base[k] - self._due(k)) for k in tails.columns])
        increase: npt.NDArray[np.float64] = np.maximum(0.0, np.maximum(0.0, bound - dues) - base)
        return increase

    def _due(self, job_pos: int) -> float:
        due = self.dues[job_pos]
        return math.inf if due is None else due

    def _screened_out(self, slot: _Slot, guard: _Guard, tails: _Tails, *, last: bool) -> bool:
        if last and guard.rush_checked and slot.est_end > self._due(guard.rush) + TOLERANCE:
            return True
        increase = self._tardiness_increase(slot, guard, tails)
        return bool(np.any(increase > TOLERANCE))

    def _first_acceptable(
        self, op: OpData, slots: Sequence[_Slot], guard: _Guard, tails: _Tails, *, last: bool
    ) -> _Slot | None:
        for slot in slots:
            if self._screened_out(slot, guard, tails, last=last):
                continue
            decoded = self._decode_at(op, slot)
            if isinstance(decoded, Decoded) and not self._violated(decoded, guard, last=last):
                return slot
        return None

    def _decode_at(self, op: OpData, slot: _Slot) -> Decoded | ScheduleIssue:
        """The decode with `op` at `slot`; a cycle found on the graph skips the decode."""
        if self.timing is not None and _closes_cycle(self.index, self.timing, op, slot):
            return _issue("sequence_cycle", op.id, f"{op.id}: this position closes a cycle")
        layout = self.layout.inserted(op.id, slot.machine_id, slot.position)
        try:
            return _decode(self.index, layout.with_trailing(self.index, self.unplaced))
        except ValueError as error:  # a slot the decoder refuses as malformed
            return _issue("no_feasible_slot", op.id, str(error))

    def _violated(self, decoded: Decoded, guard: _Guard, *, last: bool) -> list[int]:
        """Constrained jobs whose exact tardiness breaks the guard at this decode."""
        index = self.index
        violated = [
            k
            for k in sorted(guard.protected)
            if max(0.0, _completion(decoded, index, k) - self._due(k))
            > max(0.0, guard.base[k] - self._due(k)) + TOLERANCE
        ]
        rush_late = _completion(decoded, index, guard.rush) > self._due(guard.rush) + TOLERANCE
        if last and guard.rush_checked and rush_late:
            violated.append(guard.rush)
        return violated

    # --- relaxation ----------------------------------------------------------------------------

    def _relax(
        self, op: OpData, slots: Sequence[_Slot], guard: _Guard, tails: _Tails, *, last: bool
    ) -> _Slot | ScheduleIssue | None:
        """Decode pos* and relax its least valuable violated job; None to try the op again."""
        cost = functools.partial(self._cost, op=op, guard=guard, tails=tails)
        failure = _issue("no_feasible_slot", op.id, f"{op.id}: no insertion decodes")
        for slot in sorted(slots, key=lambda s: (cost(s), s.added, s.key)):
            decoded = self._decode_at(op, slot)
            if isinstance(decoded, ScheduleIssue):
                failure = decoded
                continue
            violated = self._violated(decoded, guard, last=last)
            if not violated:
                return slot
            self._relax_job(self._least_valuable(violated), decoded, guard)
            return None
        return failure

    def _cost(self, slot: _Slot, *, op: OpData, guard: _Guard, tails: _Tails) -> float:
        """Estimated sum over P and the rush job of w_k x tardiness increase."""
        weights = np.array([self.index.jobs[k].weight for k in tails.columns])
        protected = float(np.dot(weights, self._tardiness_increase(slot, guard, tails)))
        rush_end = slot.est_end + op.remaining_shortest_run
        rush = self.index.jobs[guard.rush].weight * max(0.0, rush_end - self._due(guard.rush))
        return protected + rush

    def _least_valuable(self, violated: Sequence[int]) -> int:
        jobs, now = self.index.jobs, self.index.now
        return min(violated, key=lambda k: (jobs[k].weight, -(self._due(k) - now), jobs[k].id))

    def _relax_job(self, job_pos: int, decoded: Decoded, guard: _Guard) -> None:
        old = self._due(job_pos)
        new = _completion(decoded, self.index, job_pos)
        self.relaxations.append((job_pos, old, new))
        self.dues[job_pos] = new
        if job_pos == guard.rush:
            guard.rush_checked = False
        else:
            guard.protected.discard(job_pos)

    # --- result --------------------------------------------------------------------------------

    def relaxation_records(
        self, final: Decoded
    ) -> tuple[tuple[Relaxation, ...], list[ScheduleIssue]]:
        """Relaxations (new due never below the final completion) and their issues."""
        records: list[Relaxation] = []
        issues: list[ScheduleIssue] = []
        for job_pos, old, new in self.relaxations:
            job_id = self.index.jobs[job_pos].id
            due = max(new, _completion(final, self.index, job_pos))
            records.append(Relaxation(job_id=job_id, old_due=old, new_due=due, reason=RELAX_REASON))
            message = f"{job_id}: due {old:g} relaxed to {due:g} to fit a rush job"
            issues.append(_issue("relaxed_due", f"jobs.{job_id}.due", message))
        return tuple(records), issues


def _rush_jobs(index: ProblemIndex, rush_ops: Sequence[str]) -> list[int]:
    """Positions of the jobs with an op absent from the previous plan, in problem order."""
    return sorted({index.op(op_id).job_pos for op_id in rush_ops})


def rush_insert_plan(
    index: ProblemIndex, previous: PreviousPlan
) -> tuple[Dispatched, tuple[Relaxation, ...], list[ScheduleIssue]] | ScheduleIssue:
    """Rush insertion with relaxation (module docstring): plan, relaxations and issues."""
    try:
        layout, rush_ops = _previous_layout(index, previous)
    except GraphCycle as error:
        return _issue("sequence_cycle", "previous_plan", str(error))
    rush_jobs = _rush_jobs(index, rush_ops)
    insertion = _RushInsertion(index, layout, rush_ops)
    for job_pos in rush_jobs:
        issue = insertion.insert_job(job_pos)
        if issue is not None:
            return issue
    plan = _decode_layout(index, insertion.layout)
    if isinstance(plan, ScheduleIssue):
        return plan
    relaxations, relax_issues = insertion.relaxation_records(plan.decoded)
    return plan, relaxations, [*insertion.issues, *relax_issues]


class RushInsertSolver:
    """`rush_insert` behind the `Solver` seam (COMP-020)."""

    __slots__ = ()

    @property
    def name(self) -> str:
        return RUSH_INSERT

    @property
    def capabilities(self) -> frozenset[Feature]:
        return frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        """The repaired plan with its relaxations; UNKNOWN with a named issue otherwise."""
        started = time.perf_counter()
        index = ProblemIndex.build(problem)
        if context.previous_plan is None:
            issue = _issue("previous_plan_required", "previous_plan", "rush_insert needs one")
            return dispatched_schedule(RUSH_INSERT, issue, index, objective, context, started)
        result = rush_insert_plan(index, context.previous_plan)
        if isinstance(result, ScheduleIssue):
            return dispatched_schedule(RUSH_INSERT, result, index, objective, context, started)
        plan, relaxations, issues = result
        schedule = dispatched_schedule(RUSH_INSERT, plan, index, objective, context, started)
        return schedule.model_copy(update={"issues": tuple(issues), "relaxations": relaxations})


# --- reschedule from state ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RescheduleSettings:
    churn_weight: float
    improver: str

    @classmethod
    def from_params(
        cls, params: Mapping[str, ParamValue], default_churn: float
    ) -> _RescheduleSettings:
        unknown = sorted(set(params) - {"churn_weight", "improver"})
        if unknown:
            raise InvalidParamError(f"unknown param {unknown[0]}")
        weight = params.get("churn_weight", default_churn)
        if isinstance(weight, bool) or not isinstance(weight, int | float):
            raise InvalidParamError("churn_weight must be a number >= 0")
        if not (math.isfinite(weight) and weight >= 0):
            raise InvalidParamError("churn_weight must be a number >= 0")
        improver = params.get("improver", DEFAULT_IMPROVER)
        if not isinstance(improver, str) or not improver:
            raise InvalidParamError("improver must be a registered method name")
        return cls(churn_weight=float(weight), improver=improver)


def unaffected_prefix(index: ProblemIndex, previous: PreviousPlan) -> list[PreviousAssignment]:
    """The longest start-ordered part of the previous plan that nothing changed reaches.

    One pass in previous start order keeps an op when its previous machine is still
    eligible, no earlier op on that machine was dropped and every scheduled predecessor is
    kept; a dropped op blocks the rest of its machine. So the result is closed under job
    and machine ancestors, and each kept op starts where it did when it is placed again.
    Ops absent from the previous plan are never kept, which drops their descendants too.
    """
    kept: set[str] = set()
    blocked: set[str] = set()
    prefix: list[PreviousAssignment] = []
    for a in _previous_assignments(index, previous):
        op = index.op(a.operation_id)
        keeps = (
            a.machine_id not in blocked
            and _eligible(op, a.machine_id)
            and all(link.pred_id in kept for link in op.predecessors)
        )
        if not keeps:
            blocked.add(a.machine_id)
            continue
        kept.add(a.operation_id)
        prefix.append(a)
    return prefix


def _dispatch_after(
    index: ProblemIndex, prefix: Sequence[PreviousAssignment], deadline: float | None
) -> Dispatched | ScheduleIssue:
    """`prefix` placed again in order, the rest dispatched with the `WARM_RULE` rule."""
    try:
        rule = registry.rule(WARM_RULE)
    except registry.UnknownRuleError as error:
        return _issue("subsolver_unavailable", "problem", str(error))
    engine = DispatchEngine(index, rule)
    state = engine.initial_state()
    for a in prefix:
        p = index.pos[a.operation_id]
        if index.ops[p].in_progress is not None:  # placed at `now` by the initial state
            continue
        issue = state.place(p, index.machine_pos[a.machine_id])
        if issue is not None:
            return issue
    return engine.complete(state, deadline)


def warm_start_plan(
    index: ProblemIndex,
    previous: PreviousPlan,
    objective: ObjectiveSpec,
    *,
    churn_tolerance: float = 0.0,
    deadline: float | None = None,
) -> Dispatched | ScheduleIssue:
    """The warm start of `reschedule` (module docstring).

    The reference is the dispatch with no prefix; each longer cut of `unaffected_prefix`
    (longest first, until `deadline`) replaces the incumbent when its churn is lower and
    `Scorer(objective, reference).delta(cut, reference) <= MAX_DEGRADATION`.
    """
    prefix = unaffected_prefix(index, previous)
    reference = _dispatch_after(index, (), None)
    if isinstance(reference, ScheduleIssue):
        return reference
    values = functools.partial(
        fast_values, index, previous_plan=previous, churn_tolerance=churn_tolerance
    )
    reference_values = values(reference.decoded)
    scorer = Scorer(objective, reference_values)
    best, best_churn = reference, reference_values[ObjectiveKind.CHURN]
    cuts = sorted({round(len(prefix) * c / WARM_CUTS) for c in range(1, WARM_CUTS + 1)})
    for k in reversed(cuts):
        if deadline is not None and time.monotonic() > deadline:
            break
        candidate = _dispatch_after(index, prefix[:k], deadline)
        if isinstance(candidate, ScheduleIssue):
            continue
        candidate_values = values(candidate.decoded)
        churn = candidate_values[ObjectiveKind.CHURN]
        within = scorer.delta(candidate_values, reference_values) <= MAX_DEGRADATION
        if within and churn < best_churn:
            best, best_churn = candidate, churn
    return best


def _previous_sequences_plan(
    index: ProblemIndex, previous: PreviousPlan, issue: ScheduleIssue
) -> Dispatched | ScheduleIssue:
    """`rush_insert`'s plan (previous sequences, absent ops inserted); else `issue`."""
    result = rush_insert_plan(index, previous)
    return issue if isinstance(result, ScheduleIssue) else result[0]


class RescheduleSolver:
    """`reschedule` behind the `Solver` seam (COMP-020); `churn_weight` is the default."""

    __slots__ = ("_churn_weight",)

    def __init__(self, churn_weight: float = DEFAULT_CHURN_WEIGHT) -> None:
        self._churn_weight = churn_weight

    @property
    def name(self) -> str:
        return RESCHEDULE

    @property
    def capabilities(self) -> frozenset[Feature]:
        return frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        """The improver's plan from the warm start; UNKNOWN with a named issue otherwise."""
        started = time.perf_counter()
        index = ProblemIndex.build(problem)
        checked = self._checked(context)
        if isinstance(checked, ScheduleIssue):
            return dispatched_schedule(RESCHEDULE, checked, index, objective, context, started)
        settings, previous = checked
        warm_deadline = time.monotonic() + WARM_SHARE * max(
            0.0, context.deadline - time.monotonic()
        )
        warm = warm_start_plan(
            index,
            previous,
            objective,
            churn_tolerance=context.churn_tolerance,
            deadline=warm_deadline,
        )
        if isinstance(warm, ScheduleIssue):
            warm = _previous_sequences_plan(index, previous, warm)
        warm_schedule = (
            None if isinstance(warm, ScheduleIssue) else warm.decoded.schedule(RESCHEDULE)
        )
        spec = effective_spec(objective, settings.churn_weight, len(previous.assignments))
        improved = registry.get(settings.improver).solve(
            problem,
            objective=spec,
            budget_seconds=budget_seconds,
            context=dataclasses.replace(context, warm_start=warm_schedule, params={}),
        )
        if improved.status in ("FEASIBLE", "OPTIMAL") or isinstance(warm, ScheduleIssue):
            elapsed = time.perf_counter() - started
            return improved.model_copy(update={"solver": RESCHEDULE, "elapsed_seconds": elapsed})
        return dispatched_schedule(RESCHEDULE, warm, index, objective, context, started)

    def _checked(
        self, context: SolveContext
    ) -> tuple[_RescheduleSettings, PreviousPlan] | ScheduleIssue:
        """Settings and previous plan, or the issue that refuses the solve."""
        try:
            settings = _RescheduleSettings.from_params(context.params, self._churn_weight)
        except InvalidParamError as error:
            path = f"profile.method_params.{RESCHEDULE}"
            return _issue("invalid_method_param", path, str(error))
        if not registry.is_registered(settings.improver):
            message = f"improver {settings.improver!r} is not registered"
            return _issue("subsolver_unavailable", "profile.method_params.reschedule", message)
        if context.previous_plan is None:
            return _issue("previous_plan_required", "previous_plan", "reschedule needs one")
        return settings, context.previous_plan


SOLVERS = (
    SolverSpec(
        name=RUSH_INSERT,
        family="reactive",
        factory=RushInsertSolver,
        deterministic=True,
        capabilities=frozenset(Feature),
    ),
    SolverSpec(
        name=RESCHEDULE,
        family="reactive",
        factory=RescheduleSolver,
        deterministic=True,
        capabilities=frozenset(Feature),
    ),
)
"""Registry entries (D-069)."""
