"""Translate confirmed linear one-piece twins and order snapshots to the finite solver."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from twinflow.plan.loader import WorkOrder
from twinflow.primitives.location import ScheduledFiring

TRANSLATOR_VERSION = "linear-piece-schedule-1"
MAX_PIECES = 500
MAX_OPERATIONS = 1_000


def work_orders_from_snapshot(schedule_snapshot: dict[str, Any]) -> list[WorkOrder]:
    """Use the committed uploaded orders, not the scenario's earlier demand."""
    return [
        WorkOrder(
            work_order_id=order["order_id"],
            part=order["product"],
            qty=order["quantity"],
            start_date=str(order["release_second"]),
            due_date=str(order["due_second"]),
        )
        for order in schedule_snapshot["orders"]
    ]


def firings_from_plan(
    translated: dict[str, Any], solver_result: dict[str, Any]
) -> list[ScheduledFiring]:
    """Resolve the verified solver assignment to exact runtime work identities."""
    if solver_result.get("status") not in ("FEASIBLE", "OPTIMAL") or not solver_result.get(
        "verified"
    ):
        raise ValueError("Only a verified feasible plan can be simulated")
    starts = solver_result["starts"]
    ends = solver_result["ends"]
    resources = solver_result["resources"]
    operations = translated["problem"]["operations"]
    expected = {operation["id"] for operation in operations}
    if set(starts) != expected or set(ends) != expected or set(resources) != expected:
        raise ValueError("Solver assignment does not cover every translated operation")
    firings = []
    for operation in operations:
        operation_id = operation["id"]
        piece_id, location_id = operation_id.rsplit(":stage-", 1)
        firings.append(
            ScheduledFiring(
                operation_id=operation_id,
                piece_id=piece_id,
                location_id=location_id,
                machine_id=resources[operation_id],
                planned_start=starts[operation_id],
                planned_end=ends[operation_id],
            )
        )
    return firings


def translate_schedule(
    scenario: dict[str, Any], schedule_snapshot: dict[str, Any]
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []

    def block(path: str, message: str) -> None:
        issues.append({"path": path, "code": "unsupported", "message": message})

    if (
        scenario["id"] != schedule_snapshot["scenario_id"]
        or scenario["digest"] != schedule_snapshot["scenario_digest"]
    ):
        block("scenario", "Schedule snapshot belongs to another scenario revision")
    if scenario.get("draft_link", {}).get("compiler_version") != "linear-piece-1":
        block("scenario", "Only confirmed linear-piece compiler scenarios can be translated")
    capsule = scenario["capsule"]
    draft = capsule.get("provenance", {}).get("facts", {})
    stages = draft.get("stages", [])
    operators = draft.get("operators", {}).get("value")
    if not stages or not isinstance(operators, int):
        block("scenario", "Compiled stage and operator facts are unavailable")
    if draft.get("unsupported_features"):
        block("scenario.unsupported_features", "Unsupported model constraints are present")
    total_machines = sum(stage["machine_capacity"]["value"] for stage in stages) if stages else 0
    if isinstance(operators, int) and operators < total_machines:
        block(
            "scenario.operators",
            "Shared operator capacity can bind; this solver has one resource per operation",
        )
    orders = schedule_snapshot["orders"]
    piece_count = sum(order["quantity"] for order in orders)
    if piece_count > MAX_PIECES:
        block("orders", f"At most {MAX_PIECES} pieces can be translated in this pilot")
    if piece_count * len(stages) > MAX_OPERATIONS:
        block("orders", f"At most {MAX_OPERATIONS} piece-stage operations are supported")
    if issues:
        return {"status": "blocked", "issues": issues, "translator_version": TRANSLATOR_VERSION}

    resources = []
    operations = []
    total_duration = sum(
        order["quantity"] * sum(stage["seconds_per_piece"]["value"] for stage in stages)
        for order in orders
    )
    horizon = max(order["release_second"] for order in orders) + total_duration + 1
    for stage in stages:
        capacity = stage["machine_capacity"]["value"]
        for slot in range(capacity):
            resources.append(
                {
                    "resource_id": (
                        f"machine_{stage['id']}"
                        if capacity == 1
                        else f"machine_{stage['id']}-{slot + 1}"
                    ),
                    "start": 0.0,
                    "end": horizon,
                    "qualifications": [],
                }
            )
    for order_index, order in enumerate(orders):
        for piece in range(order["quantity"]):
            prior = None
            for stage in stages:
                operation_id = f"order-{order_index}:piece-{piece}:stage-{stage['id']}"
                operations.append(
                    {
                        "id": operation_id,
                        "duration": stage["seconds_per_piece"]["value"],
                        "predecessors": [prior] if prior is not None else [],
                        "eligible_resources": [
                            f"machine_{stage['id']}"
                            if stage["machine_capacity"]["value"] == 1
                            else f"machine_{stage['id']}-{slot + 1}"
                            for slot in range(stage["machine_capacity"]["value"])
                        ],
                        "required_qualifications": [],
                        "release_time": order["release_second"],
                    }
                )
                prior = operation_id
    problem = {"operations": operations, "resources": resources, "objective": "makespan"}
    digest = hashlib.sha256(
        json.dumps(problem, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return {
        "status": "translated",
        "issues": [],
        "translator_version": TRANSLATOR_VERSION,
        "problem": problem,
        "problem_digest": digest,
        "objective": "makespan",
        "due_date_treatment": "recorded_for_review_but_not_optimized",
        "constraint_scope": (
            "per-piece stage precedence, release seconds, and stage machine capacity"
        ),
    }
