import base64
import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from twinflow.scheduling.model import ObjectiveKind, SolveResult
from twinflow.scheduling.profile import default_profile
from twinflow.scheduling.seam import Feature
from twinflow.service.app import create_app

ROOT = Path(__file__).resolve().parents[2]
ALL_OBJECTIVES = {kind.value for kind in ObjectiveKind}
DEFAULT_OBJECTIVE = default_profile().objective.model_dump(mode="json")


def _assert_v2_plan_record(plan: dict[str, Any]) -> None:
    """A verified v2 snapshot plan record (REQ-083): v2 fields beside the SolveResult."""
    assert plan["schema_version"] == "2.0"
    assert plan["translator_version"] == "capsule-ops-2"
    assert plan["objective_spec"] == DEFAULT_OBJECTIVE
    assert set(plan["objective_values"]) == ALL_OBJECTIVES
    assert plan["objective_values"] == plan["result"]["objective_values"]
    assert plan["features"] == plan["result"]["features"]
    assert plan["held_job_ids"] == []
    assert len(plan["problem_digest"]) == 64


def test_import_branch_export_and_draft(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        assert client.get("/api/workspace/capabilities").json()["production_writeback"] is False
        response = client.post("/api/workspace/examples/load", json={"example_id": "spring"})
        assert response.status_code == 201, response.text
        original = response.json()
        exported = client.get(f"/api/workspace/scenarios/{original['id']}/export")
        assert exported.json() == original["capsule"]
        child = client.post(
            f"/api/workspace/scenarios/{original['id']}/branch",
            json={"name": "Alternative", "content": exported.text},
        ).json()
        assert child["parent_id"] == original["id"]
        assert client.get(f"/api/workspace/scenarios/{original['id']}").json() == original
        draft = client.post(
            "/api/workspace/drafts", json={"name": "Intake", "facts": {"process": "cut then pack"}}
        ).json()
        assert draft["status"] == "draft"
        assert "process" not in {q["id"] for q in draft["questions"]}
        assert len(draft["questions"]) == 4


def test_missing_and_invalid_inputs_do_not_create_scenarios(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        assert client.get("/api/workspace/scenarios/missing").status_code == 404
        assert (
            client.post(
                "/api/workspace/examples/load", json={"example_id": "../spring"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/workspace/scenarios", json={"name": "Invalid", "content": "{}"}
            ).status_code
            == 422
        )
        assert client.get("/api/workspace/scenarios").json() == []
        assert (
            client.post(
                "/api/workspace/scenarios/id/evaluate", json={"reps": 51, "request_key": "a"}
            ).status_code
            == 422
        )


def test_request_idempotency_and_persistence(tmp_path: Path, monkeypatch) -> None:
    from twinflow.application import scenarios

    monkeypatch.setattr(
        scenarios, "evaluate_capsule", lambda *args: {"metrics": {"on_time_pct": 100}}
    )
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        scenario = client.post("/api/workspace/examples/load", json={"example_id": "spring"}).json()
        url = f"/api/workspace/scenarios/{scenario['id']}/evaluate"
        body = {"request_key": "once", "reps": 1, "seed": 42}
        a = client.post(url, json=body)
        b = client.post(url, json=body)
        assert a.status_code == b.status_code == 202
        assert a.json()["id"] == b.json()["id"]
        assert client.post(url, json={**body, "seed": 43}).status_code == 409
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        assert len(client.get("/api/workspace/scenarios").json()) == 1
        assert len(client.get("/api/workspace/jobs").json()) == 1


def test_decision_result_provenance_and_wait_evidence(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        scenario = client.post("/api/workspace/examples/load", json={"example_id": "spring"}).json()
        submitted = client.post(
            f"/api/workspace/scenarios/{scenario['id']}/evaluate",
            json={"request_key": "result-contract", "reps": 1, "seed": 42},
        ).json()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = client.get(f"/api/workspace/jobs/{submitted['id']}").json()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        assert job["status"] == "completed", job
        result = job["result"]
        assert result["schema_version"] == "1.1"
        assert result["scenario"] == {
            "id": scenario["id"],
            "digest": scenario["digest"],
            "parent_id": None,
        }
        assert result["snapshot_ref"]["id"] == scenario["capsule"]["snapshot"]["id"]
        assert result["job"]["id"] == job["id"]
        assert result["model_validity"] == "provisional"
        assert result["metric_definitions"]["on_time_pct"]["unit"] == "percent"
        assert result["resource_utilization"]
        assert result["wait_breakdown"]
        assert result["order_outcomes"]
        assert result["replication_outcomes"] == result["outcomes"]
        query_url = f"/api/workspace/jobs/{job['id']}/query"
        waits = client.post(query_url, json={"topic": "blockers"}).json()
        termination = client.post(query_url, json={"topic": "termination"}).json()
        assert waits["evidence_kind"] == "simulated_wait_attribution"
        assert termination["evidence_kind"] == "run_termination"
        assert waits["evidence"] != termination["evidence"]


def test_model_draft_compile_and_import_are_linked(tmp_path: Path) -> None:
    def fact(value, unit=None):
        return {"status": "supplied", "value": value, "unit": unit, "source": "operator"}

    draft = {
        "product": fact("finished"),
        "input_part": fact("blank"),
        "as_of": fact("2026-10-08T00:00:00Z"),
        "operators": fact(1, "person"),
        "simple_flow_confirmed": fact(True),
        "stages": [
            {
                "id": "cut",
                "seconds_per_piece": fact(4.0, "second/piece"),
                "machine_capacity": fact(1, "machine"),
            }
        ],
        "orders": [
            {
                "id": "o1",
                "quantity": fact(1, "piece"),
                "release_second": fact(0.0, "second"),
                "due_second": fact(100.0, "second"),
            }
        ],
    }
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        saved_response = client.post(
            "/api/workspace/model-drafts", json={"name": "Linear demo", "draft": draft}
        )
        assert saved_response.status_code == 201, saved_response.text
        saved = saved_response.json()
        compiled_response = client.post(f"/api/workspace/model-drafts/{saved['id']}/compile")
        assert compiled_response.status_code == 200, compiled_response.text
        compiled = compiled_response.json()
        assert compiled["status"] == "compiled", compiled["issues"]
        imported_response = client.post(f"/api/workspace/model-drafts/{saved['id']}/import")
        assert imported_response.status_code == 201, imported_response.text
        scenario = imported_response.json()
        assert scenario["digest"] == compiled["digest"]
        assert scenario["draft_link"] == {
            "id": saved["id"],
            "revision": 1,
            "compiler_version": "linear-piece-1",
        }
        assert scenario["capsule"]["provenance"]["draft_id"] == saved["id"]
        submitted = client.post(
            f"/api/workspace/scenarios/{scenario['id']}/evaluate",
            json={"request_key": "compiled-demo", "reps": 1, "seed": 42},
        ).json()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = client.get(f"/api/workspace/jobs/{submitted['id']}").json()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        assert job["status"] == "completed", job

        content = base64.b64encode(
            b"Order,Part,Qty,Release,Due\nnew-1,finished,2,0,1000\n"
        ).decode()
        mapping = {
            "order_id": "Order",
            "product": "Part",
            "quantity": "Qty",
            "release_second": "Release",
            "due_second": "Due",
        }
        upload = {
            "scenario_id": scenario["id"],
            "filename": "orders.csv",
            "content_base64": content,
            "mapping": mapping,
            "time_unit": "second",
            "time_origin": "scenario_snapshot_as_of",
        }
        preview = client.post("/api/workspace/schedule-uploads/preview", json=upload).json()
        committed = client.post(
            "/api/workspace/schedule-uploads/commit",
            json={**upload, "original_file_digest": preview["original_file_digest"]},
        ).json()
        solved = client.post(
            f"/api/workspace/schedule-snapshots/{committed['id']}/solve",
            json={"backend": "baseline", "time_limit": 5},
        )
        assert solved.status_code == 200, solved.text
        plan = solved.json()
        assert SolveResult.model_validate(plan["result"]).verified
        assert plan["result"]["schedule"]["status"] == "FEASIBLE"
        assert plan["scenario_id"] == scenario["id"]
        assert plan["schedule_snapshot_id"] == committed["id"]
        _assert_v2_plan_record(plan)
        assert plan["status_explanation"] == "Verified baseline schedule"
        assert plan["simulation"]["status"] == "available"
        baseline_submission = client.post(
            f"/api/workspace/schedule-snapshots/{committed['id']}/simulate-baseline",
            json={"request_key": "schedule-baseline", "reps": 2, "seed": 42},
        )
        assert baseline_submission.status_code == 202, baseline_submission.text
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            baseline_job = client.get(
                f"/api/workspace/jobs/{baseline_submission.json()['id']}"
            ).json()
            if baseline_job["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        assert baseline_job["status"] == "completed", baseline_job
        assert baseline_job["result"]["schedule_baseline"]["mode"] == (
            "piece_dispatch_without_enforced_plan"
        )
        original_submission = client.post(
            f"/api/workspace/scenarios/{scenario['id']}/evaluate",
            json={"request_key": "original-orders", "reps": 2, "seed": 42},
        ).json()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            original_job = client.get(f"/api/workspace/jobs/{original_submission['id']}").json()
            if original_job["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        assert original_job["status"] == "completed", original_job


def test_schedule_upload_preview_and_snapshot_commit(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        scenario = client.post("/api/workspace/examples/load", json={"example_id": "spring"}).json()
        content = base64.b64encode(b"Order,Part,Qty,Release,Due\nnew-1,coated,2,0,1000\n").decode()
        mapping = {
            "order_id": "Order",
            "product": "Part",
            "quantity": "Qty",
            "release_second": "Release",
            "due_second": "Due",
        }
        request = {
            "scenario_id": scenario["id"],
            "filename": "orders.csv",
            "content_base64": content,
            "mapping": mapping,
            "time_unit": "second",
            "time_origin": "scenario_snapshot_as_of",
        }
        preview = client.post("/api/workspace/schedule-uploads/preview", json=request)
        assert preview.status_code == 200, preview.text
        assert preview.json()["committable"]
        committed = client.post(
            "/api/workspace/schedule-uploads/commit",
            json={**request, "original_file_digest": preview.json()["original_file_digest"]},
        )
        assert committed.status_code == 201, committed.text
        record = committed.json()
        assert record["scenario_digest"] == scenario["digest"]
        assert record["source_snapshot_id"] == scenario["capsule"]["snapshot"]["id"]
        assert record["orders"][0]["order_id"] == "new-1"
        assert record["original_file_retention"] == "discarded_after_parse"
        assert client.get(f"/api/workspace/schedule-snapshots/{record['id']}").json() == record
        unsupported = client.post(
            f"/api/workspace/schedule-snapshots/{record['id']}/simulate-baseline",
            json={"request_key": "spring-baseline", "reps": 1, "seed": 42},
        )
        assert unsupported.status_code == 422


# --- v2 scheduling transport (T-106, COMP-014, REQ-083) --------------------------------------

V1_PROBLEM = {
    "objective": "makespan",
    "operations": [{"id": "cut", "duration": 2}],
    "resources": [{"resource_id": "machine-1", "start": 0, "end": 20}],
}
MAX_LATENESS = {"mode": "lexicographic", "levels": [{"kind": "max_lateness"}]}


def _option(machine_id: str, seconds: float) -> dict[str, Any]:
    return {
        "machine_id": machine_id,
        "fixed_seconds": seconds,
        "unit_seconds": 0.0,
        "preference_cost": 0.0,
    }


def _v2_request(objective: dict[str, Any] | None = None) -> dict[str, Any]:
    """cut (2 s on m1 or m2) then assemble (3 s on b1), due at 4 s."""
    cut = {
        "id": "cut",
        "work_center_id": "machining",
        "quantity": 1,
        "options": [_option("m1", 2.0), _option("m2", 2.0)],
    }
    assemble = {
        "id": "assemble",
        "work_center_id": "assembly",
        "quantity": 1,
        "options": [_option("b1", 3.0)],
        "predecessors": [{"operation_id": "cut"}],
    }
    request: dict[str, Any] = {
        "schema_version": "2.0",
        "problem": {
            "id": "hand",
            "jobs": [{"id": "J", "due": 4.0, "operations": [cut, assemble]}],
            "machines": [
                {"id": "m1", "work_center_id": "machining"},
                {"id": "m2", "work_center_id": "machining"},
                {"id": "b1", "work_center_id": "assembly"},
            ],
            "work_centers": [{"id": "machining"}, {"id": "assembly"}],
        },
    }
    if objective is not None:
        request["profile"] = {"objective": objective}
    return request


def _routing_capsule(steps: tuple[str, ...]) -> dict[str, Any]:
    """A multi-step routing capsule imported directly, with no linear-piece draft link."""
    things = ["blank", *(f"{step}_out" for step in steps[:-1]), "finished"]
    locations = [
        {
            "name": step,
            "consumes": [{"thing": things[index], "qty": 1, "uom": "piece"}],
            "emits": [{"thing": things[index + 1], "qty": 1, "uom": "piece"}],
            "setup_key": f"setup_{step}",
            "time_model": {"kind": "rate_based", "rate": 0.25},
            "machine": f"machine_{step}",
            "capacity": 1,
            "labor_skill": "operator",
        }
        for index, step in enumerate(steps)
    ]
    model = {
        "defaults": {"cycle_time_cv": 0.0},
        "stocks": [],
        "part_types": [{"name": thing, "uom": "piece", "attributes": {}} for thing in things],
        "machines": [{"name": f"machine_{step}"} for step in steps],
        "labor": {
            "pools": [{"name": "operators", "headcount": len(steps), "skills": ["operator"]}]
        },
        "locations": locations,
        "routing": [{"part": "finished", "steps": list(steps)}],
    }
    return {
        "schema_version": "0.1",
        "model": model,
        "snapshot": {
            "id": "routing-snapshot",
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
        "experiment": {"id": "routing", "replications": 1, "seed": 1},
        "assumptions": [],
        "provenance": {"source": "test"},
        "required_capabilities": [],
    }


def _commit_orders(client: TestClient, scenario_id: str, csv: bytes) -> dict[str, Any]:
    upload = {
        "scenario_id": scenario_id,
        "filename": "orders.csv",
        "content_base64": base64.b64encode(csv).decode(),
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
    return dict(committed.json())


def test_multi_step_routing_snapshot_solves_without_a_linear_piece_gate(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        imported = client.post(
            "/api/workspace/scenarios",
            json={
                "name": "Routing",
                "content": json.dumps(_routing_capsule(("cut", "drill", "pack"))),
            },
        )
        assert imported.status_code == 201, imported.text
        assert imported.json().get("draft_link") is None
        snapshot = _commit_orders(
            client,
            imported.json()["id"],
            b"Order,Part,Qty,Release,Due\nr-1,finished,2,0,1000\nr-2,finished,1,5,1000\n",
        )

        solved = client.post(
            f"/api/workspace/schedule-snapshots/{snapshot['id']}/solve",
            json={"backend": "baseline", "time_limit": 5},
        )

        assert solved.status_code == 200, solved.text
        plan = solved.json()
        _assert_v2_plan_record(plan)
        result = SolveResult.model_validate(plan["result"])
        assert result.verified, result.issues
        assert len(result.schedule.assignments) == 2 * 3
        assert "operators" in plan["features"]
        assert plan["simulation"]["status"] == "available"
        stored = client.get(f"/api/workspace/schedule-plans/{plan['id']}").json()
        assert stored["schema_version"] == "2.0"
        assert stored["objective_values"] == plan["objective_values"]


def test_schedules_route_takes_a_v2_request_and_returns_a_v2_record(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        request = _v2_request(MAX_LATENESS)

        response = client.post(
            "/api/workspace/schedules",
            json={"request": request, "backend": "baseline", "time_limit": 5},
        )

        assert response.status_code == 201, response.text
        record = response.json()
        assert record["schema_version"] == "2.0"
        assert record["request"] == request
        assert record["backend"] == "baseline"
        assert record["verified"] is True
        assert record["objective_spec"] == MAX_LATENESS
        assert set(record["objective_values"]) == ALL_OBJECTIVES
        assert record["objective_values"]["max_lateness"] == 1.0
        assert record["features"] == ["alternatives"]
        result = SolveResult.model_validate(record["result"])
        assert result.verified and result.schedule.status == "FEASIBLE"
        assert [row["id"] for row in client.get("/api/workspace/schedules").json()] == [
            record["id"]
        ]


def test_schedules_route_runs_cpsat_on_a_v2_request(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        response = client.post(
            "/api/workspace/schedules",
            json={"request": _v2_request(), "backend": "ortools", "time_limit": 5},
        )

        assert response.status_code == 201, response.text
        record = response.json()
        assert record["result"]["schedule"]["status"] == "OPTIMAL"
        assert record["verified"] is True
        assert record["objective_spec"] == DEFAULT_OBJECTIVE
        assert record["objective_values"]["makespan"] == 5.0


def test_schedules_route_rejects_v1_bodies_naming_schema_version(tmp_path: Path) -> None:
    v1_versioned = {**_v2_request(), "schema_version": "1.0"}
    bodies = [
        {"problem": V1_PROBLEM},
        {"problem": V1_PROBLEM, "backend": "baseline", "time_limit": 5},
        {"request": {"schema_version": "1.0", "problem": V1_PROBLEM}},
        {"request": v1_versioned},
    ]
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        accepted = client.post("/api/workspace/schedules", json={"request": _v2_request()})
        assert accepted.status_code == 201, accepted.text
        for body in bodies:
            rejected = client.post("/api/workspace/schedules", json=body)
            assert rejected.status_code == 422, (body, rejected.text)
            assert "schema_version" in json.dumps(rejected.json()["detail"]), rejected.text
        assert len(client.get("/api/workspace/schedules").json()) == 1


def test_schedules_route_bounds_backend_and_time_limit(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        ok = client.post(
            "/api/workspace/schedules", json={"request": _v2_request(), "time_limit": 30}
        )
        assert ok.status_code == 201, ok.text
        for extra in ({"time_limit": 0}, {"time_limit": 30.5}, {"backend": "gurobi"}):
            response = client.post(
                "/api/workspace/schedules", json={"request": _v2_request(), **extra}
            )
            assert response.status_code == 422, (extra, response.text)


def test_tool_contracts_publish_the_v2_scheduling_contract(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        contracts = client.get("/api/workspace/tool-contracts").json()

        example = contracts["scheduling_example"]
        assert example["schema_version"] == "2.0"
        scheduling = contracts["scheduling"]
        assert scheduling["schema_version"] == "2.0"
        assert set(scheduling["features"]) == {feature.value for feature in Feature}
        assert set(scheduling["objectives"]) == ALL_OBJECTIVES
        assert "max_solver_pieces" not in contracts["schedule_upload"]
        assert "max_solver_operations" not in contracts["schedule_upload"]
        solved = client.post("/api/workspace/schedules", json={"request": example})
        assert solved.status_code == 201, solved.text
        assert solved.json()["verified"] is True
