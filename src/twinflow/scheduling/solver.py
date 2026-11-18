"""Solver entry point: `solve()` (COMP-001, COMP-008, REQ-072, REQ-073, REQ-087).

`solve()` is the one entry point every surface calls: validate the request, compile its
overrides (D-066), refuse features the solver does not declare (D-067; `features.py`
derives them), run the solver on the compiled problem with the profile's objective spec,
verify any FEASIBLE or OPTIMAL answer with the independent verifier, and evaluate every
objective of a verified schedule (D-068). The solver gets a `SolveContext` built from the
request (D-070).

The portfolio runner (COMP-024) reuses the same path in parts: `prepare` once per request,
`solve_prepared` per pipeline stage, `check_schedule` per Pareto search point.

The seam types (`Feature`, `Solver`, `SolveContext`) live in the leaf `seam.py`; solver
modules import them from there, so this module imports the default baseline at module top
without a cycle.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass

from twinflow.scheduling.baseline import BaselineSolver
from twinflow.scheduling.features import problem_features
from twinflow.scheduling.model import (
    CompiledProblem,
    ObjectiveKind,
    Schedule,
    ScheduleIssue,
    SolveRequest,
    SolveResult,
)
from twinflow.scheduling.objectives import evaluate
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.seam import Feature, SolveContext, Solver, enforces_budget
from twinflow.scheduling.validator import validate
from twinflow.scheduling.verifier import verify

SOLUTION_STATUSES = frozenset({"FEASIBLE", "OPTIMAL"})
PROBLEM_PATH = "problem"
BUDGET_PATH = "budget_seconds"
SOLVER_PATH = "solver"


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
        schedule = solver.solve(
            compiled.problem,
            objective=compiled.profile.objective,
            budget_seconds=budget,
            context=context,
        )
        if enforces_budget(solver) and not schedule.budget_enforced:
            schedule = schedule.model_copy(update={"budget_enforced": True})
        return schedule
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
