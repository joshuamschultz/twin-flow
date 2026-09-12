"""Narrow, explicit translator from the current manufacturing config surface."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from twinflow.scheduling.core import Operation, ResourceWindow, SchedulingProblem


@dataclass(frozen=True)
class TranslationError(ValueError):
    issues: tuple[str, ...]

    def __init__(self, issues: Sequence[str]) -> None:
        object.__setattr__(self, "issues", tuple(issues))
        ValueError.__init__(self, "; ".join(self.issues))


def translate_legacy(
    model: Mapping[str, Any], plan: Sequence[Mapping[str, Any]]
) -> SchedulingProblem:
    """Translate deterministic rate-based routes, refusing unsupported semantics."""
    issues: list[str] = []
    locations = {
        str(item.get("name")): item for item in model.get("locations", []) if isinstance(item, dict)
    }
    routing = {
        str(item.get("part")): item.get("steps", [])
        for item in model.get("routing", [])
        if isinstance(item, dict)
    }
    machines = {
        str(item.get("name")) for item in model.get("machines", []) if isinstance(item, dict)
    }
    operations: list[Operation] = []
    resource_roles: dict[str, set[str]] = {}
    for row_index, row in enumerate(plan):
        order_id = str(row.get("work_order_id", ""))
        part = str(row.get("part", ""))
        steps = routing.get(part)
        if not isinstance(steps, list) or not steps:
            issues.append(f"plan[{row_index}] has no route for part {part!r}")
            continue
        if row.get("initial_wip_location") not in (None, ""):
            issues.append(
                f"plan[{row_index}] initial WIP is unsupported by deterministic translator"
            )
        previous: str | None = None
        for step in steps:
            location = locations.get(str(step))
            if not isinstance(location, dict):
                issues.append(f"plan[{row_index}] references unknown location {step!r}")
                continue
            time_model = location.get("time_model", {})
            if not isinstance(time_model, dict) or time_model.get("kind") != "rate_based":
                issues.append(f"location {step!r} uses unsupported non-deterministic time model")
                continue
            rate = time_model.get("rate")
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
                issues.append(f"location {step!r} has invalid rate")
                continue
            machine = str(location.get("machine", ""))
            if machine not in machines:
                issues.append(f"location {step!r} references unknown machine {machine!r}")
            role = str(location.get("labor_skill", ""))
            operation_id = f"{order_id}:{step}"
            operations.append(
                Operation(
                    operation_id,
                    1.0 / float(rate),
                    (previous,) if previous else (),
                    (machine,),
                    frozenset({role}) if role else frozenset(),
                    float(row.get("start_date", 0)),
                )
            )
            resource_roles.setdefault(machine, set()).add(role)
            previous = operation_id
    if issues:
        raise TranslationError(issues)
    horizon = max((float(row.get("due_date", 1_000_000.0)) for row in plan), default=1_000_000.0)
    resources = tuple(
        ResourceWindow(resource_id, 0.0, max(horizon, 1_000_000.0), frozenset(roles))
        for resource_id, roles in resource_roles.items()
    )
    return SchedulingProblem(tuple(operations), resources)
