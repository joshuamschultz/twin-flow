"""Dispatch engine and machine policy (COMP-007, REQ-090, REQ-091, D-071, D-072).

`DispatchEngine(index, rule, mode=..., policy=...)` builds a machine choice and one
sequence per machine by list scheduling, then hands both to `kernel.decode.decode`, which
builds the schedule (D-071): `Dispatched.decoded` is exactly that decode. The base of every
priority method (elementary rules, ATC/ATCS, Giffler-Thompson, beam search, rollouts).

The engine places each op through the same `MachineTimeline` / `PoolTimeline` / `SlotCheck`
calls and the same append-only earliest start as the decoder (never before its machine's
last placed op), so every rule of TF-002 holds while it decides and its placements are the
decoder's (operator pools aside, where the decoder's placement order may differ).

**Non-delay mode** (Pinedo p.372-375): the next decision is the machine with the smallest
`t_i = max(f_i, min est(o, i))` over the ready ops policy-accepted on i (ties: machine id),
where `f_i` is the end of i's last occupation; `C` = those accepted ops with
`est(o, i) <= t_i`; the op with the highest `rule.index` (ties: op id) is placed. This reads
the SDD pseudo-code's "machine with the smallest free time, else wait to the earliest est"
as one key, so a machine never commits ahead of another machine's earlier decision.

**Active mode** (Giffler-Thompson, Pinedo Alg 7.1.3, p.183-184): `t*` = min over ready
accepted (o, i) of the estimated completion `max(f_i, est(o, i)) + s_i(last_i, o) + p_i(o)`,
`i*` its machine (ties: machine id), `C` = the accepted ready ops on `i*` with estimated
start `< t*`; the rule picks from `C` at `t` = the smallest start in `C`. `gt_branch(state)`
returns `C` for beam search.

`est(o, i)` = max(material gate, now, pin earliest start, transfer-lot end floor - p_i(o)):
the earliest start the engine assumes, computed once when o becomes ready (all scheduled
predecessors placed).

**Machine policy `PreferredUnlessLate`** (D-072, Pinedo p.523; this module's reading): an op
with a due date is accepted on its lowest `preference_cost` options whose estimated finish
`max(f_k, est(o, k)) + p_k(o)` meets `local_due(o)`; when none does, on the option(s) with the
earliest estimated finish at any cost. An op without a due date is accepted on its
lowest-cost options only.

A machine policy implements `MachinePolicy` (`accepted(state, p)`, the machine positions the
ready op at position p may go to now); `PreferredUnlessLate` is the default.

A rule implements `PriorityRule`: `index(state, op_id, machine_id, t, last_op_id)` returns a
float or a tuple of floats (compared lexicographically; higher first), with an optional
`prepare(index)` the engine calls once at construction.

**Followers** (ATCS macro jobs, Pinedo p.93, p.448): a rule may also define
`followers(state, op_id, machine_id) -> Sequence[str]`. When machine i decides next, the
engine first asks for the followers of i's last placed op and places the first one that is
ready, eligible on i and has est no later than i's free time (its gate is met when the
last op finishes), ahead of the conflict set; followers that do not qualify stay in the
pool. A placed follower is i's new last op, so a chain continues decision by decision.
Followers are ordinary placements, so the sequences still decode to the engine's schedule.

Per decision the engine touches only the deciding machine's candidates: ready ops are kept
per machine incrementally, `t_i` and the completion bound are cached per machine and
recomputed only for machines a placement can affect.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from twinflow.scheduling.calendar_math import Intervals
from twinflow.scheduling.kernel.decode import (
    Decoded,
    decode,
    machine_timeline,
    unplaceable_issue,
)
from twinflow.scheduling.kernel.index import OpData, OpOption, ProblemIndex
from twinflow.scheduling.kernel.score import fast_values
from twinflow.scheduling.model import ObjectiveSpec, Problem, Schedule, ScheduleIssue
from twinflow.scheduling.objectives import primary_kind
from twinflow.scheduling.seam import Feature, SolveContext
from twinflow.scheduling.timeline import (
    Gate,
    MachineTimeline,
    Placed,
    PoolTimeline,
    Slot,
    SlotCheck,
    SlotRequest,
    effective_calendar,
    hold_operators,
    material_gate,
    operator_retry,
)

Mode = Literal["nondelay", "active"]
Priority = float | tuple[float, ...]
"""A rule's index: higher goes first; tuples compare level by level."""

_EPS = 1e-9
_NONE = -1
_INF = math.inf


class PriorityRule(Protocol):
    """A dispatch rule; it may also define `prepare(index)`, called once before `index`."""

    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority: ...


@dataclass(frozen=True, slots=True)
class Dispatched:
    """The engine's machine choice and sequences, and their decode (D-071)."""

    choice: Mapping[str, str]
    sequences: Mapping[str, tuple[str, ...]]
    decoded: Decoded


# --- static per-op data, built once per engine ---------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class _Static:
    """Plain per-position data the engine reads at every decision (built once)."""

    index: ProblemIndex
    machine_rank: tuple[int, ...]
    op_rank: tuple[int, ...]
    machines_of: tuple[tuple[int, ...], ...]
    lowest_of: tuple[tuple[int, ...], ...]
    earliest: tuple[float, ...]
    option_by_machine: tuple[Mapping[str, OpOption], ...]
    calendars: dict[tuple[str, tuple[str, ...]], Intervals]

    @classmethod
    def build(cls, index: ProblemIndex) -> _Static:
        machine_ids = [m.id for m in index.machines]
        machines_of = tuple(
            tuple(index.machine_pos[o.machine_id] for o in op.options) for op in index.ops
        )
        return cls(
            index=index,
            machine_rank=_ranks(machine_ids),
            op_rank=_ranks(list(index.op_ids)),
            machines_of=machines_of,
            lowest_of=tuple(_lowest(op, ms) for op, ms in zip(index.ops, machines_of, strict=True)),
            earliest=tuple(_earliest(op, index.now) for op in index.ops),
            option_by_machine=tuple({o.machine_id: o for o in op.options} for op in index.ops),
            calendars={},
        )

    def calendar(self, machine_id: str, op: OpData, pools: Mapping[str, PoolTimeline]) -> Intervals:
        """Effective calendar of `op` on the machine (memoized; values never change)."""
        key = (machine_id, tuple(need.pool_id for need in op.operator_needs))
        if key not in self.calendars:
            machine = self.index.machines[self.index.machine_pos[machine_id]]
            pool_calendars = (pools[pool_id].calendar for pool_id in key[1])
            self.calendars[key] = effective_calendar(machine.calendar, pool_calendars)
        return self.calendars[key]


def _ranks(ids: Sequence[str]) -> tuple[int, ...]:
    """Per position, the rank of its id in sorted order."""
    order = sorted(range(len(ids)), key=lambda p: ids[p])
    ranks = [0] * len(ids)
    for rank, p in enumerate(order):
        ranks[p] = rank
    return tuple(ranks)


def _lowest(op: OpData, machines: tuple[int, ...]) -> tuple[int, ...]:
    """Machine positions of the op's lowest-preference-cost options."""
    lowest = min(o.preference_cost for o in op.options)
    return tuple(
        m for m, o in zip(machines, op.options, strict=True) if o.preference_cost == lowest
    )


def _earliest(op: OpData, now: float) -> float:
    pin = op.pin
    if pin is not None and pin.earliest_start is not None:
        return max(now, pin.earliest_start)
    return now


# --- machine policy -------------------------------------------------------------------------


class MachinePolicy(Protocol):
    """Which machines a ready op may go to now (machine positions, a subset of its options)."""

    def accepted(self, state: DispatchState, p: int) -> tuple[int, ...]: ...


@dataclass(frozen=True, slots=True)
class PreferredUnlessLate:
    """Preferred machine unless late (D-072, Pinedo p.523); see the module docstring."""

    def accepted(self, state: DispatchState, p: int) -> tuple[int, ...]:
        """Machine positions the ready op at position `p` may go to now."""
        static = state.static
        machines = static.machines_of[p]
        local_due = static.index.ops[p].local_due
        if len(machines) == 1 or local_due is None:
            return static.lowest_of[p]
        finish = {m: state.estimated_start(p, m) + state.run_at(p, m) for m in machines}
        meets = tuple(m for m in static.lowest_of[p] if finish[m] <= local_due + _EPS)
        if meets:
            return meets
        best = min(finish.values())
        return tuple(m for m in machines if finish[m] == best)


# --- state -----------------------------------------------------------------------------------


class DispatchState:
    """A partial dispatch: what is placed, what is ready, per-machine timelines.

    `commit(op_id, machine_id)` places one ready op; `copy()` is independent (beam search,
    rollouts). Rules read it through `index`, `ready_time`, `option`, `queue_length`,
    `last_op` and `free_time`.
    """

    __slots__ = (
        "_cands",
        "_choice",
        "_completion",
        "_free",
        "_gates",
        "_last",
        "_last_start",
        "_placed",
        "_policy",
        "_pools",
        "_queue",
        "_remaining",
        "_sequences",
        "_start",
        "_timelines",
        "_waiting",
        "issue",
        "static",
    )

    static: _Static
    issue: ScheduleIssue | None
    _cands: list[dict[int, float]]
    _choice: list[int]
    _completion: list[tuple[float, int] | None]
    _free: list[float]
    _gates: list[Gate | None]
    _last: list[int]
    _last_start: list[float]
    _placed: list[Placed | None]
    _policy: MachinePolicy
    _pools: dict[str, PoolTimeline]
    _queue: dict[str, int]
    _remaining: int
    _sequences: list[list[int]]
    _start: list[float | None]
    _timelines: list[MachineTimeline]
    _waiting: list[int]

    @classmethod
    def initial(cls, static: _Static, policy: MachinePolicy) -> DispatchState:
        """In-progress ops placed at `now`; every op with no unplaced predecessor ready.

        A conflict among in-progress ops is kept in `issue` (the engine returns it).
        """
        index = static.index
        n, k = len(index.ops), len(index.machines)
        state = cls.__new__(cls)
        state.static = static
        state.issue = None
        state._policy = policy
        state._timelines = [machine_timeline(m, index) for m in index.machines]
        state._pools = {
            pid: PoolTimeline(pid, pool.size, pool.calendar) for pid, pool in index.pools.items()
        }
        state._cands = [{} for _ in range(k)]
        state._choice = [_NONE] * n
        state._gates = [None] * n
        state._free = [max(index.now, m.available_from) for m in index.machines]
        state._last = [_NONE] * k
        state._last_start = [-_INF] * k
        state._placed = [None] * n
        state._queue = dict.fromkeys(index.center_machines, 0)
        state._remaining = n
        state._sequences = [[] for _ in range(k)]
        state._start = [None] * k
        state._completion = [None] * k
        state._waiting = [len(op.predecessors) for op in index.ops]
        state.issue = state._start_in_progress()
        for p, count in enumerate(state._waiting):
            if count == 0 and state._placed[p] is None and state._gates[p] is None:
                state._make_ready(p)
        return state

    def copy(self) -> DispatchState:
        """An independent copy; static data and the setup memo are shared."""
        twin = DispatchState.__new__(DispatchState)
        twin.static = self.static
        twin.issue = self.issue
        twin._policy = self._policy
        twin._timelines = [t.copy() for t in self._timelines]
        twin._pools = {pid: pool.copy() for pid, pool in self._pools.items()}
        twin._cands = [dict(c) for c in self._cands]
        twin._choice = list(self._choice)
        twin._gates = list(self._gates)
        twin._free = list(self._free)
        twin._last = list(self._last)
        twin._last_start = list(self._last_start)
        twin._placed = list(self._placed)
        twin._queue = dict(self._queue)
        twin._remaining = self._remaining
        twin._sequences = [list(s) for s in self._sequences]
        twin._start = list(self._start)
        twin._completion = list(self._completion)
        twin._waiting = list(self._waiting)
        return twin

    # --- read API for rules ------------------------------------------------------------

    @property
    def index(self) -> ProblemIndex:
        return self.static.index

    @property
    def done(self) -> bool:
        """True when every op is placed."""
        return self._remaining == 0

    def ready_time(self, op_id: str) -> float:
        """The ready op's material gate (release, predecessors, lags)."""
        gate = self._gates[self.static.index.pos[op_id]]
        if gate is None:
            raise ValueError(f"{op_id} is not ready")
        return gate.ready

    def option(self, op_id: str, machine_id: str) -> OpOption:
        """The op's option on `machine_id` (KeyError when not eligible)."""
        return self.static.option_by_machine[self.static.index.pos[op_id]][machine_id]

    def queue_length(self, center_id: str) -> int:
        """Ready (every predecessor placed), unplaced ops of a work center."""
        return self._queue.get(center_id, 0)

    def is_placed(self, op_id: str) -> bool:
        return self._placed[self.static.index.pos[op_id]] is not None

    def last_op(self, machine_id: str) -> str | None:
        last = self._last[self.static.index.machine_pos[machine_id]]
        return None if last == _NONE else self.static.index.op_ids[last]

    def free_time(self, machine_id: str) -> float:
        """End of the machine's last occupation (its availability before any)."""
        return self._free[self.static.index.machine_pos[machine_id]]

    # --- position API for the engine and the policy -------------------------------------

    def estimated_start(self, p: int, m: int) -> float:
        """max(f_m, est(o, m)) for ready op `p` on machine position `m`."""
        return max(self._free[m], self._cands[m][p])

    def run_at(self, p: int, m: int) -> float:
        machine_id = self.static.index.machines[m].id
        return self.static.option_by_machine[p][machine_id].run

    def accepted(self, p: int) -> tuple[int, ...]:
        return self._policy.accepted(self, p)

    def candidates(self, m: int) -> Mapping[int, float]:
        """Ready ops eligible on machine `m` with their est on it."""
        return self._cands[m]

    def decision_time(self, m: int) -> float:
        """Non-delay `t_m` (inf when no accepted op is ready for `m`); cached."""
        cached = self._start[m]
        if cached is None:
            best = min(
                (est for p, est in self._cands[m].items() if m in self.accepted(p)), default=_INF
            )
            cached = max(self._free[m], best)
            self._start[m] = cached
        return cached

    def earliest_completion(self, m: int) -> tuple[float, int]:
        """(estimated completion, op) of the first accepted ready op on `m`; cached."""
        cached = self._completion[m]
        if cached is None:
            cached = (_INF, _NONE)
            rank = self.static.op_rank
            for p in self._cands[m]:
                if m not in self.accepted(p):
                    continue
                completion = self.estimated_completion(p, m)
                if completion < cached[0] or (
                    completion == cached[0] and rank[p] < rank[cached[1]]
                ):
                    cached = (completion, p)
            self._completion[m] = cached
        return cached

    def estimated_completion(self, p: int, m: int) -> float:
        index = self.static.index
        last = self._last[m]
        setup = index.setups.seconds(
            index.machines[m].id, None if last == _NONE else index.op_ids[last], index.op_ids[p]
        )
        return self.estimated_start(p, m) + setup + self.run_at(p, m)

    def last_pos(self, m: int) -> int:
        return self._last[m]

    # --- placing -------------------------------------------------------------------------

    def commit(self, op_id: str, machine_id: str) -> ScheduleIssue | None:
        """Place a ready op on one of its machines; the issue when no slot exists.

        ValueError for an unknown, unready or placed op, or a machine it has no option on.
        """
        index = self.static.index
        p, m = index.pos.get(op_id), index.machine_pos.get(machine_id)
        if p is None or m is None or p not in self._cands[m]:
            raise ValueError(f"{op_id} is not a ready op with an option on {machine_id}")
        return self.place(p, m)

    def place(self, p: int, m: int, *, gap_insertion: bool = False) -> ScheduleIssue | None:
        """Place ready op `p` on machine position `m`; the issue when no slot exists.

        Append-only by default, as the decoder places; `gap_insertion` lets the op take an
        earlier idle gap that keeps every placed follower's setup (forward loading).
        """
        request, found = self._find_slot(p, m, gap_insertion)
        if isinstance(found, ScheduleIssue):
            return found
        machine_id = self.static.index.machines[m].id
        self._record(p, m, self.static.option_by_machine[p][machine_id], request, found)
        return None

    def trial_end(self, p: int, m: int, *, gap_insertion: bool = False) -> float | None:
        """The end `place(p, m)` would give, without placing; None when no slot exists."""
        _, found = self._find_slot(p, m, gap_insertion)
        return None if isinstance(found, ScheduleIssue) else found.end

    def _find_slot(
        self, p: int, m: int, gap_insertion: bool
    ) -> tuple[SlotRequest, Slot | ScheduleIssue]:
        static = self.static
        op, gate = static.index.ops[p], self._gates[p]
        if gate is None:
            raise AssertionError(f"{op.id}: placed before it was ready")
        machine_id = static.index.machines[m].id
        option = static.option_by_machine[p][machine_id]
        floor = (
            static.earliest[p] if gap_insertion else max(static.earliest[p], self._last_start[m])
        )
        request = SlotRequest(
            op_id=op.id,
            run_seconds=option.run,
            earliest_start=floor,
            gate_ready=gate.ready,
            quantity=op.quantity,
            calendar=static.calendar(machine_id, op, self._pools),
            detached=op.setup_detached,
            shift_crossing=op.shift_crossing,
            batch_family=op.batch_family,
        )
        check = SlotCheck(
            gate,
            unit=option.unit_seconds,
            latest_start=op.pin.latest_start if op.pin else None,
            detached=op.setup_detached,
            needs=op.operator_needs,
            pools=self._pools,
        )
        slot = self._timelines[m].find_slot(request, check)
        if slot is None:
            return request, unplaceable_issue(op.id, check.missed_max_lag)
        return request, slot

    def plan(self) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
        """(choice, sequences) by ids; sequences in placement order, one per machine."""
        index = self.static.index
        sequences = {
            machine.id: tuple(index.op_ids[p] for p in self._sequences[m])
            for m, machine in enumerate(index.machines)
        }
        return self._choice_by_id(), sequences

    def plan_by_start(self) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
        """(choice, sequences) with each sequence in start order (after gap insertion)."""
        sequences = {timeline.machine_id: timeline.sequence() for timeline in self._timelines}
        return self._choice_by_id(), sequences

    def end_of(self, op_id: str) -> float | None:
        """The placed op's end; None while it is unplaced."""
        placed = self._placed[self.static.index.pos[op_id]]
        return None if placed is None else placed.end

    def _choice_by_id(self) -> dict[str, str]:
        index = self.static.index
        return {index.op_ids[p]: index.machines[m].id for p, m in enumerate(self._choice) if m >= 0}

    def _start_in_progress(self) -> ScheduleIssue | None:
        """In-progress ops by id on their machine at `now` (as the decoder places them)."""
        index = self.static.index
        running = [p for p, op in enumerate(index.ops) if op.in_progress is not None]
        for p in sorted(running, key=lambda p: index.op_ids[p]):
            issue = self._run_from_now(p)
            if issue is not None:
                return issue
        return None

    def _run_from_now(self, p: int) -> ScheduleIssue | None:
        index = self.static.index
        op, now, m = index.ops[p], index.now, self.static.machines_of[p][0]
        if self._last[m] != _NONE:
            return _issue("no_feasible_slot", op.id, f"{op.id}: its machine already runs an op")
        remaining = op.in_progress.remaining_seconds if op.in_progress else None
        slot = Slot(start=now, setup_end=now, end=now + (remaining or 0.0), setup_seconds=0.0)
        if op.operator_needs and operator_retry(slot, op.operator_needs, self._pools) is not None:
            return _issue("no_feasible_slot", op.id, f"{op.id}: operators busy at now")
        request = SlotRequest(
            op_id=op.id,
            run_seconds=slot.end - now,
            earliest_start=now,
            gate_ready=now,
            quantity=op.quantity,
        )
        self._record(p, m, index.ops[p].options[0], request, slot)
        return None

    def _record(self, p: int, m: int, option: OpOption, request: SlotRequest, slot: Slot) -> None:
        """Commit `slot` and update ready ops, free times and the per-machine caches."""
        static = self.static
        op = static.index.ops[p]
        self._timelines[m].place(request, slot)
        if op.operator_needs:
            hold_operators(slot, op.operator_needs, self._pools)
        self._placed[p] = Placed(
            slot.setup_end, slot.end, option.fixed_seconds, option.unit_seconds
        )
        self._free[m] = max(self._free[m], slot.end)
        if slot.start >= self._last_start[m]:  # a gap-inserted op is not the machine's last
            self._last[m], self._last_start[m] = p, slot.start
        self._sequences[m].append(p)
        self._choice[p] = m
        self._remaining -= 1
        stale = {m}
        if self._gates[p] is not None:  # it was ready (not an in-progress op)
            self._queue[op.work_center_id] -= 1
            for k in static.machines_of[p]:
                del self._cands[k][p]
                stale.add(k)
        for q in self._cands[m]:
            stale.update(static.machines_of[q])  # their policy reads f_m
        for s in static.index.successors[p]:
            self._waiting[s] -= 1
            if self._waiting[s] == 0 and self._placed[s] is None:
                stale.update(self._make_ready(s))
        for k in stale:
            self._start[k] = None
            self._completion[k] = None

    def _make_ready(self, p: int) -> tuple[int, ...]:
        """Gate and per-machine est of `p`; adds it to its machines' candidates."""
        static = self.static
        op = static.index.ops[p]
        pairs = []
        for link in op.predecessors:
            placed = self._placed[link.pred_pos]
            if placed is None:
                raise AssertionError(f"{op.id}: predecessor {link.pred_id} not placed")
            pairs.append((link, placed))
        gate = material_gate(op.release, pairs)
        base = max(gate.ready, static.earliest[p])
        for m, option in zip(static.machines_of[p], op.options, strict=True):
            floor = max((end + q * option.unit_seconds for end, q in gate.lots), default=-_INF)
            self._cands[m][p] = max(base, floor - option.run)
        self._gates[p] = gate
        self._queue[op.work_center_id] = self._queue.get(op.work_center_id, 0) + 1
        return static.machines_of[p]


def _issue(code: str, path: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path=path, message=message)


# --- Giffler-Thompson branch ----------------------------------------------------------------


def _conflict_set(state: DispatchState) -> tuple[int, float, list[int]] | None:
    """(i*, t*, C as op positions) per Alg 7.1.3; None when nothing is ready."""
    static = state.static
    best: tuple[float, int, int] | None = None
    for m in range(len(static.index.machines)):
        completion, p = state.earliest_completion(m)
        if p == _NONE:
            continue
        key = (completion, static.machine_rank[m], m)
        if best is None or key < best:
            best = key
    if best is None:
        return None
    t_star, _, m = best
    argmin = state.earliest_completion(m)[1]
    conflict = [
        p
        for p in state.candidates(m)
        if p == argmin or (state.estimated_start(p, m) < t_star and m in state.accepted(p))
    ]
    return m, t_star, conflict


def gt_branch(state: DispatchState) -> tuple[tuple[str, str], ...]:
    """The Giffler-Thompson conflict set as (op_id, machine_id) by op id; () when done."""
    found = _conflict_set(state)
    if found is None:
        return ()
    m, _, conflict = found
    index = state.index
    machine_id = index.machines[m].id
    return tuple(sorted((index.op_ids[p], machine_id) for p in conflict))


# --- the engine ------------------------------------------------------------------------------


Followers = Callable[[DispatchState, str, str], Sequence[str]]


class DispatchEngine:
    """List scheduling with a priority rule and the machine policy (module docstring)."""

    __slots__ = ("_followers", "_mode", "_policy", "_rule", "_static")

    def __init__(
        self,
        index: ProblemIndex,
        rule: PriorityRule,
        *,
        mode: Mode = "nondelay",
        policy: MachinePolicy | None = None,
    ) -> None:
        if mode not in ("nondelay", "active"):
            raise ValueError(f"unknown dispatch mode {mode!r}")
        self._static = _Static.build(index)
        self._rule = rule
        self._mode = mode
        self._policy: MachinePolicy = policy or PreferredUnlessLate()
        self._followers: Followers | None = getattr(rule, "followers", None)
        prepare: Callable[[ProblemIndex], None] | None = getattr(rule, "prepare", None)
        if prepare is not None:
            prepare(index)

    def initial_state(self) -> DispatchState:
        return DispatchState.initial(self._static, self._policy)

    def run(self, deadline: float | None = None) -> Dispatched | ScheduleIssue:
        """Dispatch every op, then decode; `time_limit` once `deadline` (monotonic) passes."""
        return self.complete(self.initial_state(), deadline)

    def complete(
        self, state: DispatchState, deadline: float | None = None
    ) -> Dispatched | ScheduleIssue:
        """Dispatch the rest of `state` (mutated in place), then decode (rollouts, beam)."""
        if state.issue is not None:
            return state.issue
        while not state.done:
            if deadline is not None and time.monotonic() > deadline:
                return _issue("time_limit", "budget_seconds", "dispatch ran out of time")
            issue = self._step(state)
            if issue is not None:
                return issue
        if deadline is not None and time.monotonic() > deadline:
            return _issue("time_limit", "budget_seconds", "dispatch ran out of time")
        choice, sequences = state.plan()
        decoded = decode(self._static.index, choice, sequences)
        if isinstance(decoded, ScheduleIssue):
            return decoded
        return Dispatched(choice=choice, sequences=sequences, decoded=decoded)

    def _step(self, state: DispatchState) -> ScheduleIssue | None:
        decision = self._nondelay(state) if self._mode == "nondelay" else self._active(state)
        if decision is None:
            return _issue("no_feasible_slot", "problem", "unplaced ops but none is ready")
        m, t, conflict = decision
        return self._dispatch(state, m, t, conflict)

    def _nondelay(self, state: DispatchState) -> tuple[int, float, list[int]] | None:
        rank = self._static.machine_rank
        best: tuple[float, int, int] | None = None
        for m in range(len(rank)):
            key = (state.decision_time(m), rank[m], m)
            if best is None or key < best:
                best = key
        if best is None or best[0] == _INF:
            return None
        t, _, m = best
        conflict = [
            p
            for p, est in state.candidates(m).items()
            if est <= t + _EPS and m in state.accepted(p)
        ]
        return m, t, conflict

    def _active(self, state: DispatchState) -> tuple[int, float, list[int]] | None:
        found = _conflict_set(state)
        if found is None:
            return None
        m, _, conflict = found
        t = min(state.estimated_start(p, m) for p in conflict)
        return m, t, conflict

    def _dispatch(
        self, state: DispatchState, m: int, t: float, conflict: list[int]
    ) -> ScheduleIssue | None:
        """Place the best op of `conflict` that fits on `m`; the first issue when none does."""
        if self._follow(state, m):
            return None
        first_issue: ScheduleIssue | None = None
        for p in self._ordered(state, m, t, conflict):
            issue = state.place(p, m)
            if issue is None:
                return None
            first_issue = first_issue or issue
        return first_issue

    def _follow(self, state: DispatchState, m: int) -> bool:
        """Place the first follower of `m`'s last op whose gate is met (module docstring)."""
        last = state.last_pos(m)
        if self._followers is None or last == _NONE:
            return False
        index = self._static.index
        machine_id = index.machines[m].id
        free = state.free_time(machine_id)
        for follower in self._followers(state, index.op_ids[last], machine_id):
            q = index.pos[follower]
            est = state.candidates(m).get(q)
            if est is not None and est <= free + _EPS and state.place(q, m) is None:
                return True
        return False

    def _ordered(self, state: DispatchState, m: int, t: float, conflict: list[int]) -> list[int]:
        """`conflict` by rule index, highest first; ties by op id."""
        index = self._static.index
        machine_id = index.machines[m].id
        last = state.last_pos(m)
        last_id = None if last == _NONE else index.op_ids[last]
        keys = {
            p: _as_key(self._rule.index(state, index.op_ids[p], machine_id, t, last_id))
            for p in conflict
        }
        by_id = sorted(conflict, key=self._static.op_rank.__getitem__)
        return sorted(by_id, key=keys.__getitem__, reverse=True)


def _as_key(value: Priority) -> tuple[float, ...]:
    return value if isinstance(value, tuple) else (value,)


# --- the solver ------------------------------------------------------------------------------


class DispatchSolver:
    """A priority rule on the dispatch engine behind the `Solver` seam (COMP-009).

    `rule_factory` builds a fresh rule per solve (rules may keep prepared tables). Returns
    FEASIBLE with the primary objective, or UNKNOWN with the engine's or decoder's issue.
    """

    __slots__ = ("_mode", "_name", "_rule_factory")

    def __init__(
        self, name: str, rule_factory: Callable[[], PriorityRule], *, mode: Mode = "nondelay"
    ) -> None:
        self._name = name
        self._rule_factory = rule_factory
        self._mode: Mode = mode

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> frozenset[Feature]:
        """Every feature: placement goes through the shared timelines and the decoder."""
        return frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        """One dispatch pass, cut off with `time_limit` at `context.deadline`."""
        started = time.perf_counter()
        index = ProblemIndex.build(problem)
        engine = DispatchEngine(index, self._rule_factory(), mode=self._mode)
        return dispatch_schedule(self._name, engine, index, objective, context, started)


def dispatch_schedule(
    name: str,
    engine: DispatchEngine,
    index: ProblemIndex,
    objective: ObjectiveSpec,
    context: SolveContext,
    started: float,
) -> Schedule:
    """Run `engine` to `context.deadline`: FEASIBLE with the primary value, else UNKNOWN.

    `started` is the solve's `time.perf_counter()` start, for `elapsed_seconds`.
    """
    result = engine.run(deadline=context.deadline)
    return dispatched_schedule(name, result, index, objective, context, started)


def dispatched_schedule(
    name: str,
    result: Dispatched | ScheduleIssue,
    index: ProblemIndex,
    objective: ObjectiveSpec,
    context: SolveContext,
    started: float,
) -> Schedule:
    """A dispatch result as a `Schedule`: FEASIBLE with the primary value, else UNKNOWN."""
    if isinstance(result, ScheduleIssue):
        return Schedule(
            status="UNKNOWN",
            assignments=(),
            solver=name,
            elapsed_seconds=time.perf_counter() - started,
            issues=(result,),
        )
    values = fast_values(index, result.decoded, context.previous_plan, context.churn_tolerance)
    return result.decoded.schedule(name).model_copy(
        update={
            "primary_value": values[primary_kind(objective)],
            "elapsed_seconds": time.perf_counter() - started,
        }
    )
