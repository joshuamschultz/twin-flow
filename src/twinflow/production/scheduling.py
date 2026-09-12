"""Deterministic rich production scheduler with independent verification."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import replace

from .contracts import (
    BatchAssignment,
    JobResult,
    OccupationKind,
    OperationAssignment,
    Phase,
    PhaseSegment,
    ProductionIssue,
    ProductionOperation,
    ProductionProblem,
    ProductionSchedule,
    ProductionVerification,
    RecipeAlternative,
    Resource,
    ResourceOccupation,
    ResourceUse,
    ScheduleComparison,
    ThermalRecipe,
)
from .validation import validate_problem

_EPSILON = 1e-9


def validate(problem: ProductionProblem) -> list[ProductionIssue]:
    """Return all structural problems without mutating the input."""

    return validate_problem(problem)


def solve(problem: ProductionProblem, *, solver: str = "baseline") -> ProductionSchedule:
    """Build an earliest-feasible deterministic schedule for the rich contract."""

    if solver != "baseline":
        return ProductionSchedule(
            "UNKNOWN",
            issues=(ProductionIssue("solver", "solver", f"unsupported rich solver {solver!r}"),),
            solver=solver,
        )
    issues = validate(problem)
    if issues:
        return ProductionSchedule("INFEASIBLE", issues=tuple(issues), solver=solver)

    reservations: list[ResourceOccupation] = []
    assignments: dict[str, OperationAssignment] = {}
    batches: list[BatchAssignment] = []
    remaining = {operation.id for operation in problem.operations}
    operations = {operation.id: operation for operation in problem.operations}
    while remaining:
        ready_operations = [
            operations[operation_id]
            for operation_id in remaining
            if all(parent in assignments for parent in operations[operation_id].predecessors)
        ]
        if not ready_operations:
            return ProductionSchedule(
                "INFEASIBLE",
                assignments=tuple(assignments.values()),
                batches=tuple(batches),
                issues=(ProductionIssue("precedence", "operations", "no operation is ready"),),
                solver=solver,
            )
        processed: set[str] = set()
        for operation in sorted(ready_operations, key=lambda item: item.id):
            if operation.id in processed:
                continue
            predecessor_end = max(
                (assignments[parent].end for parent in operation.predecessors), default=0.0
            )
            first_alternative = operation.alternatives[0]
            if first_alternative.batch is not None:
                group = _ready_batch_group(operation, ready_operations, processed, problem)
                batch_result = _place_batch_group(
                    problem, group, assignments, reservations, len(batches) + 1
                )
                if batch_result is None:
                    assignment = None
                else:
                    batch, batch_assignments = batch_result
                    batches.append(batch)
                    for item in batch_assignments:
                        assignments[item.operation_id] = item
                        processed.add(item.operation_id)
                    reservations.append(
                        ResourceOccupation(batch.id, batch.resource_id, batch.start, batch.end)
                    )
                    assignment = batch_assignments[0]
            else:
                assignment = _place_operation(
                    problem,
                    operation,
                    max(operation.release_time, predecessor_end),
                    reservations,
                )
                if assignment is not None:
                    assignments[operation.id] = assignment
                    reservations.extend(assignment.occupations)
                    processed.add(operation.id)
            if assignment is None:
                return ProductionSchedule(
                    "UNKNOWN",
                    assignments=tuple(assignments.values()),
                    batches=tuple(batches),
                    issues=(
                        ProductionIssue(
                            "no_feasible_slot",
                            f"operations[{operation.id}]",
                            "no alternative fits the declared resources",
                        ),
                    ),
                    solver=solver,
                )
        remaining -= processed

    ordered = tuple(assignments[item.id] for item in problem.operations)
    job_results = _job_results(problem, ordered)
    objective = _objective(problem, ordered, job_results)
    on_time = _on_time_fraction(job_results)
    candidate = ProductionSchedule(
        "FEASIBLE",
        ordered,
        tuple(batches),
        job_results,
        objective,
        on_time,
        problem.cohort,
        False,
        (),
        solver,
    )
    verification = verify(problem, candidate)
    return replace(
        candidate,
        status="FEASIBLE" if verification.valid else "UNKNOWN",
        verified=verification.valid,
        issues=verification.issues,
    )


def verify(problem: ProductionProblem, schedule: ProductionSchedule) -> ProductionVerification:
    """Recompute constraints from the problem and reject forged schedule metadata."""

    issues = validate(problem)
    operations = {item.id: item for item in problem.operations}
    assignment_ids = [item.operation_id for item in schedule.assignments]
    if len(set(assignment_ids)) != len(assignment_ids):
        issues.append(
            ProductionIssue("duplicate_assignment", "assignments", "operation assigned twice")
        )
    expected_order = [item.id for item in problem.operations]
    if assignment_ids != expected_order:
        issues.append(
            ProductionIssue("assignment_order", "assignments", "assignment order differs")
        )
    assignments = {item.operation_id: item for item in schedule.assignments}
    for operation_id in assignments.keys() - operations.keys():
        issues.append(ProductionIssue("unknown_assignment", "assignments", operation_id))
    for operation in problem.operations:
        assignment = assignments.get(operation.id)
        if assignment is None:
            issues.append(
                ProductionIssue("missing_assignment", f"operations[{operation.id}]", "omitted")
            )
            continue
        alternative = next(
            (item for item in operation.alternatives if item.id == assignment.alternative_id), None
        )
        if alternative is None:
            issues.append(
                ProductionIssue("alternative", f"operations[{operation.id}]", "unknown choice")
            )
            continue
        if assignment.recipe_revision != alternative.revision:
            issues.append(
                ProductionIssue(
                    "recipe_revision", f"operations[{operation.id}]", "revision differs"
                )
            )
        if assignment.lot_ids != operation.lot_ids:
            issues.append(
                ProductionIssue("lot_identity", f"operations[{operation.id}]", "lot IDs differ")
            )
        if not _finite(assignment.start, assignment.end) or assignment.end < assignment.start:
            issues.append(
                ProductionIssue("assignment_time", f"operations[{operation.id}]", "invalid time")
            )
        if assignment.start < operation.release_time - _EPSILON:
            issues.append(
                ProductionIssue("release_time", f"operations[{operation.id}]", "starts early")
            )
        if assignment.segments and (
            not math.isclose(
                assignment.start,
                min(item.start for item in assignment.segments),
                abs_tol=_EPSILON,
            )
            or not math.isclose(
                assignment.end,
                max(item.end for item in assignment.segments),
                abs_tol=_EPSILON,
            )
        ):
            issues.append(
                ProductionIssue("assignment_bounds", f"operations[{operation.id}]", "bounds differ")
            )
        _verify_phases(operation, alternative, assignment, issues)
        _verify_uses(problem, operation, alternative, assignment, issues)
        for parent in operation.predecessors:
            predecessor = assignments.get(parent)
            if predecessor is not None and assignment.start < predecessor.end - _EPSILON:
                issues.append(
                    ProductionIssue("precedence", f"operations[{operation.id}]", "starts early")
                )
    _verify_resource_capacity(problem, schedule, issues)
    _verify_batches(problem, schedule, issues)
    expected_jobs = _job_results(problem, schedule.assignments)
    if schedule.job_results != expected_jobs:
        issues.append(ProductionIssue("job_tardiness", "job_results", "job results differ"))
    expected_objective = _objective(problem, schedule.assignments, expected_jobs)
    if not _optional_close(schedule.objective_value, expected_objective):
        issues.append(ProductionIssue("objective", "objective_value", "objective differs"))
    if schedule.cohort != problem.cohort:
        issues.append(ProductionIssue("cohort", "cohort", "cohort differs from problem"))
    expected_service = _on_time_fraction(expected_jobs)
    if not _optional_close(schedule.customer_on_time_fraction, expected_service):
        issues.append(
            ProductionIssue("service_metric", "customer_on_time_fraction", "metric differs")
        )
    if schedule.status not in {"FEASIBLE", "OPTIMAL"}:
        issues.append(ProductionIssue("status", "status", "assigned schedule is not feasible"))
    return ProductionVerification(not issues, tuple(issues))


def compare_schedules(left: ProductionSchedule, right: ProductionSchedule) -> ScheduleComparison:
    """Compare results only when their explicit scenario cohorts match."""

    if left.cohort != right.cohort:
        return ScheduleComparison(
            False,
            (ProductionIssue("cohort_mismatch", "cohort", "schedule cohorts differ"),),
        )
    delta = None
    if left.objective_value is not None and right.objective_value is not None:
        delta = right.objective_value - left.objective_value
    return ScheduleComparison(True, objective_delta=delta)


def _place_operation(
    problem: ProductionProblem,
    operation: ProductionOperation,
    ready: float,
    reservations: list[ResourceOccupation],
) -> OperationAssignment | None:
    alternatives = sorted(
        operation.alternatives,
        key=lambda item: (
            item.preference_cost,
            sum(phase.duration for phase in item.phases),
            item.id,
        ),
    )
    for alternative in alternatives:
        if alternative.batch is not None:
            continue
        candidate = ready
        for _ in range(10_000):
            placed = _simulate_phases(problem, operation, alternative, candidate, reservations)
            if placed is None:
                break
            segments, occupations = placed
            conflicts = _conflicts(
                occupations,
                reservations,
                {item.id: item for item in problem.resources},
            )
            if not conflicts:
                return OperationAssignment(
                    operation.id,
                    operation.order_id,
                    alternative.id,
                    alternative.revision,
                    min(segment.start for segment in segments),
                    max(segment.end for segment in segments),
                    tuple(segments),
                    tuple(occupations),
                    operation.lot_ids,
                )
            candidate = max(candidate + _EPSILON, min(item.end for item in conflicts))
    return None


def _simulate_phases(
    problem: ProductionProblem,
    operation: ProductionOperation,
    alternative: RecipeAlternative,
    start: float,
    reservations: list[ResourceOccupation],
) -> tuple[list[PhaseSegment], list[ResourceOccupation]] | None:
    resources = {item.id: item for item in problem.resources}
    phases = list(alternative.phases)
    segments: list[PhaseSegment] = []
    cursor = start
    restart_occupations: list[ResourceOccupation] = []
    for index, phase in enumerate(phases):
        active_uses = [use for use in alternative.uses if _use_contains(use, phases, index)]
        resource_quantities: dict[str, int] = {}
        for use in active_uses:
            for resource_id in use.resource_ids:
                resource_quantities[resource_id] = (
                    resource_quantities.get(resource_id, 0) + use.quantity
                )
        resource_ids = tuple(resource_quantities)
        if phase.interruptible:
            placed = _place_interruptible(
                operation.id,
                phase,
                cursor,
                resource_ids,
                resources,
                reservations,
                resource_quantities,
            )
            if placed is None:
                return None
            phase_segments, restart_uses = placed
            segments.extend(phase_segments)
            restart_occupations.extend(restart_uses)
            cursor = max(segment.end for segment in phase_segments)
        else:
            phase_start = _next_contiguous_slot(
                cursor,
                phase.duration,
                resource_ids,
                resources,
                reservations,
                resource_quantities=resource_quantities,
            )
            if phase_start is None:
                return None
            segments.append(
                PhaseSegment(operation.id, phase.id, phase_start, phase_start + phase.duration)
            )
            cursor = phase_start + phase.duration
    occupations = _occupations_from_uses(operation.id, alternative, phases, segments)
    occupations.extend(restart_occupations)
    return segments, occupations


def _place_interruptible(
    operation_id: str,
    phase: Phase,
    start: float,
    resource_ids: tuple[str, ...],
    resources: dict[str, Resource],
    reservations: list[ResourceOccupation],
    resource_quantities: dict[str, int],
) -> tuple[list[PhaseSegment], list[ResourceOccupation]] | None:
    remaining = phase.duration
    cursor = start
    segments: list[PhaseSegment] = []
    restart_uses: list[ResourceOccupation] = []
    while remaining > _EPSILON:
        interval = _next_shared_interval(
            cursor,
            resource_ids,
            resources,
            reservations,
            resource_quantities=resource_quantities,
        )
        if interval is None:
            return None
        available_start, available_end = interval
        if segments and phase.restart_rule is not None:
            idle = available_start - segments[-1].end
            rule = phase.restart_rule
            if idle >= rule.idle_threshold - _EPSILON:
                restart_start = _next_contiguous_slot(
                    available_start,
                    rule.duration,
                    rule.resource_ids,
                    resources,
                    reservations,
                    qualifications=rule.qualifications,
                    resource_quantities={
                        resource_id: rule.quantity for resource_id in rule.resource_ids
                    },
                )
                if restart_start is None:
                    return None
                restart_end = restart_start + rule.duration
                if restart_end > available_end + _EPSILON:
                    cursor = available_end
                    continue
                segments.append(
                    PhaseSegment(operation_id, phase.id, restart_start, restart_end, "restart")
                )
                restart_uses.extend(
                    ResourceOccupation(
                        operation_id,
                        resource_id,
                        restart_start,
                        restart_end,
                        rule.quantity,
                        "restart",
                    )
                    for resource_id in rule.resource_ids
                )
                available_start = restart_end
        amount = min(remaining, available_end - available_start)
        if amount <= _EPSILON:
            cursor = available_end + _EPSILON
            continue
        segments.append(
            PhaseSegment(operation_id, phase.id, available_start, available_start + amount)
        )
        remaining -= amount
        cursor = available_start + amount
        if remaining > _EPSILON:
            cursor = available_end + _EPSILON
    return segments, restart_uses


def _next_contiguous_slot(
    start: float,
    duration: float,
    resource_ids: tuple[str, ...],
    resources: dict[str, Resource],
    reservations: list[ResourceOccupation],
    *,
    qualifications: frozenset[str] = frozenset(),
    resource_quantities: dict[str, int] | None = None,
) -> float | None:
    candidate = start
    for _ in range(10_000):
        interval = _next_shared_interval(
            candidate,
            resource_ids,
            resources,
            reservations,
            qualifications=qualifications,
            resource_quantities=resource_quantities,
        )
        if interval is None:
            return None
        interval_start, interval_end = interval
        if interval_start + duration <= interval_end + _EPSILON:
            return interval_start
        candidate = interval_end + _EPSILON
    return None


def _next_shared_interval(
    start: float,
    resource_ids: tuple[str, ...],
    resources: dict[str, Resource],
    reservations: list[ResourceOccupation],
    *,
    qualifications: frozenset[str] = frozenset(),
    resource_quantities: dict[str, int] | None = None,
) -> tuple[float, float] | None:
    if not resource_ids:
        return (start, math.inf)
    candidates = {start}
    quantities = resource_quantities or {resource_id: 1 for resource_id in resource_ids}
    for resource_id in resource_ids:
        resource = resources[resource_id]
        if not qualifications <= resource.qualifications:
            return None
        candidates.update(window.start for window in resource.windows if window.end >= start)
        candidates.update(
            item.end
            for item in reservations
            if item.resource_id == resource_id and item.end >= start
        )
    for candidate in sorted(candidates):
        if candidate < start - _EPSILON:
            continue
        ends: list[float] = []
        valid = True
        for resource_id in resource_ids:
            resource = resources[resource_id]
            window = next(
                (
                    item
                    for item in resource.windows
                    if item.start <= candidate + _EPSILON and candidate < item.end - _EPSILON
                ),
                None,
            )
            if window is None:
                valid = False
                break
            active = [
                item
                for item in reservations
                if item.resource_id == resource_id
                and item.start <= candidate + _EPSILON
                and candidate < item.end - _EPSILON
            ]
            required_quantity = quantities.get(resource_id, 1)
            if sum(item.quantity for item in active) + required_quantity > resource.capacity:
                valid = False
                break
            next_conflict = math.inf
            future_starts = sorted(
                {
                    item.start
                    for item in reservations
                    if item.resource_id == resource_id and item.start > candidate
                }
            )
            for future in future_starts:
                future_active = sum(
                    item.quantity
                    for item in reservations
                    if item.resource_id == resource_id
                    and item.start <= future + _EPSILON
                    and future < item.end - _EPSILON
                )
                if future_active + required_quantity > resource.capacity:
                    next_conflict = future
                    break
            ends.append(min(window.end, next_conflict))
        if valid:
            return candidate, min(ends)
    return None


def _occupations_from_uses(
    operation_id: str,
    alternative: RecipeAlternative,
    phases: list[Phase],
    segments: list[PhaseSegment],
) -> list[ResourceOccupation]:
    result: list[ResourceOccupation] = []
    for use in alternative.uses:
        relevant = [
            segment
            for segment in segments
            if segment.kind == "work"
            and _phase_between(segment.phase_id, use.start_phase, use.end_phase, phases)
        ]
        if not relevant:
            continue
        interrupted = len(relevant) > len({segment.phase_id for segment in relevant})
        kind: OccupationKind = (
            "hold"
            if use.start_phase != use.end_phase or (interrupted and use.hold_during_pause)
            else "productive"
        )
        for resource_id in use.resource_ids:
            if interrupted and not use.hold_during_pause:
                result.extend(
                    ResourceOccupation(
                        operation_id, resource_id, segment.start, segment.end, use.quantity
                    )
                    for segment in relevant
                )
            else:
                result.append(
                    ResourceOccupation(
                        operation_id,
                        resource_id,
                        min(segment.start for segment in relevant),
                        max(segment.end for segment in relevant),
                        use.quantity,
                        kind,
                    )
                )
    return result


def _use_contains(use: ResourceUse, phases: list[Phase], phase_index: int) -> bool:
    ids = [item.id for item in phases]
    return ids.index(use.start_phase) <= phase_index <= ids.index(use.end_phase)


def _phase_between(phase_id: str, start: str, end: str, phases: list[Phase]) -> bool:
    ids = [item.id for item in phases]
    return ids.index(start) <= ids.index(phase_id) <= ids.index(end)


def _conflicts(
    proposed: Iterable[ResourceOccupation],
    existing: Iterable[ResourceOccupation],
    resources: dict[str, Resource],
) -> list[ResourceOccupation]:
    conflicts: list[ResourceOccupation] = []
    for new in proposed:
        overlap = [
            old
            for old in existing
            if new.resource_id == old.resource_id
            and new.start < old.end - _EPSILON
            and old.start < new.end - _EPSILON
        ]
        if (
            new.quantity + sum(item.quantity for item in overlap)
            > resources[new.resource_id].capacity
        ):
            conflicts.extend(overlap)
    return conflicts


def _ready_batch_group(
    operation: ProductionOperation,
    ready_operations: list[ProductionOperation],
    processed: set[str],
    problem: ProductionProblem,
) -> list[tuple[ProductionOperation, RecipeAlternative]]:
    alternative = operation.alternatives[0]
    requirement = alternative.batch
    if requirement is None:
        raise RuntimeError("validated batch alternative is missing its requirement")
    recipe = next(
        item for item in problem.thermal_recipes if item.id == requirement.thermal_recipe_id
    )
    group = [(operation, alternative)]
    for candidate in ready_operations:
        if candidate.id == operation.id or candidate.id in processed:
            continue
        candidate_alternative = candidate.alternatives[0]
        if candidate_alternative.batch is None:
            continue
        if _batch_group_fits(
            group,
            candidate_alternative,
            recipe.capacity,
            {item.id: item for item in problem.thermal_recipes},
        ):
            group.append((candidate, candidate_alternative))
    return group


def _place_batch_group(
    problem: ProductionProblem,
    group: list[tuple[ProductionOperation, RecipeAlternative]],
    assignments: dict[str, OperationAssignment],
    reservations: list[ResourceOccupation],
    index: int,
) -> tuple[BatchAssignment, list[OperationAssignment]] | None:
    operation, alternative = group[0]
    requirement = alternative.batch
    if requirement is None:
        raise RuntimeError("validated batch group is missing its requirement")
    recipes = {item.id: item for item in problem.thermal_recipes}
    recipe = recipes[requirement.thermal_recipe_id]
    duration = recipe.warmup_duration + recipe.hold_duration + recipe.cooldown_duration
    ready = max(
        max(
            member.release_time,
            max((assignments[parent].end for parent in member.predecessors), default=0.0),
        )
        for member, _ in group
    )
    resource_id = alternative.primary_resource_id
    start = _next_contiguous_slot(
        ready,
        duration,
        (resource_id,),
        {item.id: item for item in problem.resources},
        reservations,
    )
    if start is None:
        return None
    end = start + duration
    member_ids = tuple(member.id for member, _ in group)
    total = sum(item.batch.quantity for _, item in group if item.batch is not None)
    batch = BatchAssignment(
        f"batch-{index}",
        recipe.id,
        resource_id,
        member_ids,
        total,
        requirement.uom,
        start,
        end,
    )
    member_assignments = [
        OperationAssignment(
            member.id,
            member.order_id,
            member_alternative.id,
            member_alternative.revision,
            start,
            end,
            (PhaseSegment(member.id, "thermal", start, end),),
            (ResourceOccupation(member.id, resource_id, start, end),),
            member.lot_ids,
        )
        for member, member_alternative in group
    ]
    return batch, member_assignments


def _batch_group_fits(
    group: list[tuple[ProductionOperation, RecipeAlternative]],
    alternative: RecipeAlternative,
    capacity: float,
    recipes: dict[str, ThermalRecipe],
) -> bool:
    requirement = alternative.batch
    first = group[0][1].batch
    if requirement is None or first is None:
        raise RuntimeError("validated batch group is missing its requirement")
    first_recipe = recipes[first.thermal_recipe_id]
    candidate_recipe = recipes[requirement.thermal_recipe_id]
    first_key = first_recipe.compatibility_key or first_recipe.id
    candidate_key = candidate_recipe.compatibility_key or candidate_recipe.id
    current = sum(item.batch.quantity for _, item in group if item.batch is not None)
    return (
        first_key == candidate_key
        and group[0][1].primary_resource_id == alternative.primary_resource_id
        and first.uom == requirement.uom
        and current + requirement.quantity <= capacity + _EPSILON
    )


def _verify_phases(
    operation: ProductionOperation,
    alternative: RecipeAlternative,
    assignment: OperationAssignment,
    issues: list[ProductionIssue],
) -> None:
    if alternative.batch is not None:
        return
    for phase in alternative.phases:
        work = [
            item
            for item in assignment.segments
            if item.phase_id == phase.id and item.kind == "work"
        ]
        duration = sum(item.end - item.start for item in work)
        if not math.isclose(duration, phase.duration, abs_tol=_EPSILON):
            issues.append(
                ProductionIssue("phase_duration", f"operations[{operation.id}]", phase.id)
            )
        if not phase.interruptible and len(work) != 1:
            issues.append(
                ProductionIssue("noninterruptible_split", f"operations[{operation.id}]", phase.id)
            )
        if phase.interruptible and phase.restart_rule is not None:
            ordered_work = sorted(work, key=lambda item: item.start)
            qualified_pauses = sum(
                right.start - left.end >= phase.restart_rule.idle_threshold - _EPSILON
                for left, right in zip(ordered_work, ordered_work[1:], strict=False)
            )
            restarts = [
                item
                for item in assignment.segments
                if item.phase_id == phase.id and item.kind == "restart"
            ]
            if len(restarts) != qualified_pauses or any(
                not math.isclose(
                    item.end - item.start, phase.restart_rule.duration, abs_tol=_EPSILON
                )
                for item in restarts
            ):
                issues.append(ProductionIssue("restart", f"operations[{operation.id}]", phase.id))


def _verify_uses(
    problem: ProductionProblem,
    operation: ProductionOperation,
    alternative: RecipeAlternative,
    assignment: OperationAssignment,
    issues: list[ProductionIssue],
) -> None:
    resources = {item.id: item for item in problem.resources}
    phases = list(alternative.phases)
    for use in alternative.uses:
        relevant = [
            item
            for item in assignment.segments
            if item.kind == "work"
            and _phase_between(item.phase_id, use.start_phase, use.end_phase, phases)
        ]
        if not relevant:
            continue
        required_start = min(item.start for item in relevant)
        required_end = max(item.end for item in relevant)
        for resource_id in use.resource_ids:
            occupations = [
                item for item in assignment.occupations if item.resource_id == resource_id
            ]
            coverage_intervals = [(item.start, item.end) for item in occupations]
            required_intervals = (
                [(required_start, required_end)]
                if use.hold_during_pause
                else [(item.start, item.end) for item in relevant]
            )
            if not occupations or any(
                not _covers(coverage_intervals, start, end) for start, end in required_intervals
            ):
                issues.append(
                    ProductionIssue(
                        "missing_resource_occupation",
                        f"operations[{operation.id}]",
                        resource_id,
                    )
                )
            resource = resources[resource_id]
            for segment in relevant:
                if not any(
                    window.start <= segment.start + _EPSILON
                    and segment.end <= window.end + _EPSILON
                    for window in resource.windows
                ):
                    issues.append(
                        ProductionIssue(
                            "resource_window", f"operations[{operation.id}]", resource_id
                        )
                    )


def _verify_resource_capacity(
    problem: ProductionProblem,
    schedule: ProductionSchedule,
    issues: list[ProductionIssue],
) -> None:
    resources = {item.id: item for item in problem.resources}
    same_batch = {
        frozenset((left, right))
        for batch in schedule.batches
        for left in batch.member_operation_ids
        for right in batch.member_operation_ids
    }
    occupations = [item for assignment in schedule.assignments for item in assignment.occupations]
    for resource_id, resource in resources.items():
        relevant = [item for item in occupations if item.resource_id == resource_id]
        points = sorted({point for item in relevant for point in (item.start, item.end)})
        for left, right in zip(points, points[1:], strict=False):
            if right <= left + _EPSILON:
                continue
            active = [
                item
                for item in relevant
                if item.start < right - _EPSILON and left < item.end - _EPSILON
            ]
            counted: list[ResourceOccupation] = []
            for item in active:
                if any(
                    frozenset((item.operation_id, prior.operation_id)) in same_batch
                    for prior in counted
                ):
                    continue
                counted.append(item)
            if sum(item.quantity for item in counted) > resource.capacity:
                issues.append(ProductionIssue("resource_overlap", "assignments", resource_id))
                break


def _verify_batches(
    problem: ProductionProblem,
    schedule: ProductionSchedule,
    issues: list[ProductionIssue],
) -> None:
    operations = {item.id: item for item in problem.operations}
    recipes = {item.id: item for item in problem.thermal_recipes}
    batch_operation_ids = {
        operation.id
        for operation in problem.operations
        if any(alternative.batch is not None for alternative in operation.alternatives)
    }
    seen_members: list[str] = []
    for batch in schedule.batches:
        requirements = []
        keys = set()
        for operation_id in batch.member_operation_ids:
            operation = operations.get(operation_id)
            if operation is None:
                issues.append(ProductionIssue("batch_member", "batches", operation_id))
                continue
            assignment = next(
                (item for item in schedule.assignments if item.operation_id == operation_id), None
            )
            if assignment is None:
                continue
            alternative = next(
                item for item in operation.alternatives if item.id == assignment.alternative_id
            )
            requirement = alternative.batch
            if requirement is None:
                issues.append(ProductionIssue("batch_member", "batches", operation_id))
                continue
            requirements.append(requirement)
            seen_members.append(operation_id)
            recipe = recipes[requirement.thermal_recipe_id]
            keys.add(recipe.compatibility_key or recipe.id)
            expected_duration = (
                recipe.warmup_duration + recipe.hold_duration + recipe.cooldown_duration
            )
            if not math.isclose(batch.end - batch.start, expected_duration, abs_tol=_EPSILON):
                issues.append(ProductionIssue("batch_duration", "batches", batch.id))
            if (
                assignment.start != batch.start
                or assignment.end != batch.end
                or alternative.primary_resource_id != batch.resource_id
                or not _covers(
                    [
                        (item.start, item.end)
                        for item in assignment.occupations
                        if item.resource_id == batch.resource_id
                    ],
                    batch.start,
                    batch.end,
                )
            ):
                issues.append(
                    ProductionIssue(
                        "missing_resource_occupation",
                        f"operations[{operation_id}]",
                        batch.resource_id,
                    )
                )
        actual_total = sum(item.quantity for item in requirements)
        if not math.isclose(batch.total_quantity, actual_total, abs_tol=_EPSILON):
            issues.append(ProductionIssue("batch_quantity", "batches", batch.id))
        batch_recipe = recipes.get(batch.recipe_id)
        if batch_recipe is not None and (
            batch.total_quantity > batch_recipe.capacity + _EPSILON
            or actual_total > batch_recipe.capacity + _EPSILON
        ):
            issues.append(ProductionIssue("batch_capacity", "batches", batch.id))
        if len(keys) > 1:
            issues.append(ProductionIssue("batch_compatibility", "batches", batch.id))
        resource = next((item for item in problem.resources if item.id == batch.resource_id), None)
        if resource is None or not any(
            window.start <= batch.start + _EPSILON and batch.end <= window.end + _EPSILON
            for window in resource.windows
        ):
            issues.append(ProductionIssue("batch_window", "batches", batch.id))
    if set(seen_members) != batch_operation_ids or len(seen_members) != len(set(seen_members)):
        issues.append(ProductionIssue("batch_membership", "batches", "batch membership incomplete"))


def _covers(intervals: list[tuple[float, float]], start: float, end: float) -> bool:
    cursor = start
    for left, right in sorted(intervals):
        if right <= cursor + _EPSILON:
            continue
        if left > cursor + _EPSILON:
            return False
        cursor = max(cursor, right)
        if cursor >= end - _EPSILON:
            return True
    return cursor >= end - _EPSILON


def _job_results(
    problem: ProductionProblem, assignments: Iterable[OperationAssignment]
) -> tuple[JobResult, ...]:
    ends = {item.operation_id: item.end for item in assignments}
    result: list[JobResult] = []
    for job in problem.jobs:
        completion = max((ends.get(item, 0.0) for item in job.terminal_operation_ids), default=0.0)
        customer = job.demand_kind == "customer"
        tardiness = (
            max(0.0, completion - job.promised_ship_time)
            if customer and job.promised_ship_time is not None
            else None
        )
        result.append(
            JobResult(
                job.id,
                completion,
                tardiness,
                tardiness <= _EPSILON if tardiness is not None else None,
                customer,
            )
        )
    return tuple(result)


def _objective(
    problem: ProductionProblem,
    assignments: Iterable[OperationAssignment],
    jobs: tuple[JobResult, ...],
) -> float:
    if problem.objective == "weighted_tardiness":
        priorities = {item.id: item.priority for item in problem.jobs}
        return sum((item.tardiness or 0.0) * priorities[item.job_id] for item in jobs)
    return max((item.end for item in assignments), default=0.0)


def _on_time_fraction(jobs: tuple[JobResult, ...]) -> float | None:
    customer = [item for item in jobs if item.customer_demand and item.on_time is not None]
    if not customer:
        return None
    return sum(item.on_time is True for item in customer) / len(customer)


def _optional_close(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, abs_tol=_EPSILON)


def _finite(*values: float) -> bool:
    return all(math.isfinite(value) for value in values)
