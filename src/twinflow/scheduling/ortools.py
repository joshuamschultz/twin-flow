"""Optional CP-SAT implementation of the scheduling solver protocol."""

from __future__ import annotations

import importlib
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

    def __init__(self) -> None:
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
        cp_model: Any = self._cp_model
        scale = 1000
        horizon = max(window.end for window in problem.resources)
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
        model.AddMaxEquality(objective_var, list(ends.values()))
        model.Minimize(objective_var)
        solver = cp_model.CpSolver()
        if time_limit_seconds is not None:
            solver.parameters.max_time_in_seconds = time_limit_seconds
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
