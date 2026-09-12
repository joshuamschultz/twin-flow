"""Hard-constraint scheduling with an independently checked baseline."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

Status = Literal["FEASIBLE", "OPTIMAL", "INFEASIBLE", "UNKNOWN"]


@dataclass(frozen=True)
class Operation:
    id: str
    duration: float
    predecessors: tuple[str, ...] = ()
    eligible_resources: tuple[str, ...] = ()
    required_qualifications: frozenset[str] = frozenset()
    release_time: float = 0.0
    material_ready_time: float = 0.0
    document_ready_time: float = 0.0
    frozen_start: float | None = None
    frozen_resource: str | None = None

    @property
    def ready_time(self) -> float:
        return max(self.release_time, self.material_ready_time, self.document_ready_time)


@dataclass(frozen=True)
class ResourceWindow:
    resource_id: str
    start: float
    end: float
    qualifications: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SchedulingProblem:
    operations: tuple[Operation, ...]
    resources: tuple[ResourceWindow, ...]
    objective: str = "makespan"
    deadline: float | None = None


@dataclass(frozen=True)
class ScheduleIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class ScheduleResult:
    status: Status
    starts: dict[str, float] = field(default_factory=dict)
    ends: dict[str, float] = field(default_factory=dict)
    resources: dict[str, str] = field(default_factory=dict)
    objective_value: float | None = None
    best_bound: float | None = None
    verified: bool = False
    issues: tuple[ScheduleIssue, ...] = ()


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    issues: tuple[ScheduleIssue, ...] = ()


def problem_from_dict(raw: Mapping[str, object]) -> SchedulingProblem:
    """Strict JSON boundary shared by CLI and application transports."""
    allowed = {"operations", "resources", "objective", "deadline"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown scheduling field: {sorted(unknown)[0]}")
    raw_operations = raw.get("operations")
    raw_resources = raw.get("resources")
    if not isinstance(raw_operations, list) or not isinstance(raw_resources, list):
        raise ValueError("operations and resources must be arrays")
    if len(raw_operations) > 100_000 or len(raw_resources) > 100_000:
        raise ValueError("scheduling input exceeds 100000 entities")
    operations: list[Operation] = []
    operation_fields = {
        "id",
        "duration",
        "predecessors",
        "eligible_resources",
        "required_qualifications",
        "release_time",
        "material_ready_time",
        "document_ready_time",
        "frozen_start",
        "frozen_resource",
    }
    for index, item in enumerate(raw_operations):
        if not isinstance(item, dict) or set(item) - operation_fields:
            raise ValueError(f"operations[{index}] has unknown fields or is not an object")
        operation_id = item.get("id")
        duration = item.get("duration")
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
        ):
            raise ValueError(f"operations[{index}] has invalid id or duration")

        def strings(
            field: str, raw_item: dict[str, object] = item, item_index: int = index
        ) -> tuple[str, ...]:
            value = raw_item.get(field, [])
            if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
                raise ValueError(f"operations[{item_index}].{field} must be an array of strings")
            return tuple(value)

        def number(
            field: str, raw_item: dict[str, object] = item, item_index: int = index
        ) -> float:
            value = raw_item.get(field, 0)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"operations[{item_index}].{field} must be finite numeric")
            return float(value)

        frozen_start = item.get("frozen_start")
        if frozen_start is not None and (
            isinstance(frozen_start, bool)
            or not isinstance(frozen_start, (int, float))
            or not math.isfinite(float(frozen_start))
        ):
            raise ValueError(f"operations[{index}].frozen_start must be finite numeric")
        frozen_resource = item.get("frozen_resource")
        if frozen_resource is not None and not isinstance(frozen_resource, str):
            raise ValueError(f"operations[{index}].frozen_resource must be a string")
        operations.append(
            Operation(
                operation_id,
                float(duration),
                strings("predecessors"),
                strings("eligible_resources"),
                frozenset(strings("required_qualifications")),
                number("release_time"),
                number("material_ready_time"),
                number("document_ready_time"),
                float(frozen_start) if frozen_start is not None else None,
                frozen_resource,
            )
        )
    resources: list[ResourceWindow] = []
    resource_fields = {"resource_id", "start", "end", "qualifications"}
    for index, item in enumerate(raw_resources):
        if not isinstance(item, dict) or set(item) - resource_fields:
            raise ValueError(f"resources[{index}] has unknown fields or is not an object")
        resource_id = item.get("resource_id")
        qualifications = item.get("qualifications", [])
        if (
            not isinstance(resource_id, str)
            or not isinstance(qualifications, list)
            or not all(isinstance(entry, str) for entry in qualifications)
        ):
            raise ValueError(f"resources[{index}] has invalid id or qualifications")
        start = item.get("start")
        end = item.get("end")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in (start, end)
        ):
            raise ValueError(f"resources[{index}] has nonfinite window")
        resources.append(
            ResourceWindow(
                resource_id,
                float(cast(float, start)),
                float(cast(float, end)),
                frozenset(qualifications),
            )
        )
    objective = raw.get("objective", "makespan")
    if not isinstance(objective, str):
        raise ValueError("objective must be a string")
    deadline = raw.get("deadline")
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(float(deadline))
    ):
        raise ValueError("deadline must be finite numeric")
    return SchedulingProblem(
        tuple(operations),
        tuple(resources),
        objective,
        float(deadline) if deadline is not None else None,
    )


class Solver(Protocol):
    def solve(
        self, problem: SchedulingProblem, *, time_limit_seconds: float | None = None
    ) -> ScheduleResult: ...


def validate(problem: SchedulingProblem) -> list[ScheduleIssue]:
    issues: list[ScheduleIssue] = []
    operations = {operation.id: operation for operation in problem.operations}
    if len(operations) != len(problem.operations):
        issues.append(
            ScheduleIssue("duplicate_operation", "operations", "operation IDs must be unique")
        )
    if problem.objective not in {"makespan", "lateness"}:
        issues.append(
            ScheduleIssue("objective", "objective", "objective must be makespan or lateness")
        )
    windows_by_resource: dict[str, list[ResourceWindow]] = {}
    for index, window in enumerate(problem.resources):
        windows_by_resource.setdefault(window.resource_id, []).append(window)
        if (
            not all(math.isfinite(value) for value in (window.start, window.end))
            or window.start < 0
            or window.end <= window.start
        ):
            issues.append(
                ScheduleIssue(
                    "invalid_window",
                    f"resources[{index}]",
                    "window must be finite and end after start",
                )
            )
    for operation in problem.operations:
        path = f"operations[{operation.id}]"
        if not operation.id or not math.isfinite(operation.duration) or operation.duration <= 0:
            issues.append(
                ScheduleIssue(
                    "invalid_operation", path, "operation ID and positive duration are required"
                )
            )
        for predecessor in operation.predecessors:
            if predecessor not in operations:
                issues.append(
                    ScheduleIssue(
                        "missing_predecessor",
                        f"{path}.predecessors",
                        f"unknown predecessor {predecessor!r}",
                    )
                )
        readiness = (
            operation.release_time,
            operation.material_ready_time,
            operation.document_ready_time,
        )
        if any(not math.isfinite(value) or value < 0 for value in readiness):
            issues.append(
                ScheduleIssue(
                    "invalid_readiness", path, "readiness times must be finite and non-negative"
                )
            )
        if operation.frozen_start is not None and (
            not math.isfinite(operation.frozen_start)
            or operation.frozen_start < operation.ready_time
        ):
            issues.append(
                ScheduleIssue("frozen_before_ready", path, "frozen start precedes a readiness gate")
            )
        eligible = operation.eligible_resources or tuple(windows_by_resource)
        if not eligible:
            issues.append(ScheduleIssue("no_resource", path, "operation has no eligible resource"))
        qualified = False
        for resource_id in eligible:
            if resource_id not in windows_by_resource:
                issues.append(
                    ScheduleIssue("missing_resource", path, f"unknown resource {resource_id!r}")
                )
            elif any(
                operation.required_qualifications <= window.qualifications
                for window in windows_by_resource[resource_id]
            ):
                qualified = True
        if eligible and not qualified:
            issues.append(
                ScheduleIssue(
                    "unqualified_resource", path, "no eligible window has required qualifications"
                )
            )
        if operation.frozen_resource is not None and operation.frozen_resource not in eligible:
            issues.append(
                ScheduleIssue("frozen_resource_ineligible", path, "frozen resource is not eligible")
            )
    if problem.deadline is not None and (
        not math.isfinite(problem.deadline) or problem.deadline < 0
    ):
        issues.append(
            ScheduleIssue(
                "invalid_deadline", "deadline", "deadline must be finite and non-negative"
            )
        )
    issues.extend(_cycle_issues(operations))
    return issues


def solve(
    problem: SchedulingProblem,
    *,
    solver: Solver | None = None,
    time_limit_seconds: float | None = None,
) -> ScheduleResult:
    """Solve with an injected solver or deterministic earliest-feasible baseline."""
    issues = validate(problem)
    if issues:
        return ScheduleResult("INFEASIBLE", issues=tuple(issues))
    if solver is not None:
        candidate = solver.solve(problem, time_limit_seconds=time_limit_seconds)
        verification = (
            verify_schedule(problem, candidate)
            if candidate.status in {"FEASIBLE", "OPTIMAL"}
            else VerificationResult(True)
        )
        return ScheduleResult(
            candidate.status,
            candidate.starts,
            candidate.ends,
            candidate.resources,
            candidate.objective_value,
            candidate.best_bound,
            verification.valid,
            verification.issues or candidate.issues,
        )
    operations = {operation.id: operation for operation in problem.operations}
    windows = _windows(problem.resources)
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    assigned: dict[str, str] = {}
    frozen = [operation for operation in problem.operations if operation.frozen_start is not None]
    for operation in sorted(frozen, key=lambda item: (item.frozen_start or 0.0, item.id)):
        frozen_start = operation.frozen_start
        if frozen_start is None:
            continue
        placement = _place(
            operation,
            max((ends[parent] for parent in operation.predecessors), default=0.0),
            windows,
            starts,
            ends,
            assigned,
        )
        if placement is None or placement[0] != frozen_start:
            return ScheduleResult(
                "UNKNOWN",
                starts,
                ends,
                assigned,
                issues=(
                    ScheduleIssue(
                        "frozen_conflict",
                        f"operations[{operation.id}]",
                        "frozen assignment cannot be reserved",
                    ),
                ),
            )
        starts[operation.id], assigned[operation.id] = placement
        ends[operation.id] = frozen_start + operation.duration
    remaining = set(operations) - {operation.id for operation in frozen}
    while remaining:
        ready = [
            operation
            for operation_id, operation in operations.items()
            if operation_id in remaining
            and all(parent in ends for parent in operation.predecessors)
        ]
        if not ready:
            return ScheduleResult(
                "UNKNOWN",
                issues=(
                    ScheduleIssue("cycle", "operations", "no precedence-ready operation exists"),
                ),
            )
        for operation in sorted(
            ready,
            key=lambda item: (
                max((ends[parent] for parent in item.predecessors), default=0.0),
                item.id,
            ),
        ):
            placement = _place(
                operation,
                max((ends[parent] for parent in operation.predecessors), default=0.0),
                windows,
                starts,
                ends,
                assigned,
            )
            if placement is None:
                return ScheduleResult(
                    "UNKNOWN",
                    starts,
                    ends,
                    assigned,
                    issues=(
                        ScheduleIssue(
                            "no_feasible_slot",
                            f"operations[{operation.id}]",
                            "no finite qualified window can fit the operation",
                        ),
                    ),
                )
            start, resource_id = placement
            starts[operation.id] = start
            ends[operation.id] = start + operation.duration
            assigned[operation.id] = resource_id
            remaining.remove(operation.id)
    objective_value = _objective(problem, ends)
    result = ScheduleResult("FEASIBLE", starts, ends, assigned, objective_value, None)
    verification = verify_schedule(problem, result)
    return ScheduleResult(
        result.status,
        result.starts,
        result.ends,
        result.resources,
        result.objective_value,
        result.best_bound,
        verification.valid,
        verification.issues,
    )


def verify_schedule(problem: SchedulingProblem, result: ScheduleResult) -> VerificationResult:
    issues = validate(problem)
    known_ids = {operation.id for operation in problem.operations}
    for collection_name, collection in (
        ("starts", result.starts),
        ("ends", result.ends),
        ("resources", result.resources),
    ):
        for operation_id in collection:
            if operation_id not in known_ids:
                issues.append(
                    ScheduleIssue(
                        "unknown_assignment", collection_name, f"unknown operation {operation_id!r}"
                    )
                )
    for operation in problem.operations:
        if (
            operation.id not in result.starts
            or operation.id not in result.ends
            or operation.id not in result.resources
        ):
            issues.append(
                ScheduleIssue(
                    "missing_assignment",
                    f"operations[{operation.id}]",
                    "schedule omits an operation",
                )
            )
            continue
        start, end, resource_id = (
            result.starts[operation.id],
            result.ends[operation.id],
            result.resources[operation.id],
        )
        if not all(math.isfinite(value) for value in (start, end)):
            issues.append(
                ScheduleIssue(
                    "nonfinite_assignment",
                    f"operations[{operation.id}]",
                    "schedule times must be finite",
                )
            )
        if not math.isclose(end - start, operation.duration, rel_tol=1e-9, abs_tol=1e-9):
            issues.append(
                ScheduleIssue(
                    "duration",
                    f"operations[{operation.id}]",
                    "scheduled duration differs from required duration",
                )
            )
        if start < operation.ready_time:
            issues.append(
                ScheduleIssue(
                    "readiness", f"operations[{operation.id}]", "operation starts before readiness"
                )
            )
        if operation.frozen_start is not None and start != operation.frozen_start:
            issues.append(
                ScheduleIssue("frozen_start", f"operations[{operation.id}]", "frozen start changed")
            )
        if operation.frozen_resource is not None and resource_id != operation.frozen_resource:
            issues.append(
                ScheduleIssue(
                    "frozen_resource", f"operations[{operation.id}]", "frozen resource changed"
                )
            )
        eligible = operation.eligible_resources or tuple(
            window.resource_id for window in problem.resources
        )
        if resource_id not in eligible or not any(
            window.start <= start
            and end <= window.end
            and window.resource_id == resource_id
            and operation.required_qualifications <= window.qualifications
            for window in problem.resources
        ):
            issues.append(
                ScheduleIssue(
                    "window",
                    f"operations[{operation.id}]",
                    "operation is outside a qualified finite window",
                )
            )
        for predecessor in operation.predecessors:
            if predecessor in result.ends and start < result.ends[predecessor]:
                issues.append(
                    ScheduleIssue(
                        "precedence",
                        f"operations[{operation.id}]",
                        f"starts before predecessor {predecessor!r} ends",
                    )
                )
    for left in problem.operations:
        for right in problem.operations:
            if left.id >= right.id or result.resources.get(left.id) != result.resources.get(
                right.id
            ):
                continue
            if (
                left.id in result.starts
                and right.id in result.starts
                and left.id in result.ends
                and right.id in result.ends
                and result.starts[left.id] < result.ends[right.id]
                and result.starts[right.id] < result.ends[left.id]
            ):
                issues.append(
                    ScheduleIssue("overlap", "operations", f"{left.id!r} overlaps {right.id!r}")
                )
    return VerificationResult(not issues, tuple(issues))


def rank_candidates(candidates: Iterable[ScheduleResult]) -> list[ScheduleResult]:
    return sorted(
        (
            candidate
            for candidate in candidates
            if candidate.verified and candidate.status in {"FEASIBLE", "OPTIMAL"}
        ),
        key=lambda candidate: (
            candidate.objective_value if candidate.objective_value is not None else float("inf")
        ),
    )


def pareto_frontier(candidates: Iterable[ScheduleResult]) -> list[ScheduleResult]:
    ranked = rank_candidates(candidates)
    frontier: list[ScheduleResult] = []
    for candidate in ranked:
        if not any(_dominates(other, candidate) for other in ranked):
            frontier.append(candidate)
    return frontier


def _dominates(left: ScheduleResult, right: ScheduleResult) -> bool:
    left_objective = left.objective_value if left.objective_value is not None else float("inf")
    right_objective = right.objective_value if right.objective_value is not None else float("inf")
    left_makespan = max(left.ends.values(), default=float("inf"))
    right_makespan = max(right.ends.values(), default=float("inf"))
    return (
        left_objective <= right_objective
        and left_makespan <= right_makespan
        and (left_objective < right_objective or left_makespan < right_makespan)
    )


def paired_deltas(
    baseline: Mapping[str, float], candidate: Mapping[str, float]
) -> dict[str, float]:
    if set(baseline) != set(candidate):
        raise ValueError("paired deltas require aligned operation IDs")
    return {key: candidate[key] - baseline[key] for key in baseline}


def _windows(resources: tuple[ResourceWindow, ...]) -> dict[str, list[ResourceWindow]]:
    grouped: dict[str, list[ResourceWindow]] = {}
    for window in resources:
        grouped.setdefault(window.resource_id, []).append(window)
    return grouped


def _place(
    operation: Operation,
    predecessor_ready: float,
    windows: dict[str, list[ResourceWindow]],
    starts: dict[str, float],
    ends: dict[str, float],
    assigned: dict[str, str],
) -> tuple[float, str] | None:
    eligible = (
        (operation.frozen_resource,)
        if operation.frozen_resource
        else operation.eligible_resources or tuple(windows)
    )
    candidates: list[tuple[float, str]] = []
    for resource_id in eligible:
        for window in windows.get(resource_id, []):
            if not operation.required_qualifications <= window.qualifications:
                continue
            earliest = (
                operation.frozen_start
                if operation.frozen_start is not None
                else max(predecessor_ready, operation.ready_time, window.start)
            )
            if earliest < window.start or earliest + operation.duration > window.end:
                continue
            while True:
                conflicts = [
                    (starts[other_id], ends[other_id])
                    for other_id, assigned_resource in assigned.items()
                    if assigned_resource == resource_id
                    and starts[other_id] < earliest + operation.duration
                    and earliest < ends[other_id]
                ]
                if not conflicts:
                    break
                if operation.frozen_start is not None:
                    earliest = window.end + 1
                    break
                earliest = max(end for _, end in conflicts)
                if earliest + operation.duration > window.end:
                    break
            if earliest + operation.duration > window.end:
                continue
            candidates.append((earliest, resource_id))
    return min(candidates) if candidates else None


def _objective(problem: SchedulingProblem, ends: Mapping[str, float]) -> float:
    makespan = max(ends.values(), default=0.0)
    if problem.objective == "lateness" and problem.deadline is not None:
        return max(0.0, makespan - problem.deadline)
    return makespan


def _cycle_issues(operations: dict[str, Operation]) -> list[ScheduleIssue]:
    visiting: set[str] = set()
    visited: set[str] = set()
    issues: list[ScheduleIssue] = []

    def visit(operation_id: str) -> None:
        if operation_id in visiting:
            issues.append(
                ScheduleIssue(
                    "cycle", f"operations[{operation_id}]", "precedence graph contains a cycle"
                )
            )
            return
        if operation_id in visited:
            return
        visiting.add(operation_id)
        for predecessor in operations[operation_id].predecessors:
            if predecessor in operations:
                visit(predecessor)
        visiting.remove(operation_id)
        visited.add(operation_id)

    for operation_id in operations:
        visit(operation_id)
    return issues
