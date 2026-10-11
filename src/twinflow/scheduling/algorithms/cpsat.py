"""Optional CP-SAT solver behind the `Solver` seam (COMP-010, COMP-016, REQ-076, REQ-104,
REQ-106, D-067, D-068, D-075).

`ORToolsSolver` translates a compiled `Problem` into one CP-SAT model:

- per operation a start, a setup end, an end and one optional interval per (operation,
  option) of size setup + run, with `AddExactlyOne` over the option presences; a pinned
  machine keeps only its own option;
- `AddNoOverlap` per machine over its option intervals plus the fixed break intervals of
  its calendar, which enforces `forbid` with a model size linear in calendar intervals
  (D-065);
- sequence-dependent setups (D-075): on a machine with a setup function, an `AddCircuit`
  over its option presences with a depot node for the machine state (or its in-progress
  operation). The arc into an option fixes that option's setup variable (from the depot:
  the setup after the state; from another option: the setup after that operation) and
  bounds its start by the previous end. A machine with a restart rule (D-074, D-075) is
  sequenced the same way, setups or not: each option has a `warm` literal, and every arc
  into it (the depot arc measures idle from max(now, available_from), or from the end of
  the machine's in-progress operation) enforces warm <=> idle > threshold, idle in ticks
  (so one tick past the threshold is warm, equal is cold, TF-003 DEV-4). The warm-up joins
  the setup phase: setup_end = start + setup + warm * warmup. With a detached setup the
  start may move earlier by the warm-up, which the verifier accepts;
- batch machines (D-075): instead of slots per family, a leader formulation (exact and
  smaller): an option on a batch machine either leads a batch or joins the batch of an
  earlier operation with the same family, run length and (with setups) the same setup
  parameters and setup family, so every member has the same setups on both sides. Members
  share start, setup_end, end, setup and warm-up with the leader; the leader's load (op
  quantities in integer millionths of the machine's unit) fits the capacity; only leaders
  occupy the machine (no-overlap and circuit). Operations without a family run alone;
- transfer-lot overlap (D-061) is linear: gate >= pred.setup_end + pred.fixed + q *
  pred.unit + min_lag (max_lag from the same point) and end >= pred.end + q * succ.unit,
  per chosen option;
- the material gate is `start`, or `setup_end` for a detached setup: readiness and every
  predecessor lag bound the gate, pins and machine availability bound the start;
- `AddCumulative` per operator pool over each need's phase (setup, run or both) and, for
  a pool with a calendar, a "fence" cumulative that keeps every operation drawing on the
  pool out of the pool's breaks (TF-002 DEV-6);
- readiness, pins and the horizon as bounds; in-progress operations fixed at `now`.

Size guard (D-075): before any table or model is built, more than `MAX_MACHINE_OPTIONS`
options on one machine, or more than `MAX_CIRCUIT_ARCS` circuit arcs (counted as (n + 1)^2
per sequenced machine), returns UNKNOWN `model_too_large`; the count is one pass over the
options.

Warm start and horizon cap (T-138, T-139, TF-002 review item 3): the model is hinted with
`context.warm_start`, else with the best verified plan under the objective (by
`kernel.score.Scorer` over `objectives.evaluate`) among the baseline and the methods named
in the `hint_solvers` param (comma-separated registry names, default `atcs`, looked up at
call time, D-069; a name not registered is skipped with a debug log). Every hint solver
runs within the same deadline, so hint time counts against the budget. Hinted: option
presences, circuit arcs, warm-ups and batch links (starts are left to propagation, see
`_add_hints`). A verified hint is also the floor of the answer: when CP-SAT ends FEASIBLE
with a plan the hint outranks, or runs out of time without a plan, the hint's plan is
returned FEASIBLE. When the objective is makespan
alone and the hint verifies, the horizon is capped at the hint's last end, so every
variable is bounded by a plan known to exist; a capped model CP-SAT calls infeasible (only
tick rounding could do that) is solved again uncapped. The cap never applies to any other
objective, since finishing later can pay off there (earliness, churn, robustness, setups,
due-date mixes).

Zero-length operations (TF-002 review item 2): CP-SAT's `AddNoOverlap` lets a zero-size
interval touch another interval's start or end but never sit strictly inside it, which
is the verifier's rule (TF-002 DEV-11), so a zero-length run holds no tick and pushes no
successor. A calendar also rejects an empty span at a break start (that instant is not
working), so the start of an option that can be zero-length is restricted to the working
instants of its machine and pool calendars.

Objectives (T-134): every `ObjectiveKind` is a linear form with integer coefficients over
model variables (`_Linear`): times in ticks, job weights, costs and hour costs in integer
millionths (`MICRO`); a positive one that rounds to 0 is refused with
`unsupported_precision`. A spec becomes one or more solve stages. Weighted sum: one stage
minimizing sum weight * signed(value) / scale over a common integer unit, each
multiplier in millionths (a positive one that rounds to 0 is refused the same way); a term
without `scale` uses 1, since CP-SAT has no reference schedule to normalize by as
`objectives.rank` does. Lexicographic: one stage per level; after a level, its signed value
is bounded by the value found plus the level's tolerance (absolute, or relative to that
value). Epsilon-constraint: each bound is a constraint on the raw value, then one stage for
the primary. Robustness is maximized; churn counts against `context.previous_plan` with
`context.churn_tolerance`. An objective whose integer range passes `MAX_OBJECTIVE` is
refused with `unsupported_magnitude`. The answer is OPTIMAL only when every stage is proven
optimal. `best_bound` is reported when the first stage optimizes a single kind
(lexicographic, epsilon-constraint, or a one-term weighted sum). The time limit is the
earlier of the budget and `context.deadline`, shared evenly by the stages still to run, and
each later stage is hinted with the previous stage's plan.

Times are integers at `SCALE` ticks per second, measured from `now` so values stay small;
time inputs with more than six decimals are refused (`unsupported_precision`) instead of
silently rounded, and inputs whose ticks (a time, a run, a setup, the horizon) pass
`MAX_TICKS` are refused (`unsupported_magnitude`) before a model is built. A run of
`fixed + quantity * unit` and a setup are rounded to the nearest tick (error <= 0.5
microseconds, inside the verifier's 1e-6 tolerance).

ortools is an optional extra: it is imported when the solver is built, and its absence
raises `seam.OptionalDependencyError`. A leaf of `algorithms/` (D-069): it registers
through `SOLVERS` and nothing outside `algorithms/` imports it.
"""

from __future__ import annotations

import importlib
import logging
import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from itertools import permutations
from typing import Any, Literal

from twinflow.scheduling.baseline import BaselineSolver
from twinflow.scheduling.features import problem_features
from twinflow.scheduling.kernel.index import DEFAULT_CLASS_WEIGHTS
from twinflow.scheduling.kernel.score import Scorer, Values
from twinflow.scheduling.model import (
    Assignment,
    CompiledProblem,
    EpsilonConstraint,
    FrozenZone,
    Job,
    Lexicographic,
    LexicographicLevel,
    Machine,
    ObjectiveKind,
    ObjectiveSpec,
    Operation,
    PreviousPlan,
    Problem,
    ProcessingOption,
    Schedule,
    ScheduleIssue,
    SchedulingProfile,
)
from twinflow.scheduling.objectives import (
    SECONDS_PER_HOUR,
    SENSE,
    evaluate,
    level_tolerance,
    primary_kind,
)
from twinflow.scheduling.registry import SolverSpec, is_registered
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature, OptionalDependencyError, SolveContext, Solver
from twinflow.scheduling.setups import SetupTable
from twinflow.scheduling.verifier import verify

SCALE = 1_000_000
"""Ticks per second: CP-SAT works on integers, times are float seconds."""

MAX_DECIMALS = 6
MAX_TICKS = 2**52
"""Largest tick magnitude a model value may have (about 142 years of seconds). CP-SAT
domains must stay inside +-int64max / 2, and expressions add a few such values (an end
plus a lag, a lateness), so 2**52 leaves ample headroom."""
MAX_OBJECTIVE = 2**62
"""Largest sum of |integer coefficient| * |variable bound| an objective may reach."""
MICRO = 1_000_000
"""Objective weights, costs and weight / scale multipliers are integers in millionths."""
BOUND_SLACK = 1e-6
"""Integer units of float noise forgiven when a bound (epsilon, level) is floored."""
LINEARIZATION_LEVEL = 2
"""CP-SAT's full LP relaxation: on mixed objectives over alternatives, setups and pool
fences the default level (1) left small instances unproven where level 2 proves them."""
MAX_MACHINE_OPTIONS = 300
"""More options than this on one machine is `model_too_large` (D-075)."""
MAX_CIRCUIT_ARCS = 2_000_000
"""More circuit arcs than this over all sequenced machines is `model_too_large` (D-075)."""
HINT_BUDGET_SHARE = 0.25
"""Most of the remaining budget the hint completion solve may use (wall clock)."""
HINT_DETERMINISTIC_TIME = 2.0
"""CP-SAT deterministic-time cap of the hint completion solve, so it stops at the same point
on every run with one worker."""
HINT_SOLVERS_PARAM = "hint_solvers"
DEFAULT_HINT_SOLVERS = "atcs"
"""Registry names whose plans compete with the baseline's for the hint (T-139, D-069)."""
SOLVER_NAME = "ortools"
OBJECTIVE_PATH = "profile.objective"
CAPABILITIES = frozenset(
    {
        Feature.ALTERNATIVES,
        Feature.CALENDARS,
        Feature.OPERATORS,
        Feature.LAGS,
        Feature.MAX_LAGS,
        Feature.PINS,
        Feature.IN_PROGRESS,
        Feature.MACHINE_STATE,
        Feature.SETUPS,
        Feature.DETACHED_SETUP,
        Feature.OVERLAP,
        Feature.RESTART,
        Feature.BATCHING,
    }
)

log = logging.getLogger(__name__)

Status = Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"]
TickInterval = tuple[int, int]


def _ticks(seconds: float) -> int:
    return round(seconds * SCALE)


def _seconds(ticks: int) -> float:
    return ticks / SCALE


def _weight(job: Job) -> float:
    """Effective weight: compiled jobs carry it in `weight_override`, else the default class."""
    if job.weight_override is not None:
        return job.weight_override
    return DEFAULT_CLASS_WEIGHTS[job.weight_class]


def _machine_ids(op: Operation) -> list[str]:
    """Option machines of a pending operation; a pinned machine keeps only its own option."""
    pin_machine = op.pin.machine_id if op.pin else None
    return [o.machine_id for o in op.options if pin_machine in (None, o.machine_id)]


def _option(op: Operation, machine_id: str) -> ProcessingOption | None:
    return next((o for o in op.options if o.machine_id == machine_id), None)


def _run_ticks(op: Operation, machine_id: str) -> int:
    option = next(o for o in op.options if o.machine_id == machine_id)
    return _ticks(option.fixed_seconds + op.quantity * option.unit_seconds)


def _in_progress_machine(op: Operation) -> str:
    return op.status.machine_id or op.options[0].machine_id


# --- refusals -------------------------------------------------------------------------------


def _feature_refusals(problem: Problem) -> list[ScheduleIssue]:
    """`unsupported_feature` per feature outside `CAPABILITIES` (D-067). `solve()` checks this
    too, but composite methods call this solver past it; without the check a `pause` op
    would be modeled as `forbid` and the answer reported OPTIMAL for the wrong problem."""
    missing = sorted(problem_features(problem) - CAPABILITIES)
    return [
        ScheduleIssue(
            code="unsupported_feature",
            path="problem",
            message=f"solver {SOLVER_NAME} does not support feature {feature.value}",
        )
        for feature in missing
    ]


def _precision_refusals(problem: Problem, context: SolveContext) -> list[ScheduleIssue]:
    """`unsupported_precision` for the first time input with more than six decimals."""
    for path, value in _time_values(problem, context):
        if round(value, MAX_DECIMALS) != value:
            message = f"{value!r} has more than {MAX_DECIMALS} decimal places"
            return [ScheduleIssue(code="unsupported_precision", path=path, message=message)]
    return []


def _magnitude_refusals(
    problem: Problem, context: SolveContext, horizon: int, setups: Mapping[str, _MachineSetups]
) -> list[ScheduleIssue]:
    """`unsupported_magnitude` for the first value whose ticks pass `MAX_TICKS`.

    Checked: every time input, every operation's summed option runs (an end expression
    adds them), the largest setup and the horizon. Every term of a lag constraint or a
    lateness is a sum of a few of these, so all stay far inside int64.
    """
    largest_setup = max((s.largest() for s in setups.values()), default=0)
    values = [
        ("problem.horizon_end", _seconds(horizon)),
        ("problem.setup_functions", _seconds(largest_setup)),
        *_time_values(problem, context),
        *_run_values(problem),
    ]
    for path, value in values:
        if abs(_ticks(value)) > MAX_TICKS:
            message = f"{value:g} s is too large for CP-SAT ticks (limit {MAX_TICKS / SCALE:g} s)"
            return [ScheduleIssue(code="unsupported_magnitude", path=path, message=message)]
    return []


def _size_refusals(
    problem: Problem, scheduled: Sequence[tuple[Job, Operation]]
) -> list[ScheduleIssue]:
    """`model_too_large` when a machine has too many options or the circuits too many arcs.

    One pass over the pending operations' options; a sequenced machine (setup function or
    restart rule) contributes (n + 1)^2 arcs for n options (depot, self loops, pairs).
    """
    counts: Counter[str] = Counter(
        machine_id
        for _, op in scheduled
        if op.status.state != "in_progress"
        for machine_id in _machine_ids(op)
    )
    for m, machine in enumerate(problem.machines):
        if counts[machine.id] > MAX_MACHINE_OPTIONS:
            message = (
                f"machine {machine.id} has {counts[machine.id]} options "
                f"(limit {MAX_MACHINE_OPTIONS})"
            )
            return [_too_large(f"problem.machines[{m}]", message)]
    table = SetupTable.for_problem(problem)
    arcs = sum(
        (counts[m.id] + 1) ** 2
        for m in problem.machines
        if counts[m.id] and (m.restart is not None or table.function_for(m.id) is not None)
    )
    if arcs > MAX_CIRCUIT_ARCS:
        message = f"the setup circuits need {arcs} arcs (limit {MAX_CIRCUIT_ARCS})"
        return [_too_large("problem.machines", message)]
    return []


def _too_large(path: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code="model_too_large", path=path, message=message)


def _run_values(problem: Problem) -> Iterator[tuple[str, float]]:
    """Per operation, the sum of its option runs `fixed + quantity * unit`."""
    for j, job in enumerate(problem.jobs):
        for k, op in enumerate(job.operations):
            runs = (o.fixed_seconds + op.quantity * o.unit_seconds for o in op.options)
            yield f"problem.jobs[{j}].operations[{k}].options", sum(runs)


def _time_values(problem: Problem, context: SolveContext) -> Iterator[tuple[str, float]]:
    """Every time-valued input the model reads, with its JSON path."""
    yield "problem.now", problem.now
    if problem.horizon_end is not None:
        yield "problem.horizon_end", problem.horizon_end
    for m, machine in enumerate(problem.machines):
        yield f"problem.machines[{m}].state.available_from", machine.state.available_from
        if machine.restart is not None:
            path = f"problem.machines[{m}].restart"
            yield f"{path}.idle_threshold_seconds", machine.restart.idle_threshold_seconds
            yield f"{path}.warmup_seconds", machine.restart.warmup_seconds
    for c, calendar in enumerate(problem.calendars):
        for k, (start, end) in enumerate(calendar.intervals):
            yield f"problem.calendars[{c}].intervals[{k}][0]", start
            yield f"problem.calendars[{c}].intervals[{k}][1]", end
    for j, job in enumerate(problem.jobs):
        yield from _job_time_values(f"problem.jobs[{j}]", job)
    if context.previous_plan is not None:
        yield "profile.frozen_zone.tolerance", context.churn_tolerance
        for k, previous in enumerate(context.previous_plan.assignments):
            yield f"previous_plan.assignments[{k}].start", previous.start


def _job_time_values(path: str, job: Job) -> Iterator[tuple[str, float]]:
    yield f"{path}.release", job.release
    if job.due is not None:
        yield f"{path}.due", job.due
    for k, op in enumerate(job.operations):
        yield from _operation_time_values(f"{path}.operations[{k}]", op)


def _operation_time_values(path: str, op: Operation) -> Iterator[tuple[str, float]]:
    optional = {
        "pin.earliest_start": op.pin.earliest_start if op.pin else None,
        "pin.latest_start": op.pin.latest_start if op.pin else None,
        "status.remaining_seconds": op.status.remaining_seconds,
    }
    yield f"{path}.ready_time", op.ready_time
    yield from ((f"{path}.{name}", v) for name, v in optional.items() if v is not None)
    for n, option in enumerate(op.options):
        yield f"{path}.options[{n}].fixed_seconds", option.fixed_seconds
        yield f"{path}.options[{n}].unit_seconds", option.unit_seconds
    for n, link in enumerate(op.predecessors):
        yield f"{path}.predecessors[{n}].min_lag", link.min_lag
        if link.max_lag is not None:
            yield f"{path}.predecessors[{n}].max_lag", link.max_lag


# --- setups ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _MachineSetups:
    """Setup ticks on one machine with a setup function (D-064, D-075).

    `first[o]`: before o when it is first, after the machine state or the machine's
    in-progress operation. `between[a, b]`: before b when a came last.
    """

    first: Mapping[str, int]
    between: Mapping[tuple[str, str], int]
    most: Mapping[str, int]
    """Largest setup before each operation on this machine, whatever came last."""

    @classmethod
    def of(cls, first: dict[str, int], between: dict[tuple[str, str], int]) -> _MachineSetups:
        most = dict(first)
        for (_, b), ticks in between.items():
            most[b] = max(most[b], ticks)
        return cls(first, between, most)

    def largest(self) -> int:
        return max(self.most.values(), default=0)


def _setup_tables(
    problem: Problem, scheduled: Sequence[tuple[Job, Operation]]
) -> dict[str, _MachineSetups]:
    """Setup ticks for every machine whose setup function is set (D-064 resolution)."""
    table = SetupTable.for_problem(problem)
    depot: dict[str, str | None] = {}
    members: dict[str, list[str]] = defaultdict(list)
    for _, op in scheduled:
        if op.status.state == "in_progress":
            depot[_in_progress_machine(op)] = op.id
            continue
        for machine_id in _machine_ids(op):
            members[machine_id].append(op.id)
    setups: dict[str, _MachineSetups] = {}
    for machine in problem.machines:
        if table.function_for(machine.id) is None:
            continue
        ops = members.get(machine.id, [])
        before = depot.get(machine.id)
        first = {o: _ticks(table.seconds(machine.id, before, o)) for o in ops}
        between = {(a, b): _ticks(table.seconds(machine.id, a, b)) for a, b in permutations(ops, 2)}
        setups[machine.id] = _MachineSetups.of(first, between)
    return setups


# --- horizon --------------------------------------------------------------------------------


def _scheduled(problem: Problem) -> list[tuple[Job, Operation]]:
    """Operations to place: not done, of jobs that are neither done nor held."""
    return [
        (job, op)
        for job in problem.jobs
        if job.status == "open"
        for op in job.operations
        if op.status.state != "done"
    ]


def _safe_horizon(
    problem: Problem,
    scheduled: Sequence[tuple[Job, Operation]],
    setups: Mapping[str, _MachineSetups],
    previous_plan: PreviousPlan | None,
) -> int:
    """A bound no optimal schedule needs to exceed: the latest fixed time plus all work.

    Fixed times are now, releases, ready times, dues, previous-plan starts, machine
    availability, pins, calendar ends and in-progress ends (dues and previous starts
    because earliness and churn reward waiting for them); all work is every operation's
    longest option with its largest setup and warm-up plus every min lag. Used when
    `problem.horizon_end` is not set.
    """
    warmups = {m.id: m.restart.warmup_seconds for m in problem.machines if m.restart}
    anchors = [problem.now, *(m.state.available_from for m in problem.machines)]
    anchors += [end for calendar in problem.calendars for _, end in calendar.intervals]
    anchors += [job.due for job, _ in scheduled if job.due is not None]
    if previous_plan is not None:
        anchors += [previous.start for previous in previous_plan.assignments]
    work = 0.0
    setup_ticks = 0
    for job, op in scheduled:
        pin_times = (op.pin.earliest_start, op.pin.latest_start) if op.pin else ()
        anchors += [job.release, op.ready_time, *(t for t in pin_times if t is not None)]
        anchors.append(problem.now + (op.status.remaining_seconds or 0.0))
        work += max(o.fixed_seconds + op.quantity * o.unit_seconds for o in op.options)
        work += sum(link.min_lag for link in op.predecessors)
        work += max((warmups.get(m, 0.0) for m in _machine_ids(op)), default=0.0)
        if op.status.state != "in_progress":
            machine_setups = [setups[m].most[op.id] for m in _machine_ids(op) if m in setups]
            setup_ticks += max(machine_setups, default=0)
    return _ticks(max(anchors)) + _ticks(work) + setup_ticks + 1


def _horizon(
    problem: Problem,
    scheduled: Sequence[tuple[Job, Operation]],
    setups: Mapping[str, _MachineSetups],
    previous_plan: PreviousPlan | None,
) -> int:
    """Last tick any operation may end: `horizon_end` (never before now), else a safe bound."""
    if problem.horizon_end is None:
        return _safe_horizon(problem, scheduled, setups, previous_plan)
    return max(_ticks(problem.horizon_end), _ticks(problem.now))


def _breaks(intervals: Sequence[TickInterval], high: int) -> list[TickInterval]:
    """Non-working tick ranges inside `[0, high)`; touching intervals leave no break."""
    gaps: list[TickInterval] = []
    cursor = 0
    for start, end in intervals:
        if start > cursor:
            gaps.append((cursor, min(start, high)))
        cursor = max(cursor, end)
        if cursor >= high:
            return gaps
    gaps.append((cursor, high))
    return gaps


# --- model ----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Choice:
    """One option of a placed operation.

    `phase` is the setup phase in ticks (setup + warm * warm-up, or 0); `interval` the
    operation's occupation for operator pools; `active` and `block` the node literal and
    interval the machine sequences (the presence and `interval`, except on a batch machine
    where only a batch leader occupies the machine). `links` pairs each batch the option
    may belong to (by leader operation id) with its literal.
    """

    machine_id: str
    presence: Any
    run: int
    setup: Any
    warm: Any
    phase: Any
    interval: Any
    active: Any = None
    block: Any = None
    links: list[tuple[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _Placed:
    """One operation in the model: start, setup end, material gate, end and options."""

    job: Job
    op: Operation
    start: Any
    setup_end: Any
    gate: Any
    end: Any
    choices: list[_Choice]


def _setup_signature(op: Operation) -> tuple[object, ...]:
    """What setups read of an operation (D-064): equal signatures mean equal setups."""
    return (op.setup_family, tuple(sorted(op.setup_params.items())))


@dataclass
class _Builder:
    """Builds the CP-SAT constraints for one problem; times are ticks after `origin` (now)."""

    cp_model: Any
    problem: Problem
    horizon: int
    origin: int
    setups: Mapping[str, _MachineSetups]
    model: Any = None
    placed: dict[str, _Placed] = field(default_factory=dict)
    machine_choices: dict[str, list[tuple[_Placed, _Choice]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    machine_floor: dict[str, int] = field(default_factory=dict)
    pool_usage: dict[str, list[tuple[Any, int]]] = field(default_factory=lambda: defaultdict(list))
    pool_members: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(list))
    machines: dict[str, Machine] = field(default_factory=dict)
    calendars: dict[str, list[TickInterval]] = field(default_factory=dict)
    pool_calendars: dict[str, str | None] = field(default_factory=dict)
    arcs: dict[tuple[str, str | None, str | None], Any] = field(default_factory=dict)
    """Circuit literals by (machine, previous op or None for the depot, next op or None)."""

    def at(self, seconds: float) -> int:
        """Model ticks of an absolute time in seconds."""
        return _ticks(seconds) - self.origin

    def build(self, scheduled: Sequence[tuple[Job, Operation]]) -> None:
        """Add every variable and constraint; objectives come later (`_Terms`)."""
        self.model = self.cp_model.CpModel()
        self.machines = {m.id: m for m in self.problem.machines}
        self.calendars = {
            c.id: [(self.at(start), self.at(end)) for start, end in c.intervals]
            for c in self.problem.calendars
        }
        self.pool_calendars = {p.id: p.calendar_id for p in self.problem.operator_pools}
        for job, op in scheduled:
            if op.status.state == "in_progress":
                self._add_in_progress(job, op)
        for job, op in scheduled:
            if op.status.state != "in_progress":
                self._add_pending(job, op)
        for placed in self.placed.values():
            if placed.op.status.state != "in_progress":
                self._add_precedence(placed)
        self._add_batches()
        self._add_machines()
        self._add_pools()

    def sequenced(self, machine_id: str) -> bool:
        """A machine whose options are ordered by a circuit: setups or a restart rule."""
        return machine_id in self.setups or self.machines[machine_id].restart is not None

    # operations

    def _add_in_progress(self, job: Job, op: Operation) -> None:
        """Fixed on its status machine at `now` for the remaining seconds, setup 0."""
        machine_id = _in_progress_machine(op)
        remaining = _ticks(op.status.remaining_seconds or 0.0)
        self.machine_floor[machine_id] = max(self.machine_floor.get(machine_id, 0), remaining)
        interval = self.model.NewFixedSizeIntervalVar(0, remaining, f"run_{op.id}")
        for need in op.operator_needs:
            if need.phase in ("run", "both"):
                self.pool_usage[need.pool_id].append((interval, need.quantity))
        choice = _Choice(machine_id, 1, remaining, 0, 0, 0, interval)
        self.placed[op.id] = _Placed(job, op, 0, 0, 0, remaining, [choice])

    def _add_pending(self, job: Job, op: Operation) -> None:
        """Start, options and end; a setup end and an end variable only with a phase."""
        start = self.model.NewIntVar(0, self.horizon, f"start_{op.id}")
        machine_ids = _machine_ids(op)
        has_phase = any(self.sequenced(m) for m in machine_ids)
        setup_end, end = start, None
        if has_phase:
            setup_end = self.model.NewIntVar(0, self.horizon, f"setup_end_{op.id}")
            end = self.model.NewIntVar(0, self.horizon, f"end_{op.id}")
        gate = setup_end if op.setup_detached else start
        placed = _Placed(job, op, start, setup_end, gate, end, [])
        placed.choices = [self._add_option(placed, machine_id) for machine_id in machine_ids]
        choices = placed.choices
        self.model.AddExactlyOne(c.presence for c in choices)
        runs = sum(c.run * c.presence for c in choices)
        if has_phase:
            self.model.Add(setup_end == start + sum(c.phase for c in choices))
            self.model.Add(end == setup_end + runs)
        else:
            placed.end = start + runs
            self.model.Add(placed.end <= self.horizon)
        self._add_start_bounds(placed)
        self.placed[op.id] = placed

    def _add_start_bounds(self, placed: _Placed) -> None:
        """Material readiness on the gate; the pin window on the start."""
        job, op = placed.job, placed.op
        self.model.Add(placed.gate >= self.at(max(job.release, op.ready_time)))
        if op.pin is not None and op.pin.earliest_start is not None:
            self.model.Add(placed.start >= self.at(op.pin.earliest_start))
        if op.pin is not None and op.pin.latest_start is not None:
            self.model.Add(placed.start <= self.at(op.pin.latest_start))

    def _add_option(self, placed: _Placed, machine_id: str) -> _Choice:
        """The option's presence, phase, occupation, machine availability and operator needs."""
        op, start = placed.op, placed.start
        presence = self.model.NewBoolVar(f"on_{op.id}_{machine_id}")
        run = _run_ticks(op, machine_id)
        if self.sequenced(machine_id):
            choice = self._phased_choice(placed, machine_id, presence, run)
        else:
            interval = self.model.NewOptionalFixedSizeIntervalVar(
                start, run, presence, f"run_{op.id}_{machine_id}"
            )
            choice = _Choice(machine_id, presence, run, 0, 0, 0, interval)
        choice.active, choice.block = presence, choice.interval
        available_from = self.machines[machine_id].state.available_from
        self.model.Add(start >= self.at(available_from)).OnlyEnforceIf(presence)
        self.machine_choices[machine_id].append((placed, choice))
        self._add_needs(placed, choice)
        if run == 0:
            self._add_zero_length_starts(placed, choice)
        return choice

    def _phased_choice(self, placed: _Placed, machine_id: str, presence: Any, run: int) -> _Choice:
        """An option with a setup variable (setup function) and a warm literal (restart)."""
        op, model = placed.op, self.model
        setups = self.setups.get(machine_id)
        rule = self.machines[machine_id].restart
        setup: Any = 0
        if setups is not None:
            setup = model.NewIntVar(0, setups.most[op.id], f"setup_{op.id}_{machine_id}")
            model.Add(setup == 0).OnlyEnforceIf(presence.Not())
        warm: Any = 0
        if rule is not None:
            warm = model.NewBoolVar(f"warm_{op.id}_{machine_id}")
            model.AddImplication(warm, presence)
        phase: Any = setup
        if rule is not None and setups is not None:
            # An interval size must be affine: one variable carries setup + warm-up.
            warmup = _ticks(rule.warmup_seconds)
            phase = model.NewIntVar(0, setups.most[op.id] + warmup, f"phase_{op.id}_{machine_id}")
            model.Add(phase == setup + warmup * warm)
        elif rule is not None:
            phase = _ticks(rule.warmup_seconds) * warm
        interval = model.NewOptionalIntervalVar(
            placed.start, phase + run, placed.end, presence, f"busy_{op.id}_{machine_id}"
        )
        return _Choice(machine_id, presence, run, setup, warm, phase, interval)

    def _add_needs(self, placed: _Placed, choice: _Choice) -> None:
        """Pool capacity per need phase; every drawing option joins its pools' fences."""
        for need in placed.op.operator_needs:
            self.pool_members[need.pool_id].append(choice.interval)
            interval = self._phase_interval(placed, choice, need.phase)
            if interval is not None:
                self.pool_usage[need.pool_id].append((interval, need.quantity))

    def _phase_interval(self, placed: _Placed, choice: _Choice, phase: str) -> Any:
        """Setup [start, setup_end), run [setup_end, end) or both; None for an empty setup."""
        if phase == "both":
            return choice.interval
        name = f"{phase}_{placed.op.id}_{choice.machine_id}"
        if phase == "run" and isinstance(choice.phase, int):
            return choice.interval
        if phase == "run":
            return self.model.NewOptionalFixedSizeIntervalVar(
                placed.setup_end, choice.run, choice.presence, name
            )
        if isinstance(choice.phase, int):
            return None
        return self.model.NewOptionalIntervalVar(
            placed.start, choice.phase, placed.setup_end, choice.presence, name
        )

    def _add_zero_length_starts(self, placed: _Placed, choice: _Choice) -> None:
        """An empty span must start at a working instant of the machine and pool calendars.

        `AddNoOverlap` already keeps it out of a break's inside; this also rejects the break's
        first instant, which is not working either. With a setup phase the rule applies only
        when the phase is 0 too, read by a literal tied to the phase.
        """
        calendar_ids = [self.machines[choice.machine_id].calendar_id]
        calendar_ids += [self.pool_calendars[n.pool_id] for n in placed.op.operator_needs]
        calendars = [self.calendars[c] for c in calendar_ids if c is not None]
        if not calendars:
            return
        enforced = [choice.presence]
        if not isinstance(choice.phase, int):
            empty = self.model.NewBoolVar(f"empty_{placed.op.id}_{choice.machine_id}")
            self.model.Add(choice.phase == 0).OnlyEnforceIf(empty)
            self.model.Add(choice.phase >= 1).OnlyEnforceIf(empty.Not())
            enforced.append(empty)
        for intervals in calendars:
            working = [[lo, hi - 1] for lo, hi in intervals if hi > lo]
            domain = self.cp_model.Domain.FromIntervals(working)
            self.model.AddLinearExpressionInDomain(placed.start, domain).OnlyEnforceIf(enforced)

    def _add_precedence(self, placed: _Placed) -> None:
        """Lags from every placed predecessor (done ones count as finished) to the gate.

        Without overlap the lags run from the predecessor's end; with a transfer lot q from
        pred.setup_end + pred.fixed + q * pred.unit, and the last lot bounds the end.
        """
        for link in placed.op.predecessors:
            pred = self.placed.get(link.operation_id)
            if pred is None:
                continue
            ready = pred.end
            if link.overlap_quantity is not None:
                transfer = self._transfer(pred, link.overlap_quantity)
                if transfer is None:
                    continue  # no option row for an in-progress machine: nothing to check
                ready = pred.setup_end + transfer
                lot = sum(
                    _ticks(link.overlap_quantity * _unit(placed.op, c.machine_id)) * c.presence
                    for c in placed.choices
                )
                self.model.Add(placed.end >= pred.end + lot)
            self.model.Add(placed.gate >= ready + _ticks(link.min_lag))
            if link.max_lag is not None:
                self.model.Add(placed.gate <= ready + _ticks(link.max_lag))

    def _transfer(self, pred: _Placed, quantity: float) -> Any:
        """Ticks from pred.setup_end to its first transfer lot, over its chosen option."""
        terms = []
        for choice in pred.choices:
            option = _option(pred.op, choice.machine_id)
            if option is None:
                return None
            lot = _ticks(option.fixed_seconds + quantity * option.unit_seconds)
            terms.append(lot * choice.presence)
        return sum(terms)

    # batches

    def _add_batches(self) -> None:
        """Leader / member literals on every batch machine (module docstring)."""
        for machine in self.problem.machines:
            if machine.batch is None:
                continue
            groups: dict[tuple[object, ...], list[tuple[_Placed, _Choice]]] = defaultdict(list)
            for placed, choice in self.machine_choices.get(machine.id, []):
                family = placed.op.batch_family
                if family is None:
                    continue
                signature = _setup_signature(placed.op) if machine.id in self.setups else ()
                groups[family, choice.run, signature].append((placed, choice))
            capacity = math.floor(machine.batch.capacity * MICRO + BOUND_SLACK)
            for group in groups.values():
                self._add_batch_group(group, capacity)

    def _add_batch_group(self, group: Sequence[tuple[_Placed, _Choice]], capacity: int) -> None:
        """Each option leads a batch or joins one led by an earlier option of the group."""
        model = self.model
        loads: list[list[Any]] = []
        for k, (placed, choice) in enumerate(group):
            leader = model.NewBoolVar(f"leads_{placed.op.id}_{choice.machine_id}")
            choice.links = [(placed.op.id, leader)]
            for j, (head, head_choice) in enumerate(group[:k]):
                joins = model.NewBoolVar(f"joins_{placed.op.id}_{head.op.id}")
                model.AddImplication(joins, head_choice.active)
                self._share_times(placed, choice, head, head_choice, joins)
                choice.links.append((head.op.id, joins))
                loads[j].append(_quantity(placed.op) * joins)
            model.Add(sum(lit for _, lit in choice.links) == choice.presence)
            choice.active = leader
            choice.block = self._leader_block(placed, choice, leader)
            loads.append([_quantity(placed.op) * leader])
        for (_, choice), load in zip(group, loads, strict=True):
            model.Add(sum(load) <= capacity * choice.active)

    def _share_times(
        self, member: _Placed, choice: _Choice, head: _Placed, head_choice: _Choice, joins: Any
    ) -> None:
        """A member has its leader's start, setup end, end, setup and warm-up."""
        pairs = [
            (member.start, head.start),
            (member.setup_end, head.setup_end),
            (member.end, head.end),
            (choice.setup, head_choice.setup),
            (choice.warm, head_choice.warm),
        ]
        for mine, theirs in pairs:
            if not (isinstance(mine, int) and isinstance(theirs, int)):
                self.model.Add(mine == theirs).OnlyEnforceIf(joins)

    def _leader_block(self, placed: _Placed, choice: _Choice, leader: Any) -> Any:
        """The machine occupation of a batch: the leader's span, present when it leads."""
        name = f"batch_{placed.op.id}_{choice.machine_id}"
        if isinstance(choice.phase, int):
            interval = self.model.NewOptionalFixedSizeIntervalVar
            return interval(placed.start, choice.run, leader, name)
        return self.model.NewOptionalIntervalVar(
            placed.start, choice.phase + choice.run, placed.end, leader, name
        )

    # resources

    def _break_intervals(self, calendar_id: str | None) -> list[Any]:
        if calendar_id is None:
            return []
        gaps = _breaks(self.calendars[calendar_id], self.horizon + 1)
        return [
            self.model.NewFixedSizeIntervalVar(lo, hi - lo, f"break_{calendar_id}_{lo}")
            for lo, hi in gaps
            if hi > lo
        ]

    def _add_machines(self) -> None:
        """No overlap with each other or the calendar's breaks; in-progress work comes first."""
        for machine in self.problem.machines:
            members = self.machine_choices.get(machine.id, [])
            floor = self.machine_floor.get(machine.id)
            for placed, choice in members:
                if floor is not None:
                    self.model.Add(placed.start >= floor).OnlyEnforceIf(choice.presence)
            intervals = [choice.block for _, choice in members]
            self.model.AddNoOverlap(intervals + self._break_intervals(machine.calendar_id))
            if self.sequenced(machine.id) and members:
                self._add_circuit(machine, members)

    def idle_base(self, machine: Machine) -> int:
        """Where the first idle runs from: the in-progress end, else max(now, available_from)."""
        floor = self.machine_floor.get(machine.id)
        if floor is not None:
            return floor
        return max(0, self.at(machine.state.available_from))

    def _add_circuit(self, machine: Machine, members: Sequence[tuple[_Placed, _Choice]]) -> None:
        """Sequence the machine's active options from a depot node 0 (the machine state, D-075).

        An arc into an option fixes its setup and its warm-up; an arc between options also
        orders them. Two zero-length runs at one instant follow the verifier's order
        (operation id).
        """
        model, setups = self.model, self.setups.get(machine.id)
        unused = model.NewBoolVar(f"unused_{machine.id}")
        arcs: list[tuple[int, int, Any]] = [(0, 0, unused)]
        base = self.idle_base(machine)
        for node, (placed, choice) in enumerate(members, start=1):
            model.AddImplication(choice.active, unused.Not())
            arcs.append((node, node, choice.active.Not()))
            first, last = model.NewBoolVar("first"), model.NewBoolVar("last")
            if setups is not None:
                model.Add(choice.setup == setups.first[placed.op.id]).OnlyEnforceIf(first)
            self._add_idle(machine, choice, placed.start - base, first)
            arcs += [(0, node, first), (node, 0, last)]
            self.arcs[machine.id, None, placed.op.id] = first
            self.arcs[machine.id, placed.op.id, None] = last
        nodes = list(enumerate(members, start=1))
        for (i, (a, choice_a)), (j, (b, choice_b)) in permutations(nodes, 2):
            follows = model.NewBoolVar("follows")
            model.Add(b.start >= a.end).OnlyEnforceIf(follows)
            if setups is not None:
                setup = setups.between[a.op.id, b.op.id]
                model.Add(choice_b.setup == setup).OnlyEnforceIf(follows)
            if choice_a.run == 0 and choice_b.run == 0 and a.op.id > b.op.id:
                model.Add(b.end >= a.start + 1).OnlyEnforceIf(follows)
            self._add_idle(machine, choice_b, b.start - a.end, follows)
            arcs.append((i, j, follows))
            self.arcs[machine.id, a.op.id, b.op.id] = follows
        model.AddCircuit(arcs)

    def _add_idle(self, machine: Machine, choice: _Choice, idle: Any, arc: Any) -> None:
        """Restart (D-074) on an arc: warm iff idle passes the threshold (strictly)."""
        rule = machine.restart
        if rule is None:
            return
        threshold = _ticks(rule.idle_threshold_seconds)
        self.model.Add(idle >= threshold + 1).OnlyEnforceIf([arc, choice.warm])
        self.model.Add(idle <= threshold).OnlyEnforceIf([arc, choice.warm.Not()])

    def _add_pools(self) -> None:
        """Capacity per pool, plus a fence keeping members out of the pool's breaks.

        The fence is a cumulative of capacity n over the n member intervals (demand 1 each)
        and the breaks (demand n): members may overlap each other but never a break.
        """
        for pool in self.problem.operator_pools:
            usage = self.pool_usage.get(pool.id, [])
            if usage:
                intervals, demands = zip(*usage, strict=True)
                self.model.AddCumulative(list(intervals), list(demands), pool.size)
            members = self.pool_members.get(pool.id, [])
            breaks = self._break_intervals(pool.calendar_id)
            if members and breaks:
                n = len(members)
                self.model.AddCumulative(members + breaks, [1] * n + [n] * len(breaks), n)


def _unit(op: Operation, machine_id: str) -> float:
    option = next(o for o in op.options if o.machine_id == machine_id)
    return option.unit_seconds


def _quantity(op: Operation) -> int:
    """Quantity in integer millionths of the batch unit, rounded up (never under-counts)."""
    return math.ceil(op.quantity * MICRO - BOUND_SLACK)


# --- objectives -----------------------------------------------------------------------------


class _Unrepresentable(ValueError):
    """A positive objective weight rounds to 0 at `MICRO` resolution (`unsupported_precision`)."""


def _micro(value: float, what: str) -> int:
    """`value` in integer micro-units; a positive value that rounds to 0 is refused."""
    micro = round(value * MICRO)
    if value > 0 and micro == 0:
        raise _Unrepresentable(f"{what} {value!r} rounds to 0 at {MAX_DECIMALS} decimals")
    return micro


@dataclass(frozen=True, slots=True)
class _Linear:
    """value = constant + sum(coefficient * expression) / (denominator * divisor).

    Coefficients are integers; `denominator` is the integer unit (ticks, micro-weights) and
    `divisor` a positive float only robustness needs. Each term carries `magnitude`, a bound
    on |expression| for the overflow guard.
    """

    terms: tuple[tuple[int, Any, int], ...] = ()
    denominator: int = 1
    constant: float = 0.0
    divisor: float = 1.0

    def negated(self) -> _Linear:
        terms = tuple((-c, e, m) for c, e, m in self.terms)
        return _Linear(terms, self.denominator, -self.constant, self.divisor)


def _signed(kind: ObjectiveKind, linear: _Linear) -> _Linear:
    """The linear form to minimize: a maximize-sense value is negated."""
    return linear.negated() if SENSE[kind] == "max" else linear


def _weighted(parts: Sequence[tuple[float, _Linear]]) -> _Linear:
    """sum multiplier * linear over one common integer unit (weighted sum, D-068).

    Each multiplier (weight / scale, over robustness's divisor) is taken in micro-units
    and refused when a positive one rounds to 0; the value of the result is approximate,
    only its minimizer is used.
    """
    quanta = [(_micro(m / linear.divisor, "objective weight"), linear) for m, linear in parts]
    common = math.lcm(*(linear.denominator for _, linear in quanta))
    terms = tuple(
        (q * (common // linear.denominator) * c, e, mag)
        for q, linear in quanta
        for c, e, mag in linear.terms
    )
    constant = math.fsum(m * linear.constant for m, linear in parts)
    return _Linear(terms, common * MICRO, constant)


@dataclass
class _Terms:
    """Linear forms of every `ObjectiveKind` over a built model (SDD Objective formulas).

    Job weights, costs and hour costs enter in integer micro-units (`MICRO`).
    """

    builder: _Builder
    context: SolveContext
    _completion: dict[str, Any] = field(default_factory=dict)
    _linear: dict[ObjectiveKind, _Linear] = field(default_factory=dict)

    def linear(self, kind: ObjectiveKind) -> _Linear:
        """The raw value of `kind` (built once per kind)."""
        if kind not in self._linear:
            forms: dict[ObjectiveKind, Callable[[], _Linear]] = {
                ObjectiveKind.MAKESPAN: self._makespan,
                ObjectiveKind.MAX_LATENESS: self._max_lateness,
                ObjectiveKind.WEIGHTED_COMPLETION: self._weighted_completion,
                ObjectiveKind.TARDY_COUNT: lambda: self._tardy_count(weighted=False),
                ObjectiveKind.WEIGHTED_TARDY_COUNT: lambda: self._tardy_count(weighted=True),
                ObjectiveKind.TOTAL_TARDINESS: lambda: self._tardiness(weighted=False),
                ObjectiveKind.WEIGHTED_TARDINESS: lambda: self._tardiness(weighted=True),
                ObjectiveKind.EARLINESS_TARDINESS: self._earliness_tardiness,
                ObjectiveKind.TOTAL_SETUP: self._total_setup,
                ObjectiveKind.COST: self._cost,
                ObjectiveKind.CHURN: self._churn,
                ObjectiveKind.ROBUSTNESS: self._robustness,
                ObjectiveKind.SOFT_PENALTY: self._soft_penalty,
            }
            self._linear[kind] = forms[kind]()
        return self._linear[kind]

    # helpers

    @property
    def model(self) -> Any:
        return self.builder.model

    @cached_property
    def jobs(self) -> list[Job]:
        """Jobs with a placed operation, in placement order."""
        jobs: dict[str, Job] = {}
        for placed in self.builder.placed.values():
            jobs.setdefault(placed.job.id, placed.job)
        return list(jobs.values())

    @cached_property
    def due_jobs(self) -> list[tuple[Job, float]]:
        return [(job, job.due) for job in self.jobs if job.due is not None]

    @cached_property
    def reach(self) -> int:
        """A bound on |time| and |lateness| ticks in the model."""
        dues = (abs(self.builder.at(due)) for _, due in self.due_jobs)
        return 2 * max([self.builder.horizon, *dues])

    def _weight(self, job: Job) -> int:
        return _micro(_weight(job), f"job {job.id} weight")

    def completion(self, job: Job) -> Any:
        """C_j: the latest end among the job's placed operations (cached)."""
        if job.id not in self._completion:
            ends = [p.end for p in self.builder.placed.values() if p.job.id == job.id]
            completion = self.model.NewIntVar(0, self.builder.horizon, f"completion_{job.id}")
            self.model.AddMaxEquality(completion, ends)
            self._completion[job.id] = completion
        return self._completion[job.id]

    def _lateness(self, job: Job, due: float) -> Any:
        return self.completion(job) - self.builder.at(due)

    def _choices(self) -> Iterator[tuple[_Placed, _Choice]]:
        for placed in self.builder.placed.values():
            for choice in placed.choices:
                yield placed, choice

    # kinds

    def _makespan(self) -> _Linear:
        if not self.jobs:
            return _Linear()
        makespan = self.model.NewIntVar(0, self.builder.horizon, "makespan")
        self.model.AddMaxEquality(makespan, [self.completion(job) for job in self.jobs])
        return _Linear(((1, makespan, self.reach),), SCALE)

    def _max_lateness(self) -> _Linear:
        if not self.due_jobs:
            return _Linear()
        latest = self.model.NewIntVar(-self.reach, self.reach, "max_lateness")
        self.model.AddMaxEquality(latest, [self._lateness(j, due) for j, due in self.due_jobs])
        return _Linear(((1, latest, self.reach),), SCALE)

    def _weighted_completion(self) -> _Linear:
        """sum w_j C_j with C_j measured from now plus the constant sum w_j * now."""
        terms = tuple((self._weight(job), self.completion(job), self.reach) for job in self.jobs)
        constant = math.fsum(_weight(job) * self.builder.problem.now for job in self.jobs)
        return _Linear(terms, SCALE * MICRO, constant)

    def _tardy_count(self, *, weighted: bool) -> _Linear:
        terms = []
        for job, due in self.due_jobs:
            tardy = self.model.NewBoolVar(f"tardy_{job.id}")
            lateness = self._lateness(job, due)
            self.model.Add(lateness >= 1).OnlyEnforceIf(tardy)
            self.model.Add(lateness <= 0).OnlyEnforceIf(tardy.Not())
            terms.append((self._weight(job) if weighted else 1, tardy, 1))
        return _Linear(tuple(terms), MICRO if weighted else 1)

    def _tardiness(self, *, weighted: bool) -> _Linear:
        terms = []
        for job, due in self.due_jobs:
            tardiness = self.model.NewIntVar(0, self.reach, f"tardiness_{job.id}")
            self.model.AddMaxEquality(tardiness, [self._lateness(job, due), 0])
            terms.append((self._weight(job) if weighted else 1, tardiness, self.reach))
        return _Linear(tuple(terms), SCALE * MICRO if weighted else SCALE)

    def _earliness_tardiness(self) -> _Linear:
        terms = []
        for job, due in self.due_jobs:
            deviation = self.model.NewIntVar(0, self.reach, f"deviation_{job.id}")
            self.model.AddAbsEquality(deviation, self._lateness(job, due))
            terms.append((1, deviation, self.reach))
        return _Linear(tuple(terms), SCALE)

    def _robustness(self) -> _Linear:
        """R(S) = 1 - sum w_j (C_j - now) / sum w_j (d_j - now); 0 when the sum is <= 0."""
        now = self.builder.problem.now
        denominator = math.fsum(_weight(job) * (due - now) for job, due in self.due_jobs)
        if denominator <= 0:
            return _Linear()
        terms = tuple(
            (-self._weight(job), self.completion(job), self.reach) for job, _ in self.due_jobs
        )
        return _Linear(terms, SCALE * MICRO, 1.0, denominator)

    def _phase_terms(self, choice: _Choice, coefficient: int) -> list[tuple[int, Any, int]]:
        """coefficient * (setup + warm * warm-up) of one option, as terms over its variables."""
        terms: list[tuple[int, Any, int]] = []
        if not isinstance(choice.setup, int):
            terms.append((coefficient, choice.setup, self.reach))
        if not isinstance(choice.warm, int):
            rule = self.builder.machines[choice.machine_id].restart
            warmup = _ticks(rule.warmup_seconds) if rule is not None else 0
            terms.append((coefficient * warmup, choice.warm, 1))
        return terms

    def _total_setup(self) -> _Linear:
        """sum (setup_end - start): the setup phase, warm-up included (D-074)."""
        terms = [t for _, choice in self._choices() for t in self._phase_terms(choice, 1)]
        return _Linear(tuple(terms), SCALE)

    def _cost(self) -> _Linear:
        """sum (run seconds * hour_cost + setup seconds * setup_hour_cost) / 3600."""
        terms: list[tuple[int, Any, int]] = []
        constant = 0.0
        for _, choice in self._choices():
            machine = self.builder.machines[choice.machine_id]
            hour_cost = _micro(machine.hour_cost, f"machine {machine.id} hour_cost")
            if isinstance(choice.presence, int):
                constant += _seconds(choice.run) * machine.hour_cost / SECONDS_PER_HOUR
                continue
            terms.append((choice.run * hour_cost, choice.presence, 1))
            if not isinstance(choice.phase, int):
                setup_cost = _micro(machine.setup_hour_cost, f"machine {machine.id} setup cost")
                terms += self._phase_terms(choice, setup_cost)
        return _Linear(tuple(terms), SCALE * MICRO * round(SECONDS_PER_HOUR), constant)

    def _soft_penalty(self) -> _Linear:
        terms: list[tuple[int, Any, int]] = []
        constant = 0.0
        for placed, choice in self._choices():
            costs = {o.machine_id: o.preference_cost for o in placed.op.options}
            cost = costs.get(choice.machine_id, 0.0)
            if isinstance(choice.presence, int):
                constant += cost
            else:
                terms.append((_micro(cost, f"{placed.op.id} preference_cost"), choice.presence, 1))
        return _Linear(tuple(terms), MICRO, constant)

    def _churn(self) -> _Linear:
        """Previous operations whose machine changed or whose start moved past the tolerance.

        A pending operation's `changed` literal is only forced on (stay put is optional), which
        is exact for a value that is minimized or bounded from above.
        """
        previous_plan = self.context.previous_plan
        if previous_plan is None:
            return _Linear()
        tolerance = _ticks(self.context.churn_tolerance)
        terms: list[tuple[int, Any, int]] = []
        constant = 0.0
        for previous in previous_plan.assignments:
            placed = self.builder.placed.get(previous.operation_id)
            if placed is None:
                continue
            start = self.builder.at(previous.start)
            if placed.op.status.state == "in_progress":
                moved = abs(start) > tolerance
                constant += float(moved or placed.choices[0].machine_id != previous.machine_id)
                continue
            changed = self._changed(placed, previous.machine_id, start, tolerance)
            if isinstance(changed, int):
                constant += changed
            else:
                terms.append((1, changed, 1))
        return _Linear(tuple(terms), 1, constant)

    def _changed(self, placed: _Placed, machine_id: str, start: int, tolerance: int) -> Any:
        presence = next((c.presence for c in placed.choices if c.machine_id == machine_id), None)
        if presence is None:
            return 1
        changed = self.model.NewBoolVar(f"changed_{placed.op.id}")
        kept = changed.Not()
        self.model.AddImplication(kept, presence)
        self.model.Add(placed.start >= start - tolerance).OnlyEnforceIf(kept)
        self.model.Add(placed.start <= start + tolerance).OnlyEnforceIf(kept)
        return changed


@dataclass(frozen=True, slots=True)
class _IntegerForm:
    """A `_Linear` with coefficients divided by their gcd: value = integer / factor + constant."""

    expression: Any
    factor: float
    constant: float
    has_terms: bool

    def value(self, integer: float) -> float:
        return integer / self.factor + self.constant

    def at_most(self, value: float) -> int:
        """The largest integer whose value is <= `value` (float noise forgiven)."""
        return math.floor((value - self.constant) * self.factor + BOUND_SLACK)


def _integer_form(linear: _Linear) -> _IntegerForm | ScheduleIssue:
    """The reduced integer objective, or `unsupported_magnitude` when it cannot fit int64."""
    terms = [(c, e, m) for c, e, m in linear.terms if c != 0]
    if not terms:
        return _IntegerForm(0, 1.0, linear.constant, has_terms=False)
    divisor = math.gcd(*(c for c, _, _ in terms))
    terms = [(c // divisor, e, m) for c, e, m in terms]
    if sum(abs(c) * m for c, _, m in terms) > MAX_OBJECTIVE:
        message = "the objective's integer range does not fit in int64"
        return ScheduleIssue(code="unsupported_magnitude", path=OBJECTIVE_PATH, message=message)
    expression = sum(c * e for c, e, _ in terms)
    factor = linear.denominator * linear.divisor / divisor
    return _IntegerForm(expression, factor, linear.constant, has_terms=True)


# --- stages ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stage:
    """One solve: minimize `objective`; a lexicographic `level` then bounds it.

    `bound_kind` is set when the objective is exactly signed(kind) (single-kind primary).
    """

    objective: _Linear
    level: LexicographicLevel | None = None
    bound_kind: ObjectiveKind | None = None


@dataclass(frozen=True, slots=True)
class _Plan:
    """The stages of a spec and its hard bounds (`raw value <= upper`)."""

    stages: tuple[_Stage, ...]
    bounds: tuple[tuple[_Linear, float], ...] = ()


def _plan(spec: ObjectiveSpec, terms: _Terms) -> _Plan:
    """Stages for a spec: one per lexicographic level, else one; epsilon bounds as constraints.

    Raises `_Unrepresentable` when a positive weight rounds to 0.
    """
    if isinstance(spec, Lexicographic):
        return _Plan(tuple(_single_stage(level.kind, terms, level) for level in spec.levels))
    if isinstance(spec, EpsilonConstraint):
        bounds = tuple((terms.linear(b.kind), b.upper) for b in spec.bounds)
        return _Plan((_single_stage(spec.primary, terms),), bounds)
    weighted = [term for term in spec.terms if term.weight > 0]
    if len(weighted) == 1:
        return _Plan((_single_stage(weighted[0].kind, terms),))
    parts = [
        (term.weight / (term.scale or 1.0), _signed(term.kind, terms.linear(term.kind)))
        for term in weighted
    ]
    return _Plan((_Stage(_weighted(parts)),))


def _single_stage(
    kind: ObjectiveKind, terms: _Terms, level: LexicographicLevel | None = None
) -> _Stage:
    return _Stage(_signed(kind, terms.linear(kind)), level, kind)


# --- warm start and horizon cap -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ModelInput:
    """Everything a model build reads besides the horizon."""

    problem: Problem
    scheduled: Sequence[tuple[Job, Operation]]
    setups: Mapping[str, _MachineSetups]
    objective: ObjectiveSpec
    context: SolveContext
    hint: Schedule | None


@dataclass(frozen=True, slots=True)
class _Hint:
    """The plan the model is hinted with; `values` are its objective values when it verifies
    (always for a constructed hint; a warm start may not), else None."""

    schedule: Schedule
    values: Values | None


def _hint(
    problem: Problem, objective: ObjectiveSpec, context: SolveContext, stop_at: float
) -> _Hint | None:
    """`context.warm_start`, else the best verified plan under the objective among the
    baseline and the `hint_solvers` methods, each run within the deadline (T-139)."""
    if context.warm_start is not None:
        return _checked_hint(problem, context, context.warm_start)
    best: _Hint | None = None
    scorer: Scorer | None = None
    for solver in _hint_solvers(context):
        remaining = stop_at - time.monotonic()
        if remaining <= 0:
            break
        schedule = solver.solve(
            problem,
            objective=objective,
            budget_seconds=remaining,
            context=replace(context, deadline=stop_at, params={}),
        )
        if schedule.status not in ("OPTIMAL", "FEASIBLE"):
            continue
        candidate = _checked_hint(problem, context, schedule)
        if candidate.values is None:
            continue
        if best is None or best.values is None:
            best, scorer = candidate, Scorer(objective, candidate.values)
        elif scorer is not None and scorer.better(candidate.values, best.values):
            best = candidate
    return best


def _hint_solvers(context: SolveContext) -> list[Solver]:
    """The baseline, then each registered `hint_solvers` name (comma-separated, default
    `atcs`), looked up by name (D-069). A plan a method gets wrong (say, a feature it does
    not model) fails verification and is not a candidate."""
    raw = context.params.get(HINT_SOLVERS_PARAM, DEFAULT_HINT_SOLVERS)
    if not isinstance(raw, str):
        raise ValueError(f"{HINT_SOLVERS_PARAM} must be a comma-separated string of names")
    solvers: list[Solver] = [BaselineSolver()]
    for name in (part.strip() for part in raw.split(",")):
        solver = _named_hint_solver(name)
        if solver is not None:
            solvers.append(solver)
    return solvers


def _named_hint_solver(name: str) -> Solver | None:
    """The registered solver `name`, or None (logged) when it cannot give a hint."""
    if not name or name == SOLVER_NAME:
        return None
    if not is_registered(name):
        log.debug("hint solver %r is not registered; skipped", name)
        return None
    try:
        return get_solver(name)
    except OptionalDependencyError:
        log.debug("hint solver %r misses an optional dependency; skipped", name)
        return None


def _checked_hint(problem: Problem, context: SolveContext, schedule: Schedule) -> _Hint:
    """The schedule with its objective values when it verifies, else with None."""
    compiled = CompiledProblem(problem=problem, profile=SchedulingProfile())
    unvalued = schedule.model_copy(update={"primary_value": None})
    if not verify(compiled, (), unvalued).valid:
        return _Hint(schedule, None)
    return _Hint(schedule, _values(problem, schedule.assignments, context))


def _makespan_only(spec: ObjectiveSpec) -> bool:
    """True when the spec optimizes makespan alone: nothing can gain from finishing later."""
    if isinstance(spec, Lexicographic):
        return len(spec.levels) == 1 and spec.levels[0].kind == ObjectiveKind.MAKESPAN
    if isinstance(spec, EpsilonConstraint):
        return False
    weighted = [term for term in spec.terms if term.weight > 0]
    return len(weighted) == 1 and weighted[0].kind == ObjectiveKind.MAKESPAN


def _makespan_cap(problem: Problem, objective: ObjectiveSpec, hint: _Hint | None) -> int | None:
    """The hint's last end in absolute ticks, when the objective is makespan alone and the
    hint verifies; None otherwise. An optimal plan never ends later than a verified one."""
    if hint is None or hint.values is None or not _makespan_only(objective):
        return None
    ends = (_ticks(a.end) for a in hint.schedule.assignments)
    return max(ends, default=_ticks(problem.now))


def _hint_wins(
    outcome: _Outcome,
    hint: _Hint | None,
    problem: Problem,
    objective: ObjectiveSpec,
    context: SolveContext,
) -> bool:
    """True when the verified hint should be the answer: CP-SAT ran out of time without a
    plan, or its unproven plan ranks behind the hint under the objective (T-139)."""
    if hint is None or hint.values is None or outcome.status == "OPTIMAL":
        return False
    if outcome.status == "UNKNOWN":
        return [issue.code for issue in outcome.issues] == ["time_limit"]
    if outcome.status != "FEASIBLE":
        return False
    found = _values(problem, outcome.assignments, context)
    return Scorer(objective, hint.values).better(hint.values, found)


def _add_hints(builder: _Builder, hint: Schedule) -> None:
    """Hint option presences, then each machine's sequence (D-075, T-138).

    Starts are not hinted: a float plan's times rounded to ticks disagree by a tick with
    the model's sums of rounded setups and runs, which made the whole hint infeasible and
    left CP-SAT without a plan on short budgets; presences, arcs, warm-ups and batch links
    fix the plan's structure and propagation finds its starts.
    """
    model = builder.model
    by_op = {a.operation_id: a for a in hint.assignments}
    for placed in builder.placed.values():
        assignment = by_op.get(placed.op.id)
        if assignment is None or placed.op.status.state == "in_progress":
            continue
        for choice in placed.choices:
            model.AddHint(choice.presence, int(choice.machine_id == assignment.machine_id))
    for machine_id, members in builder.machine_choices.items():
        chosen = [
            (placed, choice, by_op[placed.op.id])
            for placed, choice in members
            if placed.op.id in by_op and by_op[placed.op.id].machine_id == machine_id
        ]
        blocks = _hint_batches(model, chosen)
        if builder.sequenced(machine_id):
            _hint_circuit(builder, builder.machines[machine_id], blocks)


_HintBlock = list[tuple[_Placed, _Choice, Assignment]]


def _hint_batches(
    model: Any, chosen: Sequence[tuple[_Placed, _Choice, Assignment]]
) -> list[_HintBlock]:
    """Group the hint's assignments into machine blocks; hint batch leader and member links.

    The leader of a hinted batch is its first member in model order, as in the model.
    """
    blocks: dict[str, _HintBlock] = {}
    for placed, choice, assignment in chosen:
        key = assignment.batch_id or f"op:{placed.op.id}"
        block = blocks.setdefault(key, [])
        leader_id = block[0][0].op.id if block else placed.op.id
        link = next((lit for head, lit in choice.links if head == leader_id), None)
        if link is not None:
            model.AddHint(link, 1)
        block.append((placed, choice, assignment))
    return sorted(blocks.values(), key=lambda b: (b[0][2].start, b[0][2].end, b[0][0].op.id))


def _hint_circuit(builder: _Builder, machine: Machine, blocks: Sequence[_HintBlock]) -> None:
    """Hint the circuit arcs along the hinted order and each block's warm-up literals."""
    model, rule = builder.model, machine.restart
    previous: str | None = None
    idle_since = builder.idle_base(machine)
    for block in blocks:
        head, _, assignment = block[0]
        arc = builder.arcs.get((machine.id, previous, head.op.id))
        if arc is not None:
            model.AddHint(arc, 1)
        start = builder.at(assignment.start)
        if rule is not None:
            warm = int(start - idle_since > _ticks(rule.idle_threshold_seconds))
            for _, choice, _ in block:
                model.AddHint(choice.warm, warm)
        previous, idle_since = head.op.id, builder.at(assignment.end)
    last = builder.arcs.get((machine.id, previous, None))
    if previous is not None and last is not None:
        model.AddHint(last, 1)


# --- solver -----------------------------------------------------------------------------------


@dataclass
class _Outcome:
    """What the stages produced: status, the last plan, the bound and named issues."""

    status: Status
    assignments: list[Assignment] = field(default_factory=list)
    best_bound: float | None = None
    issues: list[ScheduleIssue] = field(default_factory=list)


class ORToolsSolver:
    """CP-SAT solver: optimal or best-found plans for every objective spec (COMP-016).

    `workers` and `seed` are passed to CP-SAT; with one worker a solve is deterministic for
    a given input and budget that is not hit.
    """

    def __init__(self, *, workers: int = 1, seed: int = 0) -> None:
        if workers < 1 or seed < 0:
            raise ValueError("workers must be positive and seed must be non-negative")
        self._workers = workers
        self._seed = seed
        try:
            self._cp_model: Any = importlib.import_module("ortools.sat.python.cp_model")
        except ImportError as exc:
            raise OptionalDependencyError(
                "install the 'scheduling' extra (ortools) to use ORToolsSolver"
            ) from exc

    @property
    def name(self) -> str:
        """The backend name surfaces report: `ortools`."""
        return SOLVER_NAME

    @property
    def capabilities(self) -> frozenset[Feature]:
        """Features CP-SAT models (D-067, D-075); `pause` stays refused."""
        return CAPABILITIES

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        """Optimize `objective` within the budget and `context.deadline`, whichever is first.

        UNKNOWN with `unsupported_feature`, `unsupported_precision`, `model_too_large` or
        `unsupported_magnitude` when refused (in that order, all before a model is built);
        INFEASIBLE with `infeasible`; UNKNOWN with `time_limit` when time ran out before any
        plan. A model CP-SAT finds invalid is a bug and raises.
        """
        started = time.perf_counter()
        scheduled = _scheduled(problem)
        refusals = (
            _feature_refusals(problem)
            or _precision_refusals(problem, context)
            or _size_refusals(problem, scheduled)
        )
        if refusals:
            return _schedule("UNKNOWN", started, issues=refusals)
        setups = _setup_tables(problem, scheduled)
        horizon = _horizon(problem, scheduled, setups, context.previous_plan)
        refusals = _magnitude_refusals(problem, context, horizon, setups)
        if refusals:
            return _schedule("UNKNOWN", started, issues=refusals)
        stop_at = min(time.monotonic() + budget_seconds, context.deadline)
        if stop_at <= time.monotonic():
            return _schedule("UNKNOWN", started, issues=[_time_limit()])
        hint = _hint(problem, objective, context, stop_at)
        cap = _makespan_cap(problem, objective, hint)
        capped = horizon if cap is None else min(horizon, cap)
        hinted = None if hint is None else hint.schedule
        model = _ModelInput(problem, scheduled, setups, objective, context, hinted)
        outcome = self._optimize(model, capped, stop_at)
        if outcome.status == "INFEASIBLE" and capped < horizon:
            outcome = self._optimize(model, horizon, stop_at)  # tick rounding broke the cap
        if hint is not None and _hint_wins(outcome, hint, problem, objective, context):
            outcome = _Outcome(
                "FEASIBLE", list(hint.schedule.assignments), best_bound=outcome.best_bound
            )
        if outcome.status not in ("OPTIMAL", "FEASIBLE"):
            return _schedule(
                outcome.status, started, issues=outcome.issues, best_bound=outcome.best_bound
            )
        value = _values(problem, outcome.assignments, context)[primary_kind(objective)]
        return _schedule(
            outcome.status,
            started,
            assignments=outcome.assignments,
            primary_value=value,
            best_bound=outcome.best_bound,
        )

    def _optimize(self, model: _ModelInput, horizon: int, stop_at: float) -> _Outcome:
        """Build the model with this horizon (absolute ticks), hint it and run the stages."""
        origin = _ticks(model.problem.now)
        builder = _Builder(self._cp_model, model.problem, horizon - origin, origin, model.setups)
        builder.build(model.scheduled)
        try:
            plan = _plan(model.objective, _Terms(builder, model.context))
        except _Unrepresentable as error:
            issue = ScheduleIssue(
                code="unsupported_precision", path=OBJECTIVE_PATH, message=str(error)
            )
            return _Outcome("UNKNOWN", issues=[issue])
        if model.hint is not None:
            _add_hints(builder, model.hint)
        return self._run(builder, plan, stop_at, hinted=model.hint is not None)

    def _complete_hint(self, builder: _Builder, stop_at: float) -> list[Assignment]:
        """Turn the structural hint into a complete one; its plan, or [] (T-138).

        A partial hint is dropped by CP-SAT after a few conflicts, which left 40-job models
        without any plan in 5 s. One solve with the hinted literals fixed finds the hint's
        starts in milliseconds; its full assignment is then the hint the stages start from,
        and its plan the answer when the first stage finds none in time (a 200-operation
        model can spend the whole budget in presolve).
        """
        limit = HINT_BUDGET_SHARE * (stop_at - time.monotonic())
        if limit <= 0:
            return []
        cp_solver = self._cp_solver(limit)
        cp_solver.parameters.fix_variables_to_their_hinted_value = True
        cp_solver.parameters.max_deterministic_time = HINT_DETERMINISTIC_TIME
        status = self._status(cp_solver.Solve(builder.model), builder.model)
        if status not in ("OPTIMAL", "FEASIBLE"):
            return []
        _hint_solution(builder.model, cp_solver)
        return _assignments(cp_solver, builder)

    def _cp_solver(self, limit: float) -> Any:
        """A CP-SAT solver with this time limit and the adapter's workers, seed and LP level."""
        cp_solver = self._cp_model.CpSolver()
        cp_solver.parameters.max_time_in_seconds = limit
        cp_solver.parameters.num_workers = self._workers
        cp_solver.parameters.random_seed = self._seed
        cp_solver.parameters.linearization_level = LINEARIZATION_LEVEL
        return cp_solver

    def _run(self, builder: _Builder, plan: _Plan, stop_at: float, *, hinted: bool) -> _Outcome:
        """Solve the stages in order; a later stage keeps earlier levels within tolerance.

        A hinted model is first completed under the hard bounds (`_complete_hint`); that plan
        is kept as the answer if the first stage finds nothing.
        """
        forms = [_integer_form(stage.objective) for stage in plan.stages]
        bounds = [(_integer_form(linear), upper) for linear, upper in plan.bounds]
        refused = [f for f in (*forms, *(f for f, _ in bounds)) if isinstance(f, ScheduleIssue)]
        if refused:
            return _Outcome("UNKNOWN", issues=refused[:1])
        for form, upper in bounds:
            if isinstance(form, _IntegerForm):
                builder.model.Add(form.expression <= form.at_most(upper))
        stages = [
            (stage, form)
            for stage, form in zip(plan.stages, forms, strict=True)
            if isinstance(form, _IntegerForm)
        ]
        outcome = _Outcome("OPTIMAL")
        if hinted:
            outcome.assignments = self._complete_hint(builder, stop_at)
        for k, (stage, form) in enumerate(stages):
            limit = (stop_at - time.monotonic()) / (len(stages) - k)
            if limit <= 0:
                outcome.status = "FEASIBLE" if outcome.assignments else "UNKNOWN"
                outcome.issues = [] if outcome.assignments else [_time_limit()]
                break
            if not self._solve_stage(builder, stage, form, limit, outcome, first=k == 0):
                break
        return outcome

    def _solve_stage(
        self,
        builder: _Builder,
        stage: _Stage,
        form: _IntegerForm,
        limit: float,
        outcome: _Outcome,
        *,
        first: bool,
    ) -> bool:
        """One CP-SAT solve; records the plan and bounds the level. False stops the stages."""
        model = builder.model
        model.Minimize(form.expression)
        cp_solver = self._cp_solver(limit)
        status = self._status(cp_solver.Solve(model), model)
        if first and stage.bound_kind is not None:
            outcome.best_bound = _bound(cp_solver, stage.bound_kind, form)
        if status == "INFEASIBLE":
            if not first:
                raise RuntimeError("a level constraint excluded the incumbent plan")
            outcome.status = "INFEASIBLE"
            outcome.issues = [_infeasible()]
            return False
        if status == "UNKNOWN":
            outcome.status = "FEASIBLE" if outcome.assignments else "UNKNOWN"
            if not outcome.assignments:
                outcome.issues = [_time_limit()]
            elif first:
                outcome.best_bound = None  # no search ran: CP-SAT's bound is not proven
            return False
        if status == "FEASIBLE":
            outcome.status = "FEASIBLE"
        outcome.assignments = _assignments(cp_solver, builder)
        if stage.level is not None:
            _bound_level(model, stage.level, form, cp_solver.ObjectiveValue())
        _hint_solution(model, cp_solver)
        return True

    def _status(self, value: int, model: Any) -> Status:
        names: dict[int, Status] = {
            self._cp_model.OPTIMAL: "OPTIMAL",
            self._cp_model.FEASIBLE: "FEASIBLE",
            self._cp_model.INFEASIBLE: "INFEASIBLE",
            self._cp_model.UNKNOWN: "UNKNOWN",
        }
        if value not in names:
            raise RuntimeError(f"CP-SAT rejected the model: {model.Validate()}")
        return names[value]


def _bound(cp_solver: Any, kind: ObjectiveKind, form: _IntegerForm) -> float:
    """The proven bound on the kind's raw value (an upper bound for robustness)."""
    signed_bound = form.value(cp_solver.BestObjectiveBound()) if form.has_terms else form.constant
    return -signed_bound if SENSE[kind] == "max" else signed_bound


def _bound_level(model: Any, level: LexicographicLevel, form: _IntegerForm, found: float) -> None:
    """Later stages keep this level within its tolerance of the value found."""
    if not form.has_terms:
        return
    best = form.value(found)
    model.Add(form.expression <= form.at_most(best + level_tolerance(level, best)))


def _hint_solution(model: Any, cp_solver: Any) -> None:
    """Start the next stage from this stage's plan, which still satisfies every level."""
    model.ClearHints()
    for index, value in enumerate(cp_solver.ResponseProto().solution):
        model.AddHint(model.GetIntVarFromProtoIndex(index), value)


def _infeasible() -> ScheduleIssue:
    return ScheduleIssue(
        code="infeasible", path="problem", message="no schedule satisfies every constraint"
    )


def _time_limit() -> ScheduleIssue:
    message = "no schedule found within the time budget"
    return ScheduleIssue(code="time_limit", path="budget_seconds", message=message)


def _assignments(cp_solver: Any, builder: _Builder) -> list[Assignment]:
    """The plan of the solver's current solution, with batch ids."""
    batch_ids = _batch_ids(cp_solver, builder)
    return [
        _assignment(cp_solver, p, builder.origin, batch_ids.get(p.op.id))
        for p in builder.placed.values()
    ]


def _assignment(cp_solver: Any, placed: _Placed, origin: int, batch_id: str | None) -> Assignment:
    """Model ticks after `origin` back to absolute seconds."""
    machine_id = next(c.machine_id for c in placed.choices if cp_solver.Value(c.presence))
    return Assignment(
        operation_id=placed.op.id,
        machine_id=machine_id,
        start=_seconds(origin + cp_solver.Value(placed.start)),
        setup_end=_seconds(origin + cp_solver.Value(placed.setup_end)),
        end=_seconds(origin + cp_solver.Value(placed.end)),
        batch_id=batch_id,
    )


def _batch_ids(cp_solver: Any, builder: _Builder) -> dict[str, str]:
    """Batch id per batched operation, `{machine}#b{n}` with n counting leaders in model order.

    A member always joins an earlier leader, so every leader is numbered before its members.
    """
    numbered: dict[str, str] = {}
    opened: Counter[str] = Counter()
    batch_of: dict[str, str] = {}
    for placed in builder.placed.values():
        for choice in placed.choices:
            leader_id = next((h for h, lit in choice.links if cp_solver.Value(lit)), None)
            if leader_id is None:
                continue
            if leader_id == placed.op.id:
                opened[choice.machine_id] += 1
                numbered[leader_id] = f"{choice.machine_id}#b{opened[choice.machine_id]}"
            batch_of[placed.op.id] = numbered[leader_id]
    return batch_of


def _values(
    problem: Problem, assignments: Sequence[Assignment], context: SolveContext
) -> dict[ObjectiveKind, float]:
    """Every objective value of the plan, by `objectives.evaluate` (the verifier's arithmetic)."""
    zone = (
        FrozenZone(until=problem.now, tolerance=context.churn_tolerance)
        if context.churn_tolerance > 0
        else None
    )
    compiled = CompiledProblem(problem=problem, profile=SchedulingProfile(frozen_zone=zone))
    schedule = Schedule(
        status="FEASIBLE", assignments=tuple(assignments), solver=SOLVER_NAME, elapsed_seconds=0.0
    )
    previous_plan: PreviousPlan | None = context.previous_plan
    return evaluate(compiled, schedule, previous_plan)


def _schedule(
    status: Status,
    started: float,
    *,
    issues: Sequence[ScheduleIssue] = (),
    assignments: Sequence[Assignment] = (),
    primary_value: float | None = None,
    best_bound: float | None = None,
) -> Schedule:
    return Schedule(
        status=status,
        assignments=tuple(assignments),
        primary_value=primary_value,
        best_bound=best_bound,
        solver=SOLVER_NAME,
        elapsed_seconds=time.perf_counter() - started,
        issues=tuple(issues),
    )


SOLVERS = (
    SolverSpec(
        name=SOLVER_NAME,
        family="exact",
        factory=ORToolsSolver,
        deterministic=True,
        capabilities=CAPABILITIES,
    ),
)
"""Registry entries (D-069). `deterministic` holds for the default single worker."""
