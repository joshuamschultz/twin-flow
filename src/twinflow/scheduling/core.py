"""Hard-constraint scheduling with an independently checked baseline."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

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
        if window.start < 0 or window.end <= window.start:
            issues.append(
                ScheduleIssue(
                    "invalid_window",
                    f"resources[{index}]",
                    "window must be finite and end after start",
                )
            )
    for operation in problem.operations:
        path = f"operations[{operation.id}]"
        if not operation.id or operation.duration <= 0:
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
        if operation.frozen_start is not None and operation.frozen_start < operation.ready_time:
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
        return solver.solve(problem, time_limit_seconds=time_limit_seconds)
    operations = {operation.id: operation for operation in problem.operations}
    windows = _windows(problem.resources)
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    assigned: dict[str, str] = {}
    remaining = set(operations)
    while remaining:
        ready = [
            operation
            for operation_id, operation in operations.items()
            if operation_id in remaining
            and all(parent in ends for parent in operation.predecessors)
        ]
        if not ready:
            return ScheduleResult(
                "INFEASIBLE",
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
                    "INFEASIBLE",
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
        if end - start != operation.duration:
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
        if not any(
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
        makespan = max(candidate.ends.values(), default=0.0)
        if not any(
            (other.objective_value or 0) <= (candidate.objective_value or 0)
            and max(other.ends.values(), default=0.0) <= makespan
            and other != candidate
            for other in ranked
        ):
            frontier.append(candidate)
    return frontier


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
            conflicts = sorted(
                (
                    ends[other_id]
                    for other_id, assigned_resource in assigned.items()
                    if assigned_resource == resource_id
                    and starts[other_id] < earliest + operation.duration
                    and earliest < ends[other_id]
                )
            )
            if conflicts:
                if operation.frozen_start is not None:
                    continue
                earliest = max(conflicts)
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
