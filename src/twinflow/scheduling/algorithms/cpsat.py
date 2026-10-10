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
  bounds its start by the previous end;
- the material gate is `start`, or `setup_end` for a detached setup: readiness and every
  predecessor lag bound the gate, pins and machine availability bound the start;
- `AddCumulative` per operator pool over each need's phase (setup, run or both) and, for
  a pool with a calendar, a "fence" cumulative that keeps every operation drawing on the
  pool out of the pool's breaks (TF-002 DEV-6);
- readiness, pins and the horizon as bounds; in-progress operations fixed at `now`.

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
import math
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from itertools import permutations
from typing import Any, Literal

from twinflow.scheduling.kernel.index import DEFAULT_CLASS_WEIGHTS
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
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature, OptionalDependencyError, SolveContext
from twinflow.scheduling.setups import SetupTable

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
    }
)

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


def _run_ticks(op: Operation, machine_id: str) -> int:
    option = next(o for o in op.options if o.machine_id == machine_id)
    return _ticks(option.fixed_seconds + op.quantity * option.unit_seconds)


def _in_progress_machine(op: Operation) -> str:
    return op.status.machine_id or op.options[0].machine_id


# --- refusals -------------------------------------------------------------------------------


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
    longest option with its largest setup plus every min lag. Used when
    `problem.horizon_end` is not set.
    """
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
    """One option of a placed operation: its machine, presence, run, setup and occupation."""

    machine_id: str
    presence: Any
    run: int
    setup: Any
    interval: Any


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
        self._add_machines()
        self._add_pools()

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
        choice = _Choice(machine_id, 1, remaining, 0, interval)
        self.placed[op.id] = _Placed(job, op, 0, 0, 0, remaining, [choice])

    def _add_pending(self, job: Job, op: Operation) -> None:
        """Start, options and end; a setup end and an end variable only with setups."""
        start = self.model.NewIntVar(0, self.horizon, f"start_{op.id}")
        machine_ids = _machine_ids(op)
        has_setup = any(m in self.setups for m in machine_ids)
        setup_end, end = start, None
        if has_setup:
            setup_end = self.model.NewIntVar(0, self.horizon, f"setup_end_{op.id}")
            end = self.model.NewIntVar(0, self.horizon, f"end_{op.id}")
        gate = setup_end if op.setup_detached else start
        placed = _Placed(job, op, start, setup_end, gate, end, [])
        placed.choices = [self._add_option(placed, machine_id) for machine_id in machine_ids]
        choices = placed.choices
        self.model.AddExactlyOne(c.presence for c in choices)
        runs = sum(c.run * c.presence for c in choices)
        if has_setup:
            self.model.Add(setup_end == start + sum(c.setup for c in choices))
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
        """The option's presence, setup, occupation, machine availability and operator needs."""
        op, start, end = placed.op, placed.start, placed.end
        presence = self.model.NewBoolVar(f"on_{op.id}_{machine_id}")
        run = _run_ticks(op, machine_id)
        setups = self.setups.get(machine_id)
        if setups is None:
            setup: Any = 0
            interval = self.model.NewOptionalFixedSizeIntervalVar(
                start, run, presence, f"run_{op.id}_{machine_id}"
            )
        else:
            setup = self.model.NewIntVar(0, setups.most[op.id], f"setup_{op.id}_{machine_id}")
            self.model.Add(setup == 0).OnlyEnforceIf(presence.Not())
            interval = self.model.NewOptionalIntervalVar(
                start, setup + run, end, presence, f"busy_{op.id}_{machine_id}"
            )
        available_from = self.machines[machine_id].state.available_from
        self.model.Add(start >= self.at(available_from)).OnlyEnforceIf(presence)
        choice = _Choice(machine_id, presence, run, setup, interval)
        self.machine_choices[machine_id].append((placed, choice))
        self._add_needs(placed, choice)
        if run == 0:
            self._add_zero_length_starts(placed, choice)
        return choice

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
        if phase == "run" and isinstance(choice.setup, int):
            return choice.interval
        if phase == "run":
            return self.model.NewOptionalFixedSizeIntervalVar(
                placed.setup_end, choice.run, choice.presence, name
            )
        if isinstance(choice.setup, int):
            return None
        return self.model.NewOptionalIntervalVar(
            placed.start, choice.setup, placed.setup_end, choice.presence, name
        )

    def _add_zero_length_starts(self, placed: _Placed, choice: _Choice) -> None:
        """An empty span must start at a working instant of the machine and pool calendars.

        `AddNoOverlap` already keeps it out of a break's inside; this also rejects the break's
        first instant, which is not working either. With setups the rule applies only when
        the setup is 0 too, read by a literal tied to the setup variable.
        """
        calendar_ids = [self.machines[choice.machine_id].calendar_id]
        calendar_ids += [self.pool_calendars[n.pool_id] for n in placed.op.operator_needs]
        calendars = [self.calendars[c] for c in calendar_ids if c is not None]
        if not calendars:
            return
        enforced = [choice.presence]
        if not isinstance(choice.setup, int):
            empty = self.model.NewBoolVar(f"empty_{placed.op.id}_{choice.machine_id}")
            self.model.Add(choice.setup == 0).OnlyEnforceIf(empty)
            self.model.Add(choice.setup >= 1).OnlyEnforceIf(empty.Not())
            enforced.append(empty)
        for intervals in calendars:
            working = [[lo, hi - 1] for lo, hi in intervals if hi > lo]
            domain = self.cp_model.Domain.FromIntervals(working)
            self.model.AddLinearExpressionInDomain(placed.start, domain).OnlyEnforceIf(enforced)

    def _add_precedence(self, placed: _Placed) -> None:
        """End-to-gate lags from every placed predecessor (done ones count as finished)."""
        for link in placed.op.predecessors:
            pred = self.placed.get(link.operation_id)
            if pred is None:
                continue
            self.model.Add(placed.gate >= pred.end + _ticks(link.min_lag))
            if link.max_lag is not None:
                self.model.Add(placed.gate <= pred.end + _ticks(link.max_lag))

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
            intervals = [choice.interval for _, choice in members]
            self.model.AddNoOverlap(intervals + self._break_intervals(machine.calendar_id))
            if machine.id in self.setups and members:
                self._add_circuit(self.setups[machine.id], members)

    def _add_circuit(
        self, setups: _MachineSetups, members: Sequence[tuple[_Placed, _Choice]]
    ) -> None:
        """Sequence the machine's options from a depot node 0 (the machine state, D-075).

        An arc into an option fixes its setup; an arc between options also orders them.
        Two zero-length runs at one instant follow the verifier's order (operation id).
        """
        model = self.model
        empty = model.NewBoolVar("idle")
        arcs: list[tuple[int, int, Any]] = [(0, 0, empty)]
        for node, (placed, choice) in enumerate(members, start=1):
            model.AddImplication(choice.presence, empty.Not())
            arcs.append((node, node, choice.presence.Not()))
            first, last = model.NewBoolVar("first"), model.NewBoolVar("last")
            model.Add(choice.setup == setups.first[placed.op.id]).OnlyEnforceIf(first)
            arcs += [(0, node, first), (node, 0, last)]
        nodes = list(enumerate(members, start=1))
        for (i, (a, choice_a)), (j, (b, choice_b)) in permutations(nodes, 2):
            follows = model.NewBoolVar("follows")
            model.Add(b.start >= a.end).OnlyEnforceIf(follows)
            setup = setups.between[a.op.id, b.op.id]
            model.Add(choice_b.setup == setup).OnlyEnforceIf(follows)
            if choice_a.run == 0 and choice_b.run == 0 and a.op.id > b.op.id:
                model.Add(b.end >= a.start + 1).OnlyEnforceIf(follows)
            arcs.append((i, j, follows))
        model.AddCircuit(arcs)

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

    def _total_setup(self) -> _Linear:
        terms = tuple(
            (1, choice.setup, self.reach)
            for _, choice in self._choices()
            if not isinstance(choice.setup, int)
        )
        return _Linear(terms, SCALE)

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
            if not isinstance(choice.setup, int):
                setup_cost = _micro(machine.setup_hour_cost, f"machine {machine.id} setup cost")
                terms.append((setup_cost, choice.setup, self.reach))
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

        UNKNOWN with `unsupported_precision` or `unsupported_magnitude` when refused;
        INFEASIBLE with `infeasible`; UNKNOWN with `time_limit` when time ran out before
        any plan. A model CP-SAT finds invalid is a bug and raises.
        """
        started = time.perf_counter()
        refusals = _precision_refusals(problem, context)
        if refusals:
            return _schedule("UNKNOWN", started, issues=refusals)
        scheduled = _scheduled(problem)
        setups = _setup_tables(problem, scheduled)
        horizon = _horizon(problem, scheduled, setups, context.previous_plan)
        refusals = _magnitude_refusals(problem, context, horizon, setups)
        if refusals:
            return _schedule("UNKNOWN", started, issues=refusals)
        stop_at = min(time.monotonic() + budget_seconds, context.deadline)
        if stop_at <= time.monotonic():
            return _schedule("UNKNOWN", started, issues=[_time_limit()])
        origin = _ticks(problem.now)
        builder = _Builder(self._cp_model, problem, horizon - origin, origin, setups)
        builder.build(scheduled)
        try:
            plan = _plan(objective, _Terms(builder, context))
        except _Unrepresentable as error:
            issue = ScheduleIssue(
                code="unsupported_precision", path=OBJECTIVE_PATH, message=str(error)
            )
            return _schedule("UNKNOWN", started, issues=[issue])
        outcome = self._run(builder, plan, stop_at)
        if outcome.status not in ("OPTIMAL", "FEASIBLE"):
            return _schedule(
                outcome.status, started, issues=outcome.issues, best_bound=outcome.best_bound
            )
        value = _primary_value(problem, objective, outcome.assignments, context)
        return _schedule(
            outcome.status,
            started,
            assignments=outcome.assignments,
            primary_value=value,
            best_bound=outcome.best_bound,
        )

    def _run(self, builder: _Builder, plan: _Plan, stop_at: float) -> _Outcome:
        """Solve the stages in order; a later stage keeps earlier levels within tolerance."""
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
        cp_solver = self._cp_model.CpSolver()
        cp_solver.parameters.max_time_in_seconds = limit
        cp_solver.parameters.num_workers = self._workers
        cp_solver.parameters.random_seed = self._seed
        cp_solver.parameters.linearization_level = LINEARIZATION_LEVEL
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
            if first:
                outcome.status = "UNKNOWN"
                outcome.issues = [_time_limit()]
            else:
                outcome.status = "FEASIBLE"
            return False
        if status == "FEASIBLE":
            outcome.status = "FEASIBLE"
        outcome.assignments = [
            _assignment(cp_solver, p, builder.origin) for p in builder.placed.values()
        ]
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


def _assignment(cp_solver: Any, placed: _Placed, origin: int) -> Assignment:
    """Model ticks after `origin` back to absolute seconds."""
    machine_id = next(c.machine_id for c in placed.choices if cp_solver.Value(c.presence))
    return Assignment(
        operation_id=placed.op.id,
        machine_id=machine_id,
        start=_seconds(origin + cp_solver.Value(placed.start)),
        setup_end=_seconds(origin + cp_solver.Value(placed.setup_end)),
        end=_seconds(origin + cp_solver.Value(placed.end)),
    )


def _primary_value(
    problem: Problem,
    objective: ObjectiveSpec,
    assignments: Sequence[Assignment],
    context: SolveContext,
) -> float:
    """The primary objective of the plan, by `objectives.evaluate` (the verifier's arithmetic)."""
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
    return evaluate(compiled, schedule, previous_plan)[primary_kind(objective)]


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
