import base64
import time
from pathlib import Path

from fastapi.testclient import TestClient

from twinflow.service.app import create_app

ROOT = Path(__file__).resolve().parents[2]


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
        assert plan["result"]["status"] == "FEASIBLE"
        assert plan["result"]["verified"] is True
        assert plan["scenario_id"] == scenario["id"]
        assert plan["schedule_snapshot_id"] == committed["id"]
        assert plan["objective"] == "makespan"
        assert plan["status_explanation"] == "Verified baseline schedule"
        assert plan["simulation"]["status"] == "available"
        submitted = client.post(
            f"/api/workspace/schedule-plans/{plan['id']}/simulate",
            json={"request_key": "schedule-run", "reps": 2, "seed": 42},
        )
        assert submitted.status_code == 202, submitted.text
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            schedule_job = client.get(f"/api/workspace/jobs/{submitted.json()['id']}").json()
            if schedule_job["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        assert schedule_job["status"] == "completed", schedule_job
        schedule_result = schedule_job["result"]
        assert schedule_result["schedule_plan"]["id"] == plan["id"]
        assert schedule_result["snapshot_ref"]["id"] == committed["id"]
        assert schedule_result["schedule_execution"]["mode"] == "enforced_verified_plan"
        assert all(
            sample["executed_operations"] == sample["planned_operations"]
            for sample in schedule_result["schedule_execution"]["replications"]
        )
        schedule_evidence = client.post(
            f"/api/workspace/jobs/{schedule_job['id']}/query",
            json={"topic": "schedule"},
        )
        assert schedule_evidence.status_code == 200, schedule_evidence.text
        assert schedule_evidence.json()["total"] == 4
        assert schedule_evidence.json()["snapshot_id"] == committed["id"]
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
        comparison = client.post(
            "/api/workspace/compare",
            json={"baseline_id": baseline_job["id"], "candidate_id": schedule_job["id"]},
        )
        assert comparison.status_code == 200, comparison.text
        assert comparison.json()["compared"]["snapshot_id"] == committed["id"]
        assert comparison.json()["compared"]["baseline_mode"] == "piece_dispatch"
        assert comparison.json()["compared"]["candidate_mode"] == "enforced_verified_plan"
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
        rejected = client.post(
            "/api/workspace/compare",
            json={"baseline_id": original_job["id"], "candidate_id": schedule_job["id"]},
        )
        assert rejected.status_code == 409
        assert "same operational snapshot" in rejected.text


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
