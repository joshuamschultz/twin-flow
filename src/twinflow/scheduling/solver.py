"""Solver seam: features, the `Solver` protocol and `solve()` (COMP-008, REQ-072, REQ-073).

`solve()` is the one entry point every surface calls: validate the request, compile its
overrides (D-066), refuse features the solver does not declare (D-067), run the solver on
the compiled problem with the profile's objective spec, verify any FEASIBLE or OPTIMAL
answer with the independent verifier, and evaluate every objective of a verified
schedule (D-068).

Solver modules (`baseline.py`, `cpsat.py`) import `Feature` and `Solver` from here, so this
module never imports them at module level; the default baseline is imported lazily inside
`_default_solver` to keep the import graph acyclic.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Protocol

from twinflow.scheduling.model import (
    CompiledProblem,
    Machine,
    ObjectiveKind,
    ObjectiveSpec,
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
from twinflow.scheduling.setups import SetupTable
from twinflow.scheduling.validator import validate
from twinflow.scheduling.verifier import verify

SOLUTION_STATUSES = frozenset({"FEASIBLE", "OPTIMAL"})
PROBLEM_PATH = "problem"
BUDGET_PATH = "budget_seconds"


class Feature(StrEnum):
    """Modeling features a problem can use and a solver can declare support for (D-067)."""

    ALTERNATIVES = "alternatives"
    SETUPS = "setups"
    DETACHED_SETUP = "detached_setup"
    CALENDARS = "calendars"
    PAUSE = "pause"
    OPERATORS = "operators"
    BATCHING = "batching"
    RESTART = "restart"
    LAGS = "lags"
    MAX_LAGS = "max_lags"
    OVERLAP = "overlap"
    PINS = "pins"
    IN_PROGRESS = "in_progress"
    MACHINE_STATE = "machine_state"


class Solver(Protocol):
    """A scheduling algorithm behind the seam (REQ-072, D-067).

    `solve` gets the compiled problem and the objective spec only, never the overrides or
    the profile. It returns UNKNOWN or INFEASIBLE with named issues when it has no plan.
    """

    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[Feature]: ...

    def solve(
        self, problem: Problem, *, objective: ObjectiveSpec, budget_seconds: float
    ) -> Schedule: ...


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
    - pause / operators / pins / in_progress: an operation with `shift_crossing` pause,
      operator needs, a pin, or state `in_progress`.
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
    features: set[Feature] = set()
    for op in operations:
        features |= _operation_features(op, table)
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
        Feature.PAUSE: op.shift_crossing == "pause",
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


def _uses_pool_calendar(problem: Problem, operations: Iterable[Operation]) -> bool:
    used = {need.pool_id for op in operations for need in op.operator_needs}
    return any(pool.id in used and pool.calendar_id is not None for pool in problem.operator_pools)


def _present(flags: dict[Feature, bool]) -> set[Feature]:
    return {feature for feature, present in flags.items() if present}


# --- solve ---------------------------------------------------------------------------------


def solve(
    request: SolveRequest, *, solver: Solver | None = None, budget_seconds: float | None = None
) -> SolveResult:
    """Validate, compile, check capabilities, run, verify and evaluate (COMP-008).

    `solver` None means the baseline heuristic. `budget_seconds` None means the profile's
    `time_budget_seconds`; a budget that is not finite and > 0 gets a `budget` issue.
    Every refusal (budget, validator issues, `unsupported_feature`, D-067) returns an
    UNKNOWN schedule with the issues and never calls the solver. `verified` is true only
    when the solver answered FEASIBLE or OPTIMAL and the verifier found no issue
    (REQ-073); only then are all thirteen objective values reported. An exception raised
    by the solver is a solver bug and propagates.
    """
    chosen = solver if solver is not None else _default_solver()
    budget = request.profile.time_budget_seconds if budget_seconds is None else budget_seconds
    if not (math.isfinite(budget) and budget > 0):
        message = f"budget_seconds must be finite and > 0, got {budget}"
        return _refused(chosen, [ScheduleIssue(code="budget", path=BUDGET_PATH, message=message)])
    request_issues = validate(request)
    if request_issues:
        return _refused(chosen, request_issues)
    compiled = apply_overrides(request)
    compiled_issues = validate(compiled)
    if compiled_issues:
        return _refused(chosen, compiled_issues, compiled)
    features = problem_features(compiled.problem)
    missing = sorted(features - chosen.capabilities)
    if missing:
        return _refused(chosen, [_unsupported(chosen, f) for f in missing], compiled, features)
    schedule = chosen.solve(
        compiled.problem, objective=compiled.profile.objective, budget_seconds=budget
    )
    return _checked(request, compiled, features, schedule)


def _default_solver() -> Solver:
    """The baseline heuristic, imported here because baseline.py imports this module."""
    from twinflow.scheduling.baseline import BaselineSolver

    return BaselineSolver()


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
    verification = verify(compiled, request.overrides, schedule)
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
