"""Optional CP-SAT solver behind the `Solver` seam (COMP-010, REQ-076, D-067, D-068).

`ORToolsSolver` translates a compiled `Problem` into one CP-SAT model:

- one optional fixed-size interval per (operation, option) with that option's run seconds
  and `AddExactlyOne` over the operation's option presences; a pinned machine keeps only
  its own option;
- `AddNoOverlap` per machine over its option intervals plus the fixed break intervals of
  its calendar (the gaps between working intervals, before the first and after the last),
  which enforces `forbid` with a model size linear in calendar intervals (D-065);
- `AddCumulative` per operator pool (demand = need quantity, capacity = pool size) and,
  for a pool with a calendar, a second "fence" cumulative that keeps every operation
  drawing on the pool, whatever its phase, out of the pool's breaks;
- precedence `succ.start >= pred.end + min_lag` and `<= pred.end + max_lag`;
- readiness, pins and the horizon as bounds; in-progress operations fixed at `now`.

Setups are refused by `solve()` (D-067), so `setup_end == start` and the material gate
equals `start`. Times are integers at `SCALE` ticks per second; inputs with more than six
decimals are refused (`unsupported_precision`) instead of silently rounded, and inputs
whose ticks (a time, a run, the horizon) pass `MAX_TICKS` are refused
(`unsupported_magnitude`) before a model is built. A run of
`fixed + quantity * unit` is rounded to the nearest tick (error <= 0.5 microseconds,
inside the verifier's 1e-6 tolerance). A zero-length run occupies one tick on its machine
and pool so CP-SAT cannot place it inside another interval or a break, which the verifier
would reject.

ortools is an optional extra: it is imported when the solver is built, and its absence
raises `OptionalDependencyError`.
"""

from __future__ import annotations

import importlib
import time
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from twinflow.scheduling.model import (
    Assignment,
    Interval,
    Job,
    Machine,
    ObjectiveKind,
    ObjectiveSpec,
    Operation,
    OperatorNeed,
    Problem,
    Schedule,
    ScheduleIssue,
)
from twinflow.scheduling.objectives import primary_kind
from twinflow.scheduling.solver import Feature

SCALE = 1_000_000
"""Ticks per second: CP-SAT works on integers, times are float seconds."""

MAX_DECIMALS = 6
MAX_TICKS = 2**52
"""Largest tick magnitude a model value may have (about 142 years of seconds). CP-SAT
domains must stay inside +-int64max / 2, and expressions add a few such values (an end
plus a lag, the objective's +-2 * reach), so 2**52 leaves ample headroom."""
SOLVER_NAME = "ortools"
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
    }
)
OPTIMIZED_OBJECTIVES = frozenset({ObjectiveKind.MAKESPAN, ObjectiveKind.MAX_LATENESS})

Status = Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"]


class OptionalDependencyError(RuntimeError):
    """Raised when the optional `ortools` extra is not installed."""


def _ticks(seconds: float) -> int:
    return round(seconds * SCALE)


def _seconds(ticks: int) -> float:
    return ticks / SCALE


# --- refusals -------------------------------------------------------------------------------


def _refusals(problem: Problem, kind: ObjectiveKind) -> list[ScheduleIssue]:
    """`unsupported_objective` for a primary CP-SAT does not optimize, else the precision guard."""
    if kind not in OPTIMIZED_OBJECTIVES:
        message = f"CP-SAT optimizes makespan or max_lateness, not {kind.value}"
        return [
            ScheduleIssue(code="unsupported_objective", path="profile.objective", message=message)
        ]
    for path, value in _time_values(problem):
        if round(value, MAX_DECIMALS) != value:
            message = f"{value!r} has more than {MAX_DECIMALS} decimal places"
            return [ScheduleIssue(code="unsupported_precision", path=path, message=message)]
    return []


def _magnitude_refusals(problem: Problem, horizon: int) -> list[ScheduleIssue]:
    """`unsupported_magnitude` for the first value whose ticks pass `MAX_TICKS`.

    Checked: every time input, every operation's summed option runs (an end expression
    adds them) and the horizon. Every term of the objective and of a lag constraint is a
    sum of a few of these, so all stay far inside int64.
    """
    values = [*_time_values(problem), *_run_values(problem)]
    for path, value in [("problem.horizon_end", _seconds(horizon)), *values]:
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


def _time_values(problem: Problem) -> Iterator[tuple[str, float]]:
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


# --- model ----------------------------------------------------------------------------------


def _run_ticks(op: Operation, machine_id: str) -> int:
    option = next(o for o in op.options if o.machine_id == machine_id)
    return _ticks(option.fixed_seconds + op.quantity * option.unit_seconds)


def _scheduled(problem: Problem) -> list[tuple[Job, Operation]]:
    """Operations to place: not done, of jobs that are neither done nor held."""
    return [
        (job, op)
        for job in problem.jobs
        if job.status == "open"
        for op in job.operations
        if op.status.state != "done"
    ]


def _safe_horizon(problem: Problem, scheduled: Sequence[tuple[Job, Operation]]) -> int:
    """A bound no optimal schedule needs to exceed: the latest fixed time plus all work.

    Fixed times are now, releases, ready times, machine availability, pins, calendar ends
    and in-progress ends; all work is every operation's longest option plus every min lag.
    Used when `problem.horizon_end` is not set.
    """
    anchors = [problem.now, *(m.state.available_from for m in problem.machines)]
    anchors += [end for calendar in problem.calendars for _, end in calendar.intervals]
    work = 0.0
    for job, op in scheduled:
        pin_times = (op.pin.earliest_start, op.pin.latest_start) if op.pin else ()
        anchors += [job.release, op.ready_time, *(t for t in pin_times if t is not None)]
        anchors.append(problem.now + (op.status.remaining_seconds or 0.0))
        work += max(o.fixed_seconds + op.quantity * o.unit_seconds for o in op.options)
        work += sum(link.min_lag for link in op.predecessors)
    return _ticks(max(anchors)) + _ticks(work) + 1


def _horizon(problem: Problem, scheduled: Sequence[tuple[Job, Operation]]) -> int:
    """Last tick any operation may end: `horizon_end` (never before now), else a safe bound."""
    if problem.horizon_end is None:
        return _safe_horizon(problem, scheduled)
    return max(_ticks(problem.horizon_end), _ticks(problem.now))


def _breaks(intervals: Sequence[Interval], low: int, high: int) -> list[tuple[int, int]]:
    """Non-working tick ranges inside `[low, high)`; touching intervals leave no break."""
    gaps: list[tuple[int, int]] = []
    cursor = low
    for start, end in intervals:
        start_ticks, end_ticks = _ticks(start), _ticks(end)
        if start_ticks > cursor:
            gaps.append((cursor, min(start_ticks, high)))
        cursor = max(cursor, end_ticks)
        if cursor >= high:
            return gaps
    gaps.append((cursor, high))
    return gaps


@dataclass
class _Placed:
    """One operation in the model: its start, its end expression and its option choices."""

    job: Job
    op: Operation
    start: Any
    end: Any
    choices: list[tuple[str, Any]]


@dataclass
class _Builder:
    """Builds the CP-SAT model for one problem; holds the per-resource interval lists."""

    cp_model: Any
    problem: Problem
    horizon: int
    model: Any = None
    placed: dict[str, _Placed] = field(default_factory=dict)
    machine_intervals: dict[str, list[tuple[Any, Any, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    machine_floor: dict[str, int] = field(default_factory=dict)
    pool_usage: dict[str, list[tuple[Any, int]]] = field(default_factory=lambda: defaultdict(list))
    pool_members: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(list))
    machines: dict[str, Machine] = field(default_factory=dict)
    calendars: dict[str, tuple[Interval, ...]] = field(default_factory=dict)

    def build(self, scheduled: Sequence[tuple[Job, Operation]], kind: ObjectiveKind) -> None:
        """Add every variable and constraint, then the objective for `kind`."""
        self.model = self.cp_model.CpModel()
        self.machines = {m.id: m for m in self.problem.machines}
        self.calendars = {c.id: c.intervals for c in self.problem.calendars}
        now = _ticks(self.problem.now)
        for job, op in scheduled:
            if op.status.state == "in_progress":
                self._add_in_progress(job, op, now)
        for job, op in scheduled:
            if op.status.state != "in_progress":
                self._add_pending(job, op, now)
        for placed in self.placed.values():
            if placed.op.status.state != "in_progress":
                self._add_precedence(placed)
        self._add_machines(now)
        self._add_pools(now)
        self._add_objective(kind, now)

    # operations

    def _add_in_progress(self, job: Job, op: Operation, now: int) -> None:
        """Fixed on its status machine at `now` for the remaining seconds, setup 0."""
        machine_id = op.status.machine_id or op.options[0].machine_id
        remaining = _ticks(op.status.remaining_seconds or 0.0)
        start = self.model.NewConstant(now)
        self.machine_floor[machine_id] = max(
            self.machine_floor.get(machine_id, now), now + remaining
        )
        interval = self.model.NewFixedSizeIntervalVar(now, remaining, f"run_{op.id}")
        self._add_needs(op, interval)
        on_machine = [(machine_id, self.model.NewConstant(1))]
        self.placed[op.id] = _Placed(job, op, start, start + remaining, on_machine)

    def _add_pending(self, job: Job, op: Operation, now: int) -> None:
        start = self.model.NewIntVar(now, self.horizon, f"start_{op.id}")
        self._add_start_bounds(job, op, start)
        pin_machine = op.pin.machine_id if op.pin else None
        machine_ids = [o.machine_id for o in op.options if pin_machine in (None, o.machine_id)]
        choices: list[tuple[str, Any]] = []
        run_terms = []
        for machine_id in machine_ids:
            presence = self.model.NewBoolVar(f"on_{op.id}_{machine_id}")
            run = _run_ticks(op, machine_id)
            self._add_option(op, machine_id, start, run, presence)
            choices.append((machine_id, presence))
            run_terms.append(run * presence)
        self.model.AddExactlyOne(presence for _, presence in choices)
        end = start + sum(run_terms)
        self.model.Add(end <= self.horizon)
        self.placed[op.id] = _Placed(job, op, start, end, choices)

    def _add_start_bounds(self, job: Job, op: Operation, start: Any) -> None:
        """Material readiness (attached setup: the gate is `start`) and the pin window."""
        self.model.Add(start >= _ticks(max(job.release, op.ready_time)))
        if op.pin is not None and op.pin.earliest_start is not None:
            self.model.Add(start >= _ticks(op.pin.earliest_start))
        if op.pin is not None and op.pin.latest_start is not None:
            self.model.Add(start <= _ticks(op.pin.latest_start))

    def _add_option(
        self, op: Operation, machine_id: str, start: Any, run: int, presence: Any
    ) -> None:
        """The option's interval, its machine availability and its operator needs."""
        size = max(run, 1)  # a zero-length run still holds one tick (module docstring)
        interval = self.model.NewOptionalFixedSizeIntervalVar(
            start, size, presence, f"run_{op.id}_{machine_id}"
        )
        available_from = self.machines[machine_id].state.available_from
        self.model.Add(start >= _ticks(available_from)).OnlyEnforceIf(presence)
        self.machine_intervals[machine_id].append((interval, start, presence))
        self._add_needs(op, interval)
        for need in op.operator_needs:
            self.pool_members[need.pool_id].append(interval)

    def _add_needs(self, op: Operation, interval: Any) -> None:
        """Pool capacity use; with no setups only the run phase holds operators."""
        for need in op.operator_needs:
            if _holds_during_run(need):
                self.pool_usage[need.pool_id].append((interval, need.quantity))

    def _add_precedence(self, placed: _Placed) -> None:
        """End-to-start lags from every predecessor that is placed (done ones count as finished)."""
        for link in placed.op.predecessors:
            pred = self.placed.get(link.operation_id)
            if pred is None:
                continue
            self.model.Add(placed.start >= pred.end + _ticks(link.min_lag))
            if link.max_lag is not None:
                self.model.Add(placed.start <= pred.end + _ticks(link.max_lag))

    # resources

    def _calendar(self, calendar_id: str | None) -> tuple[Interval, ...] | None:
        if calendar_id is None:
            return None
        return self.calendars[calendar_id]

    def _break_intervals(self, calendar_id: str | None, now: int) -> list[Any]:
        intervals = self._calendar(calendar_id)
        if intervals is None:
            return []
        gaps = _breaks(intervals, now, self.horizon + 1)
        return [
            self.model.NewFixedSizeIntervalVar(lo, hi - lo, f"break_{calendar_id}_{lo}")
            for lo, hi in gaps
            if hi > lo
        ]

    def _add_machines(self, now: int) -> None:
        """No overlap with each other or the calendar's breaks; in-progress work comes first."""
        for machine in self.problem.machines:
            members = self.machine_intervals.get(machine.id, [])
            floor = self.machine_floor.get(machine.id)
            for _, start, presence in members:
                if floor is not None:
                    self.model.Add(start >= floor).OnlyEnforceIf(presence)
            intervals = [interval for interval, _, _ in members]
            self.model.AddNoOverlap(intervals + self._break_intervals(machine.calendar_id, now))

    def _add_pools(self, now: int) -> None:
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
            breaks = self._break_intervals(pool.calendar_id, now)
            if members and breaks:
                n = len(members)
                self.model.AddCumulative(members + breaks, [1] * n + [n] * len(breaks), n)

    # objective

    def _add_objective(self, kind: ObjectiveKind, now: int) -> None:
        """Minimize max(C_j) - now, or max over jobs with a due date of C_j - d_j (D-068)."""
        if kind is ObjectiveKind.MAKESPAN:
            terms = [p.end - now for p in self.placed.values()]
        else:
            terms = [
                p.end - _ticks(p.job.due) for p in self.placed.values() if p.job.due is not None
            ]
        reach = max(abs(term_bound) for term_bound in (self.horizon, now, *self._due_ticks()))
        objective = self.model.NewIntVar(-2 * reach, 2 * reach, "objective")
        if terms:
            self.model.AddMaxEquality(objective, terms)
        else:
            self.model.Add(objective == 0)
        self.model.Minimize(objective)

    def _due_ticks(self) -> list[int]:
        return [_ticks(p.job.due) for p in self.placed.values() if p.job.due is not None]


def _holds_during_run(need: OperatorNeed) -> bool:
    return need.phase in ("run", "both")


# --- solver -----------------------------------------------------------------------------------


class ORToolsSolver:
    """CP-SAT solver: optimal or best-found plans for makespan or max lateness (COMP-010).

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
        """Features CP-SAT models in TF-002 (D-067)."""
        return CAPABILITIES

    def solve(
        self, problem: Problem, *, objective: ObjectiveSpec, budget_seconds: float
    ) -> Schedule:
        """Optimize the primary objective of `objective` within `budget_seconds`.

        UNKNOWN with `unsupported_objective`, `unsupported_precision` or
        `unsupported_magnitude` when refused (the model is not built); INFEASIBLE with
        `infeasible`; UNKNOWN with `time_limit` when the budget ran out before any plan. A
        model CP-SAT finds invalid is a bug and raises.
        """
        started = time.perf_counter()
        kind = primary_kind(objective)
        refusals = _refusals(problem, kind)
        if refusals:
            return _schedule("UNKNOWN", started, issues=refusals)
        scheduled = _scheduled(problem)
        horizon = _horizon(problem, scheduled)
        refusals = _magnitude_refusals(problem, horizon)
        if refusals:
            return _schedule("UNKNOWN", started, issues=refusals)
        builder = _Builder(self._cp_model, problem, horizon)
        builder.build(scheduled, kind)
        cp_solver = self._cp_model.CpSolver()
        cp_solver.parameters.max_time_in_seconds = budget_seconds
        cp_solver.parameters.num_workers = self._workers
        cp_solver.parameters.random_seed = self._seed
        status = self._status(cp_solver.Solve(builder.model), builder.model)
        return _answer(status, cp_solver, builder, kind, started)

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


def _answer(
    status: Status, cp_solver: Any, builder: _Builder, kind: ObjectiveKind, started: float
) -> Schedule:
    if status == "INFEASIBLE":
        issue = ScheduleIssue(
            code="infeasible", path="problem", message="no schedule satisfies every constraint"
        )
        return _schedule(status, started, issues=[issue])
    bound = cp_solver.BestObjectiveBound() / SCALE
    if status == "UNKNOWN":
        message = "no schedule found within the time budget"
        issue = ScheduleIssue(code="time_limit", path="budget_seconds", message=message)
        return _schedule(status, started, issues=[issue], best_bound=bound)
    assignments = [_assignment(cp_solver, placed) for placed in builder.placed.values()]
    value = _primary_value(builder, assignments, kind)
    return _schedule(
        status, started, assignments=assignments, primary_value=value, best_bound=bound
    )


def _assignment(cp_solver: Any, placed: _Placed) -> Assignment:
    machine_id = next(m for m, presence in placed.choices if cp_solver.Value(presence))
    start = _seconds(cp_solver.Value(placed.start))
    end = _seconds(cp_solver.Value(placed.end))
    return Assignment(
        operation_id=placed.op.id, machine_id=machine_id, start=start, setup_end=start, end=end
    )


def _primary_value(
    builder: _Builder, assignments: Sequence[Assignment], kind: ObjectiveKind
) -> float:
    """The primary objective in seconds, from the plan itself, as `objectives.evaluate` does."""
    completion: dict[str, float] = {}
    for a in assignments:
        job = builder.placed[a.operation_id].job
        completion[job.id] = max(completion.get(job.id, a.end), a.end)
    if kind is ObjectiveKind.MAKESPAN:
        return max(completion.values(), default=builder.problem.now) - builder.problem.now
    dues = {job.id: job.due for job in builder.problem.jobs if job.due is not None}
    return max((c - dues[j] for j, c in completion.items() if j in dues), default=0.0)


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
        budget_enforced=True,
    )
