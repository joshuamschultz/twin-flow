"""Solver entry point: feature derivation and `solve()` (COMP-001, COMP-008, REQ-072, REQ-073,
REQ-087).

`solve()` is the one entry point every surface calls: validate the request, compile its
overrides (D-066), refuse features the solver does not declare (D-067), run the solver on
the compiled problem with the profile's objective spec, verify any FEASIBLE or OPTIMAL
answer with the independent verifier, and evaluate every objective of a verified
schedule (D-068). The solver gets a `SolveContext` built from the request (D-070).

The portfolio runner (COMP-024) reuses the same path in parts: `prepare` once per request,
`solve_prepared` per pipeline stage, `check_schedule` per Pareto search point.

The seam types (`Feature`, `Solver`, `SolveContext`) live in the leaf `seam.py`; solver
modules import them from there, so this module imports the default baseline at module top
without a cycle.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from twinflow.scheduling.baseline import BaselineSolver
from twinflow.scheduling.model import (
    CompiledProblem,
    Machine,
    ObjectiveKind,
    Operation,
    Precedence,
    Problem,
    Schedule,
    ScheduleIssue,
    SolveRequest,
    SolveResult,
)
from twinflow.scheduling.objectives import evaluate
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.seam import Feature, SolveContext, Solver
from twinflow.scheduling.setups import SetupTable
from twinflow.scheduling.validator import validate
from twinflow.scheduling.verifier import verify

SOLUTION_STATUSES = frozenset({"FEASIBLE", "OPTIMAL"})
PROBLEM_PATH = "problem"
BUDGET_PATH = "budget_seconds"
SOLVER_PATH = "solver"


# --- feature derivation --------------------------------------------------------------------


def problem_features(problem: Problem) -> frozenset[Feature]:
    """Every `Feature` the problem uses; operations in state `done` are skipped.

    The problem must be valid (`validate` returns no issue). The rules:

    - alternatives: an operation with more than one option.
    - setups: an option machine of an operation resolves a setup function (its own, else
      its work center's, D-064).
    - detached_setup: a `setup_detached` operation that has `setups` by the rule above. A
      resolved function is taken as "can get a non-zero setup" without inspecting its
      values, so a detached op always comes with `setups` and a flag on a setup-free
      problem triggers no refusal.
    - calendars: a machine with a calendar, or a pool with a calendar that an operation
      draws operators from.
    - pause: an operation with `shift_crossing` pause where a calendar can apply to it: an
      option machine has a calendar, or a pool it draws operators from has one. Without
      such a calendar the op never crosses a closed period, so pause changes nothing.
    - operators / pins / in_progress: an operation with operator needs, a pin, or state
      `in_progress`.
    - batching: an operation with a `batch_family`. A batch machine alone is not batching:
      operations without a family run on it one at a time.
    - restart: a machine with a restart rule.
    - lags / max_lags / overlap: a precedence with `min_lag` > 0, a `max_lag`, or an
      `overlap_quantity`.
    - machine_state: a machine available only after `now`, or with a last setup (params or
      family) that can change the first setup.
    """
    table = SetupTable.for_problem(problem)
    operations = [op for job in problem.jobs for op in job.operations if op.status.state != "done"]
    calendared = _Calendared.of(problem)
    features: set[Feature] = set()
    for op in operations:
        features |= _operation_features(op, table)
        if op.shift_crossing == "pause" and calendared.apply_to(op):
            features.add(Feature.PAUSE)
    for machine in problem.machines:
        features |= _machine_features(machine, problem.now)
    if _uses_pool_calendar(problem, operations):
        features.add(Feature.CALENDARS)
    return frozenset(features)


def _operation_features(op: Operation, table: SetupTable) -> set[Feature]:
    has_setup = any(table.function_for(option.machine_id) is not None for option in op.options)
    flags = {
        Feature.ALTERNATIVES: len(op.options) > 1,
        Feature.SETUPS: has_setup,
        Feature.DETACHED_SETUP: op.setup_detached and has_setup,
        Feature.OPERATORS: bool(op.operator_needs),
        Feature.BATCHING: op.batch_family is not None,
        Feature.PINS: op.pin is not None,
        Feature.IN_PROGRESS: op.status.state == "in_progress",
    }
    return _present(flags) | _precedence_features(op.predecessors)


def _precedence_features(links: Sequence[Precedence]) -> set[Feature]:
    flags = {
        Feature.LAGS: any(link.min_lag > 0 for link in links),
        Feature.MAX_LAGS: any(link.max_lag is not None for link in links),
        Feature.OVERLAP: any(link.overlap_quantity is not None for link in links),
    }
    return _present(flags)


def _machine_features(machine: Machine, now: float) -> set[Feature]:
    state = machine.state
    flags = {
        Feature.CALENDARS: machine.calendar_id is not None,
        Feature.RESTART: machine.restart is not None,
        Feature.MACHINE_STATE: state.available_from > now
        or bool(state.last_setup_params)
        or state.last_setup_family is not None,
    }
    return _present(flags)


@dataclass(frozen=True, slots=True)
class _Calendared:
    """Ids of machines and of operator pools that carry a calendar (kept apart: ids may clash)."""

    machines: frozenset[str]
    pools: frozenset[str]

    @classmethod
    def of(cls, problem: Problem) -> _Calendared:
        machines = frozenset(m.id for m in problem.machines if m.calendar_id is not None)
        pools = frozenset(p.id for p in problem.operator_pools if p.calendar_id is not None)
        return cls(machines, pools)

    def apply_to(self, op: Operation) -> bool:
        """True when an option machine of `op` or a pool it draws from has a calendar."""
        return any(option.machine_id in self.machines for option in op.options) or any(
            need.pool_id in self.pools for need in op.operator_needs
        )


def _uses_pool_calendar(problem: Problem, operations: Iterable[Operation]) -> bool:
    used = {need.pool_id for op in operations for need in op.operator_needs}
    return any(pool.id in used and pool.calendar_id is not None for pool in problem.operator_pools)


def _present(flags: dict[Feature, bool]) -> set[Feature]:
    return {feature for feature, present in flags.items() if present}


# --- solve ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Prepared:
    """A request validated and compiled once, ready for any number of solver runs.

    `issues` holds the refusal issues (budget, validator); with any, no solver runs.
    `compiled` is None when the request itself (or its budget) was refused. `solve()` and
    the portfolio runner (COMP-024) share this path, so every candidate is checked exactly
    like a `solve()` answer. Picklable: the runner ships it to spawned workers.
    """

    request: SolveRequest
    compiled: CompiledProblem | None
    features: frozenset[Feature]
    issues: tuple[ScheduleIssue, ...]


def prepare(request: SolveRequest, budget: float) -> Prepared:
    """Check the budget, validate, compile the overrides and validate the compiled problem.

    A budget that is not finite and > 0 gets a `budget` issue; the validator's issues on the
    request or on the compiled problem are refusals too.
    """
    if not (math.isfinite(budget) and budget > 0):
        message = f"budget_seconds must be finite and > 0, got {budget}"
        issue = ScheduleIssue(code="budget", path=BUDGET_PATH, message=message)
        return Prepared(request, None, frozenset(), (issue,))
    request_issues = validate(request)
    if request_issues:
        return Prepared(request, None, frozenset(), tuple(request_issues))
    compiled = apply_overrides(request)
    compiled_issues = validate(compiled)
    if compiled_issues:
        return Prepared(request, compiled, frozenset(), tuple(compiled_issues))
    return Prepared(request, compiled, problem_features(compiled.problem), ())


def solve(
    request: SolveRequest,
    *,
    solver: Solver | None = None,
    budget_seconds: float | None = None,
    seed: int = 0,
    warm_start: Schedule | None = None,
) -> SolveResult:
    """Validate, compile, check capabilities, run, verify and evaluate (COMP-001, COMP-008).

    `solver` None means the baseline heuristic. `budget_seconds` None means the profile's
    `time_budget_seconds`; a budget that is not finite and > 0 gets a `budget` issue.
    Every refusal (budget, validator issues, `unsupported_feature`, D-067) returns an
    UNKNOWN schedule with the issues and never calls the solver. `verified` is true only
    when the solver answered FEASIBLE or OPTIMAL and the verifier found no issue
    (REQ-073); only then are all thirteen objective values reported. An exception raised
    by the solver is a solver bug: it becomes an UNKNOWN schedule with a `solver_error`
    issue whose message is the exception class name only, so no internals leak.

    The solver receives a `SolveContext` (D-070): the request's previous plan, the frozen
    zone tolerance (0 without a zone), `seed`, `warm_start`, a monotonic deadline of now +
    budget taken just before the call, and the profile's `method_params` for its name.
    """
    chosen = solver if solver is not None else BaselineSolver()
    budget = request.profile.time_budget_seconds if budget_seconds is None else budget_seconds
    prepared = prepare(request, budget)
    return solve_prepared(prepared, chosen, budget, seed=seed, warm_start=warm_start)


def solve_prepared(
    prepared: Prepared,
    solver: Solver,
    budget: float,
    *,
    seed: int = 0,
    warm_start: Schedule | None = None,
) -> SolveResult:
    """The rest of `solve()` on a prepared request: refuse, check features, run, check.

    The solver runs on the compiled problem with `compiled.profile.objective`.
    """
    compiled = prepared.compiled
    if compiled is None or prepared.issues:
        return _refused(solver, prepared.issues, compiled)
    missing = sorted(prepared.features - solver.capabilities)
    if missing:
        issues = [_unsupported(solver, f) for f in missing]
        return _refused(solver, issues, compiled, prepared.features)
    context = _context(prepared.request, solver, budget, seed, warm_start)
    schedule = _run(solver, compiled, budget, context)
    return _checked(prepared.request, compiled, prepared.features, schedule)


def check_schedule(prepared: Prepared, schedule: Schedule) -> SolveResult:
    """Verify and evaluate a schedule built outside a solver (Pareto search points).

    Raises ValueError for a refused request: it has no compiled problem to check against.
    """
    if prepared.compiled is None or prepared.issues:
        raise ValueError("a refused request has no compiled problem to check a schedule on")
    return _checked(prepared.request, prepared.compiled, prepared.features, schedule)


def _context(
    request: SolveRequest,
    solver: Solver,
    budget: float,
    seed: int,
    warm_start: Schedule | None,
) -> SolveContext:
    """The `SolveContext` for one call; the deadline clock starts here."""
    profile = request.profile
    zone = profile.frozen_zone
    return SolveContext(
        previous_plan=request.previous_plan,
        churn_tolerance=zone.tolerance if zone is not None else 0.0,
        seed=seed,
        deadline=time.monotonic() + budget,
        warm_start=warm_start,
        params=profile.method_params.get(solver.name, {}),
    )


def _run(
    solver: Solver, compiled: CompiledProblem, budget: float, context: SolveContext
) -> Schedule:
    """The solver's answer; an exception becomes UNKNOWN with `solver_error`."""
    try:
        return solver.solve(
            compiled.problem,
            objective=compiled.profile.objective,
            budget_seconds=budget,
            context=context,
        )
    except Exception as error:  # any solver bug must not escape solve() as a traceback
        issue = ScheduleIssue(code="solver_error", path=SOLVER_PATH, message=type(error).__name__)
        return Schedule(
            status="UNKNOWN",
            assignments=(),
            solver=solver.name,
            elapsed_seconds=0.0,
            issues=(issue,),
        )


def _unsupported(solver: Solver, feature: Feature) -> ScheduleIssue:
    message = f"solver {solver.name} does not support feature {feature.value}"
    return ScheduleIssue(code="unsupported_feature", path=PROBLEM_PATH, message=message)


def _refused(
    solver: Solver,
    issues: Sequence[ScheduleIssue],
    compiled: CompiledProblem | None = None,
    features: frozenset[Feature] = frozenset(),
) -> SolveResult:
    """An UNKNOWN result for a solver that was not called."""
    schedule = Schedule(status="UNKNOWN", assignments=(), solver=solver.name, elapsed_seconds=0.0)
    return _result(schedule, compiled, features, issues=tuple(issues))


def _checked(
    request: SolveRequest,
    compiled: CompiledProblem,
    features: frozenset[Feature],
    schedule: Schedule,
) -> SolveResult:
    """Verify a FEASIBLE or OPTIMAL answer and evaluate it when it verifies."""
    if schedule.status not in SOLUTION_STATUSES:
        return _result(schedule, compiled, features, issues=schedule.issues)
    verification = verify(compiled, request.overrides, schedule, request.previous_plan)
    issues = (*schedule.issues, *verification.issues)
    if not verification.valid:
        return _result(schedule, compiled, features, issues=issues)
    values = evaluate(compiled, schedule, request.previous_plan)
    return _result(schedule, compiled, features, issues=issues, values=values)


def _result(
    schedule: Schedule,
    compiled: CompiledProblem | None,
    features: frozenset[Feature],
    *,
    issues: tuple[ScheduleIssue, ...],
    values: dict[ObjectiveKind, float] | None = None,
) -> SolveResult:
    """Assemble the result; objective values are present exactly when verified."""
    return SolveResult(
        schedule=schedule,
        objective_values=values or {},
        verified=values is not None,
        issues=issues,
        held_job_ids=compiled.held_job_ids if compiled else (),
        released_from_frozen=compiled.released_from_frozen if compiled else (),
        features=tuple(feature.value for feature in sorted(features)),
    )
