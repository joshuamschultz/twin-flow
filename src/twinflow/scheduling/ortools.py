"""Optional CP-SAT implementation of the scheduling solver protocol."""

from __future__ import annotations

import importlib
from decimal import Decimal
from typing import Any, cast

from twinflow.scheduling.core import (
    ScheduleIssue,
    ScheduleResult,
    SchedulingProblem,
    Solver,
    Status,
    verify_schedule,
)


class OptionalDependencyError(RuntimeError):
    """Raised when the optional optimization backend is absent."""


class ORToolsSolver(Solver):
    """CP-SAT translation using integer milliseconds for float input."""

    def __init__(self, *, workers: int = 1, seed: int = 0) -> None:
        if workers < 1 or seed < 0:
            raise ValueError("workers must be positive and seed must be non-negative")
        self._workers = workers
        self._seed = seed
        try:
            self._cp_model = importlib.import_module("ortools.sat.python.cp_model")
        except ImportError as exc:
            raise OptionalDependencyError(
                "install the 'ortools' extra to use ORToolsSolver"
            ) from exc

    def solve(
        self, problem: SchedulingProblem, *, time_limit_seconds: float | None = None
    ) -> ScheduleResult:
        from twinflow.scheduling.core import validate

        issues = validate(problem)
        if issues:
            return ScheduleResult("INFEASIBLE", issues=tuple(issues))
        if time_limit_seconds is not None and time_limit_seconds <= 0:
            return ScheduleResult(
                "UNKNOWN",
                issues=(
                    ScheduleIssue(
                        "invalid_time_limit", "time_limit_seconds", "time limit must be positive"
                    ),
                ),
            )
        precision = _precision_issue(problem)
        if precision is not None:
            return ScheduleResult("UNKNOWN", issues=(precision,))
        cp_model: Any = self._cp_model
        scale = 1_000_000
        horizon = max((window.end for window in problem.resources), default=0.0)
        horizon_i = int(
            round((horizon + sum(operation.duration for operation in problem.operations)) * scale)
        )
        model = cp_model.CpModel()
        starts = {
            operation.id: model.NewIntVar(0, horizon_i, f"start_{operation.id}")
            for operation in problem.operations
        }
        ends = {
            operation.id: model.NewIntVar(0, horizon_i, f"end_{operation.id}")
            for operation in problem.operations
        }
        choices: dict[str, list[tuple[str, Any, Any]]] = {}
        intervals_by_resource: dict[str, list[Any]] = {}
        for operation in problem.operations:
            model.Add(
                ends[operation.id] == starts[operation.id] + int(round(operation.duration * scale))
            )
            model.Add(starts[operation.id] >= int(round(operation.ready_time * scale)))
            eligible = operation.eligible_resources or tuple(
                {window.resource_id for window in problem.resources}
            )
            choices[operation.id] = []
            for index, window in enumerate(problem.resources):
                if (
                    window.resource_id not in eligible
                    or not operation.required_qualifications <= window.qualifications
                ):
                    continue
                presence = model.NewBoolVar(f"choose_{operation.id}_{index}")
                model.Add(starts[operation.id] >= int(round(window.start * scale))).OnlyEnforceIf(
                    presence
                )
                model.Add(ends[operation.id] <= int(round(window.end * scale))).OnlyEnforceIf(
                    presence
                )
                if operation.frozen_start is not None:
                    model.Add(
                        starts[operation.id] == int(round(operation.frozen_start * scale))
                    ).OnlyEnforceIf(presence)
                if (
                    operation.frozen_resource is not None
                    and window.resource_id != operation.frozen_resource
                ):
                    model.Add(presence == 0)
                interval = model.NewOptionalIntervalVar(
                    starts[operation.id],
                    int(round(operation.duration * scale)),
                    ends[operation.id],
                    presence,
                    f"interval_{operation.id}_{index}",
                )
                choices[operation.id].append((window.resource_id, presence, interval))
                intervals_by_resource.setdefault(window.resource_id, []).append(interval)
            if not choices[operation.id]:
                return ScheduleResult(
                    "INFEASIBLE",
                    issues=(
                        ScheduleIssue(
                            "no_resource",
                            f"operations[{operation.id}]",
                            "no CP-SAT eligible resource",
                        ),
                    ),
                )
            model.Add(sum(choice[1] for choice in choices[operation.id]) == 1)
        for intervals in intervals_by_resource.values():
            model.AddNoOverlap(intervals)
        for operation in problem.operations:
            for predecessor in operation.predecessors:
                model.Add(ends[predecessor] <= starts[operation.id])
        objective_var = model.NewIntVar(0, horizon_i, "objective")
        if problem.objective == "lateness" and problem.deadline is not None:
            lateness = []
            deadline = int(round(problem.deadline * scale))
            for operation in problem.operations:
                value = model.NewIntVar(0, horizon_i, f"lateness_{operation.id}")
                model.AddMaxEquality(value, [0, ends[operation.id] - deadline])
                lateness.append(value)
            model.AddMaxEquality(objective_var, lateness)
        else:
            model.AddMaxEquality(objective_var, list(ends.values()))
        model.Minimize(objective_var)
        solver = cp_model.CpSolver()
        if time_limit_seconds is not None:
            solver.parameters.max_time_in_seconds = time_limit_seconds
        solver.parameters.num_search_workers = self._workers
        solver.parameters.random_seed = self._seed
        status_value = solver.Solve(model)
        status_map = {
            cp_model.OPTIMAL: "OPTIMAL",
            cp_model.FEASIBLE: "FEASIBLE",
            cp_model.INFEASIBLE: "INFEASIBLE",
            cp_model.UNKNOWN: "UNKNOWN",
        }
        status = cast(Status, status_map.get(status_value, "UNKNOWN"))
        if status not in {"FEASIBLE", "OPTIMAL"}:
            bound = float(solver.BestObjectiveBound()) / scale if status == "UNKNOWN" else None
            return ScheduleResult(status, best_bound=bound)
        start_values = {
            operation.id: solver.Value(starts[operation.id]) / scale
            for operation in problem.operations
        }
        end_values = {
            operation.id: solver.Value(ends[operation.id]) / scale
            for operation in problem.operations
        }
        resource_values = {
            operation.id: next(
                resource_id
                for resource_id, presence, _ in choices[operation.id]
                if solver.Value(presence)
            )
            for operation in problem.operations
        }
        candidate = ScheduleResult(
            status,
            start_values,
            end_values,
            resource_values,
            solver.ObjectiveValue() / scale,
            solver.BestObjectiveBound() / scale,
        )
        verification = verify_schedule(problem, candidate)
        return ScheduleResult(
            status,
            start_values,
            end_values,
            resource_values,
            candidate.objective_value,
            candidate.best_bound,
            verification.valid,
            verification.issues,
        )


def _precision_issue(problem: SchedulingProblem) -> ScheduleIssue | None:
    values = [operation.duration for operation in problem.operations]
    values.extend(
        value
        for operation in problem.operations
        for value in (
            operation.release_time,
            operation.material_ready_time,
            operation.document_ready_time,
            operation.frozen_start,
        )
        if value is not None
    )
    values.extend(value for window in problem.resources for value in (window.start, window.end))
    if problem.deadline is not None:
        values.append(problem.deadline)
    exponents = [Decimal(str(value)).as_tuple().exponent for value in values]
    if any(isinstance(exponent, int) and abs(exponent) > 6 for exponent in exponents):
        return ScheduleIssue(
            "unsupported_precision",
            "problem",
            "CP-SAT adapter supports at most six decimal places",
        )
    return None
