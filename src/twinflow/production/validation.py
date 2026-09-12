"""Validation helpers for rich production problems."""

from __future__ import annotations

import math

from .contracts import ProductionIssue, ProductionProblem


def validate_problem(problem: ProductionProblem) -> list[ProductionIssue]:
    issues: list[ProductionIssue] = []
    resources = {item.id: item for item in problem.resources}
    operations = {item.id: item for item in problem.operations}
    recipes = {item.id: item for item in problem.thermal_recipes}
    jobs = {item.id: item for item in problem.jobs}
    if len(resources) != len(problem.resources):
        issues.append(ProductionIssue("duplicate_resource", "resources", "resource IDs differ"))
    if len(operations) != len(problem.operations):
        issues.append(ProductionIssue("duplicate_operation", "operations", "operation IDs differ"))
    if len(recipes) != len(problem.thermal_recipes):
        issues.append(ProductionIssue("duplicate_recipe", "thermal_recipes", "recipe IDs differ"))
    if len(jobs) != len(problem.jobs):
        issues.append(ProductionIssue("duplicate_job", "jobs", "job IDs differ"))
    if problem.time_unit not in {"minutes", "seconds"}:
        issues.append(ProductionIssue("time_unit", "time_unit", "unsupported time unit"))
    if problem.objective not in {"makespan", "weighted_tardiness"}:
        issues.append(ProductionIssue("objective", "objective", "unsupported objective"))
    issues.extend(_validate_recipes(problem))
    for index, resource in enumerate(problem.resources):
        path = f"resources[{index}]"
        if (
            not resource.id
            or isinstance(resource.capacity, bool)
            or not isinstance(resource.capacity, int)
            or resource.capacity < 1
            or not resource.windows
        ):
            issues.append(ProductionIssue("resource", path, "resource ID and capacity required"))
        for window in resource.windows:
            if (
                not _finite(window.start, window.end)
                or window.start < 0
                or window.end <= window.start
            ):
                issues.append(ProductionIssue("window", path, "invalid resource window"))
    for index, operation in enumerate(problem.operations):
        path = f"operations[{index}]"
        if not operation.id or not operation.order_id or not operation.alternatives:
            issues.append(
                ProductionIssue("operation", path, "operation identity and alternatives required")
            )
        if not _finite(operation.release_time) or operation.release_time < 0:
            issues.append(ProductionIssue("operation", path, "release time must be non-negative"))
        if any(parent not in operations for parent in operation.predecessors):
            issues.append(ProductionIssue("predecessor", path, "unknown predecessor"))
        alternative_ids = [item.id for item in operation.alternatives]
        if len(set(alternative_ids)) != len(alternative_ids):
            issues.append(ProductionIssue("alternative", path, "alternative IDs must be unique"))
        for alternative in operation.alternatives:
            phase_ids = [phase.id for phase in alternative.phases]
            if len(set(phase_ids)) != len(phase_ids):
                issues.append(ProductionIssue("phase", path, "phase IDs must be unique"))
            if (
                not alternative.id
                or not alternative.revision
                or alternative.primary_resource_id not in resources
            ):
                issues.append(ProductionIssue("resource", path, "unknown primary resource"))
            if not _finite(alternative.preference_cost) or alternative.preference_cost < 0:
                issues.append(ProductionIssue("alternative", path, "invalid preference cost"))
            for phase in alternative.phases:
                if not phase.id or not _finite(phase.duration) or phase.duration <= 0:
                    issues.append(ProductionIssue("phase", path, "phase duration must be positive"))
                rule = phase.restart_rule
                if rule is not None:
                    if (
                        not phase.interruptible
                        or not _finite(rule.idle_threshold, rule.duration)
                        or rule.idle_threshold < 0
                        or rule.duration <= 0
                        or isinstance(rule.quantity, bool)
                        or not isinstance(rule.quantity, int)
                        or rule.quantity < 1
                        or not rule.resource_ids
                    ):
                        issues.append(ProductionIssue("restart", path, "invalid restart rule"))
                    for resource_id in rule.resource_ids:
                        restart_resource = resources.get(resource_id)
                        if restart_resource is None:
                            issues.append(
                                ProductionIssue("restart", path, "unknown restart resource")
                            )
                        elif not rule.qualifications <= restart_resource.qualifications:
                            issues.append(
                                ProductionIssue(
                                    "qualification_mismatch",
                                    path,
                                    "restart resource lacks qualification",
                                )
                            )
            for use in alternative.uses:
                if (
                    not use.resource_ids
                    or isinstance(use.quantity, bool)
                    or not isinstance(use.quantity, int)
                    or use.quantity < 1
                ):
                    issues.append(ProductionIssue("resource_use", path, "invalid resource use"))
                if use.start_phase not in phase_ids or use.end_phase not in phase_ids:
                    issues.append(
                        ProductionIssue("phase_span", path, "resource use names unknown phase")
                    )
                    continue
                if phase_ids.index(use.start_phase) > phase_ids.index(use.end_phase):
                    issues.append(
                        ProductionIssue("phase_span", path, "resource phase span is reversed")
                    )
                for resource_id in use.resource_ids:
                    used_resource = resources.get(resource_id)
                    if used_resource is None:
                        issues.append(ProductionIssue("resource", path, "resource use is unknown"))
                    elif not use.qualifications <= used_resource.qualifications:
                        issues.append(
                            ProductionIssue(
                                "qualification_mismatch",
                                path,
                                "resource lacks required qualification",
                            )
                        )
            primary_used = any(
                alternative.primary_resource_id in use.resource_ids for use in alternative.uses
            )
            if alternative.batch is None and not primary_used:
                issues.append(
                    ProductionIssue("primary_occupation", path, "primary resource is not occupied")
                )
            if alternative.batch is not None:
                batch = alternative.batch
                recipe = recipes.get(batch.thermal_recipe_id)
                if recipe is None:
                    issues.append(ProductionIssue("batch_recipe", path, "unknown thermal recipe"))
                elif batch.uom != recipe.capacity_uom:
                    issues.append(
                        ProductionIssue("batch_uom", path, "batch and capacity units differ")
                    )
                if not _finite(batch.quantity) or batch.quantity <= 0:
                    issues.append(
                        ProductionIssue("batch_quantity", path, "batch quantity must be positive")
                    )
                if not batch.lot_id or not batch.uom:
                    issues.append(
                        ProductionIssue("batch_quantity", path, "batch identity required")
                    )
    issues.extend(_cycle_issues(problem))
    for index, job in enumerate(problem.jobs):
        path = f"jobs[{index}]"
        if job.demand_kind not in {"customer", "production", "replenishment"}:
            issues.append(ProductionIssue("demand_kind", path, "unknown demand kind"))
        if not job.id or not job.terminal_operation_ids:
            issues.append(ProductionIssue("job", path, "job and terminal operations required"))
        if any(
            operation_id not in operations or operations[operation_id].order_id != job.id
            for operation_id in job.terminal_operation_ids
        ):
            issues.append(ProductionIssue("job_operation", path, "unknown terminal operation"))
        if job.promised_ship_time is not None and not _finite(job.promised_ship_time):
            issues.append(ProductionIssue("promise", path, "promise must be finite"))
        if isinstance(job.priority, bool) or not isinstance(job.priority, int) or job.priority < 0:
            issues.append(
                ProductionIssue("priority", path, "priority must be non-negative integer")
            )
    if problem.cohort is not None:
        cohort = problem.cohort
        if (
            not cohort.id
            or not cohort.snapshot_at
            or cohort.total_work_order_count < 0
            or (cohort.pending_demand_count is not None and cohort.pending_demand_count < 0)
            or any(job_id not in jobs for job_id in cohort.included_job_ids)
            or len(set(cohort.included_job_ids)) != len(cohort.included_job_ids)
            or cohort.total_work_order_count < len(cohort.included_job_ids)
        ):
            issues.append(ProductionIssue("cohort", "cohort", "cohort references invalid scope"))
    return issues


def _validate_recipes(problem: ProductionProblem) -> list[ProductionIssue]:
    issues: list[ProductionIssue] = []
    for index, recipe in enumerate(problem.thermal_recipes):
        path = f"thermal_recipes[{index}]"
        if (
            not recipe.id
            or not recipe.revision
            or not recipe.capacity_uom
            or not _finite(
                recipe.capacity,
                recipe.warmup_duration,
                recipe.hold_duration,
                recipe.cooldown_duration,
            )
            or recipe.capacity <= 0
            or recipe.warmup_duration < 0
            or recipe.hold_duration <= 0
            or recipe.cooldown_duration < 0
        ):
            issues.append(ProductionIssue("thermal_recipe", path, "invalid thermal recipe"))
    return issues


def _finite(*values: float) -> bool:
    return all(not isinstance(value, bool) and math.isfinite(value) for value in values)


def _cycle_issues(problem: ProductionProblem) -> list[ProductionIssue]:
    parents = {operation.id: set(operation.predecessors) for operation in problem.operations}
    remaining = set(parents)
    while remaining:
        ready = {item for item in remaining if not (parents[item] & remaining)}
        if not ready:
            return [ProductionIssue("cycle", "operations", "operation graph contains a cycle")]
        remaining -= ready
    return []
