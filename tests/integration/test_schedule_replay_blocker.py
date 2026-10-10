"""Plan records report the DES replay blocker (T-108, COMP-013, COMP-014, REQ-082).

A verified v2 plan whose scenario the DES replay guards refuse (here a setup changeover)
is kept with `simulation.status = "blocked"` and the guard's own message, and simulating
it is refused with that text. A plan the guards accept stays available.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from twinflow.application.schedule_problem import build_request, firings_from_plan
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import WorkOrder
from twinflow.scenario import ScenarioCapsule
from twinflow.scheduling.solver import solve
from twinflow.service.app import create_app

ROOT = Path(__file__).resolve().parents[2]
ORDERS_CSV = b"Order,Part,Qty,Release,Due\nr-1,finished,2,0,1000\n"


def _capsule(changeover_seconds: float) -> dict[str, Any]:
    """One cut station (4 s per piece) with an optional setup changeover."""
    location: dict[str, Any] = {
        "name": "cut",
        "consumes": [{"thing": "blank", "qty": 1, "uom": "piece"}],
        "emits": [{"thing": "finished", "qty": 1, "uom": "piece"}],
        "setup_key": "setup_cut",
        "time_model": {"kind": "rate_based", "rate": 0.25},
        "machine": "machine_cut",
        "capacity": 1,
        "labor_skill": "operator",
    }
    if changeover_seconds:
        location["changeover_seconds"] = changeover_seconds
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


def _solved_plan(client: TestClient, changeover_seconds: float) -> dict[str, Any]:
    imported = client.post(
        "/api/workspace/scenarios",
        json={"name": "Setup", "content": json.dumps(_capsule(changeover_seconds))},
    )
    assert imported.status_code == 201, imported.text
    upload = {
        "scenario_id": imported.json()["id"],
        "filename": "orders.csv",
        "content_base64": base64.b64encode(ORDERS_CSV).decode(),
        "mapping": {
            "order_id": "Order",
            "product": "Part",
            "quantity": "Qty",
            "release_second": "Release",
            "due_second": "Due",
        },
        "time_unit": "second",
        "time_origin": "scenario_snapshot_as_of",
    }
    preview = client.post("/api/workspace/schedule-uploads/preview", json=upload).json()
    committed = client.post(
        "/api/workspace/schedule-uploads/commit",
        json={**upload, "original_file_digest": preview["original_file_digest"]},
    )
    assert committed.status_code == 201, committed.text
    solved = client.post(
        f"/api/workspace/schedule-snapshots/{committed.json()['id']}/solve",
        json={"backend": "baseline", "time_limit": 5},
    )
    assert solved.status_code == 200, solved.text
    return dict(solved.json())


def _guard_message(changeover_seconds: float) -> str:
    """The message `RunDriver.check_execution_schedule` raises for this scenario's plan."""
    capsule = _capsule(changeover_seconds)
    scenario = {"id": "s", "digest": "d", "capsule": capsule}
    orders = [
        {
            "order_id": "r-1",
            "product": "finished",
            "quantity": 2,
            "release_second": 0.0,
            "due_second": 1000.0,
        }
    ]
    snapshot = {"scenario_id": "s", "scenario_digest": "d", "orders": orders}
    translation = build_request(scenario, snapshot)
    assert translation.request is not None, translation.issues
    result = solve(translation.request)
    assert result.verified, result.issues
    plan = [WorkOrder(work_order_id="r-1", part="finished", qty=2, start_date="0", due_date="1000")]
    driver = RunDriver(ScenarioCapsule.from_dict(capsule).compile().model)
    with pytest.raises(ValueError) as refused:
        driver.check_execution_schedule(plan, firings_from_plan(translation, result), None)
    return str(refused.value)


def test_setup_changeover_plan_records_the_replay_guard_blocker(tmp_path: Path) -> None:
    expected = _guard_message(changeover_seconds=30.0)
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        plan = _solved_plan(client, changeover_seconds=30.0)

        assert plan["verified"] is True
        assert plan["simulation"]["status"] == "blocked"
        assert plan["simulation"]["blocker"] == expected
        stored = client.get(f"/api/workspace/schedule-plans/{plan['id']}").json()
        assert stored["simulation"] == plan["simulation"]
        submitted = client.post(
            f"/api/workspace/schedule-plans/{plan['id']}/simulate",
            json={"request_key": "blocked-run", "reps": 1, "seed": 42},
        )
        assert submitted.status_code == 422, submitted.text
        assert submitted.json()["detail"] == expected


def test_plan_the_replay_guards_accept_stays_available(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        plan = _solved_plan(client, changeover_seconds=0.0)

        assert plan["verified"] is True
        assert plan["simulation"] == {**plan["simulation"], "status": "available", "blocker": None}
        submitted = client.post(
            f"/api/workspace/schedule-plans/{plan['id']}/simulate",
            json={"request_key": "linear-run", "reps": 1, "seed": 42},
        )
        assert submitted.status_code == 202, submitted.text
