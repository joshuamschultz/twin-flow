"""Verified v2 lot plans are actually executed piece by piece by the DES runner."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from twinflow.application.linear_draft import LinearDraft, compile_draft
from twinflow.application.schedule_problem import build_request, firings_from_plan
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import WorkOrder
from twinflow.scenario import ScenarioCapsule
from twinflow.scheduling.solver import solve


def _fact(value, unit=None):
    return {"status": "confirmed", "value": value, "unit": unit, "source": "test fixture"}


def _work_orders(snapshot: dict[str, Any]) -> list[WorkOrder]:
    """The committed uploaded orders, not the scenario's earlier demand."""
    return [
        WorkOrder(
            work_order_id=order["order_id"],
            part=order["product"],
            qty=order["quantity"],
            start_date=str(order["release_second"]),
            due_date=str(order["due_second"]),
        )
        for order in snapshot["orders"]
    ]


def test_runner_replays_verified_machine_assignments(tmp_path: Path) -> None:
    draft = LinearDraft.model_validate(
        {
            "product": _fact("finished"),
            "input_part": _fact("blank"),
            "as_of": _fact("2026-10-08T00:00:00Z"),
            "operators": _fact(3, "person"),
            "simple_flow_confirmed": _fact(True),
            "stages": [
                {
                    "id": "cut",
                    "seconds_per_piece": _fact(4.0, "second/piece"),
                    "machine_capacity": _fact(2, "machine"),
                },
                {
                    "id": "pack",
                    "seconds_per_piece": _fact(2.0, "second/piece"),
                    "machine_capacity": _fact(1, "machine"),
                },
            ],
            "orders": [
                {
                    "id": "original",
                    "quantity": _fact(1, "piece"),
                    "release_second": _fact(0.0, "second"),
                    "due_second": _fact(100.0, "second"),
                }
            ],
        }
    )
    compiled = compile_draft(draft, "draft", 1)
    assert compiled["status"] == "compiled", compiled["issues"]
    capsule = ScenarioCapsule.from_dict(compiled["capsule_preview"])
    scenario = {
        "id": "scenario",
        "digest": capsule.digest,
        "capsule": capsule.to_dict(),
    }
    snapshot = {
        "scenario_id": "scenario",
        "scenario_digest": capsule.digest,
        "orders": [
            {
                "order_id": "new",
                "product": "finished",
                "quantity": 2,
                "release_second": 0.0,
                "due_second": 100.0,
            },
        ],
    }
    translation = build_request(scenario, snapshot)
    assert translation.status == "ready", translation.issues
    assert translation.request is not None
    solved = solve(translation.request)
    assert solved.schedule.status == "FEASIBLE" and solved.verified
    firings = firings_from_plan(translation, solved)
    orders = _work_orders(snapshot)
    with pytest.raises(ValueError, match="exactly one firing"):
        RunDriver(capsule.compile().model).run(
            orders,
            seed=42,
            replication_index=0,
            artifact_dir=tmp_path,
            execution_schedule=firings[:-1],
        )
    # A lot runs its pieces back to back on one machine; pulling the second piece's start
    # onto the first's makes the two firings overlap on that machine.
    first_cut, second_cut = (firing for firing in firings if firing.location_id == "cut")
    assert first_cut.machine_id == second_cut.machine_id
    overlap = [
        replace(firing, planned_start=first_cut.planned_start)
        if firing.operation_id == second_cut.operation_id
        else firing
        for firing in firings
    ]
    with pytest.raises(ValueError, match="overlapping"):
        RunDriver(capsule.compile().model).run(
            orders,
            seed=42,
            replication_index=0,
            artifact_dir=tmp_path,
            execution_schedule=overlap,
        )
    run = RunDriver(capsule.compile().model).run(
        orders,
        seed=42,
        replication_index=0,
        artifact_dir=tmp_path,
        execution_schedule=firings,
    )
    assert run.outcome == "completed"
    assert len(run.schedule_trace) == len(firings) == 2 * 2
    by_id = {row["operation_id"]: row for row in run.schedule_trace}
    for firing in firings:
        row = by_id[firing.operation_id]
        assert row["machine_id"] == firing.machine_id
        assert row["actual_start"] >= firing.planned_start
        assert row["actual_start"] == firing.planned_start
    assert run.run_meta["execution_mode"] == "verified_schedule"
    repeated = RunDriver(capsule.compile().model).run(
        orders,
        seed=42,
        replication_index=0,
        artifact_dir=tmp_path,
        execution_schedule=firings,
    )
    assert repeated.schedule_trace == run.schedule_trace
