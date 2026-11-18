"""Scenario plans simulate through the new executor (T-189, COMP-141, REQ-142, D-086).

The material-flow DES no longer replays a verified plan, so scenario plans the old replay
guards refused (a setup changeover) now simulate through `PlanningService.simulate_plan`.
Also pins the scenario `schedule_plans` record's `simulation` block shape.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _fake_portfolio import FakePortfolioApi
from _simulation_world import scenario_plan

from twinflow.application.planning import PlanningService
from twinflow.application.workspace import Workspace

CALLER = "arc"
ORDERS_CSV = b"Order,Part,Qty,Release,Due\nr-1,finished,2,0,1000\n"
MAPPING = {
    "order_id": "Order",
    "product": "Part",
    "quantity": "Qty",
    "release_second": "Release",
    "due_second": "Due",
}


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    ws = Workspace(tmp_path / "workspace")  # the signing key must live outside it
    yield ws
    ws.close()


@pytest.fixture
def service(workspace: Workspace) -> Iterator[PlanningService]:
    planning = PlanningService(workspace, portfolio=FakePortfolioApi())
    yield planning
    planning.close()


def _changeover_capsule(changeover_seconds: float) -> dict[str, Any]:
    """One cut station (4 s per piece) with a setup changeover (the old replay blocker)."""
    location: dict[str, Any] = {
        "name": "cut",
        "consumes": [{"thing": "blank", "qty": 1, "uom": "piece"}],
        "emits": [{"thing": "finished", "qty": 1, "uom": "piece"}],
        "setup_key": "setup_cut",
        "time_model": {"kind": "rate_based", "rate": 0.25},
        "machine": "machine_cut",
        "capacity": 1,
        "labor_skill": "operator",
        "changeover_seconds": changeover_seconds,
    }
    model = {
        "defaults": {"cycle_time_cv": 0.0},
        "stocks": [],
        "part_types": [
            {"name": thing, "uom": "piece", "attributes": {}} for thing in ("blank", "finished")
        ],
        "machines": [{"name": "machine_cut"}],
        "labor": {"pools": [{"name": "operators", "headcount": 1, "skills": ["operator"]}]},
        "locations": [location],
        "routing": [{"part": "finished", "steps": ["cut"]}],
    }
    return {
        "schema_version": "0.1",
        "model": model,
        "snapshot": {
            "id": "setup-snapshot",
            "as_of": "2026-10-08T00:00:00+00:00",
            "model_revision": "1",
            "production_plan": [
                {
                    "work_order_id": "seed",
                    "part": "finished",
                    "qty": 1,
                    "start_date": "0",
                    "due_date": "100",
                }
            ],
        },
        "experiment": {"id": "setup", "replications": 1, "seed": 1},
        "assumptions": [],
        "provenance": {"source": "test"},
        "required_capabilities": [],
    }


def _changeover_plan(workspace: Workspace, changeover_seconds: float) -> dict[str, Any]:
    scenario = workspace.import_scenario(
        json.dumps(_changeover_capsule(changeover_seconds)), "Setup"
    )
    content = base64.b64encode(ORDERS_CSV).decode()
    preview = workspace.preview_schedule_upload(scenario["id"], "orders.csv", content, MAPPING)
    snapshot = workspace.commit_schedule_upload(
        scenario["id"], "orders.csv", content, preview["original_file_digest"], MAPPING
    )
    return workspace.solve_schedule_snapshot(snapshot["id"], "baseline", 5)


def test_scenario_plan_simulates_through_the_executor_and_completes(
    workspace: Workspace, service: PlanningService
) -> None:
    plan_id = scenario_plan(workspace)

    result = service.simulate_plan(plan_id, request_key="sim", caller_id=CALLER)

    assert result.status == "completed", result.issues
    assert result.simulation_id is not None
    record = workspace.repository.get("simulations", result.simulation_id)
    assert record["source_kind"] == "scenario"


def test_setup_changeover_plan_the_old_replay_refused_now_simulates(
    workspace: Workspace, service: PlanningService
) -> None:
    plan = _changeover_plan(workspace, changeover_seconds=30.0)

    assert plan["verified"] is True
    assert plan["simulation"] == {"status": "available"}
    result = service.simulate_plan(plan["id"], request_key="sim", caller_id=CALLER)

    assert result.status == "completed", result.issues
    assert result.simulation_id is not None
    assert workspace.repository.count("jobs") == 0


def test_plan_record_simulation_block_has_only_status_when_available(
    workspace: Workspace,
) -> None:
    plan = _changeover_plan(workspace, changeover_seconds=0.0)

    assert plan["verified"] is True
    assert plan["simulation"] == {"status": "available"}
    stored = workspace.repository.get("schedule_plans", plan["id"])
    assert stored["simulation"] == {"status": "available"}
