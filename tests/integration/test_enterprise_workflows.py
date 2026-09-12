"""End-to-end contracts across independently built epics, with real domain engines."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from twinflow.application.scenarios import evaluate_capsule, validate_content
from twinflow.data import SnapshotBuilder, SQLiteEventStore, parse_event
from twinflow.service.app import create_app
from twinflow.service.settings import ServiceSettings

ROOT = Path(__file__).resolve().parents[2]


def _wait(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get(f"/api/workspace/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            assert job["status"] == "completed", job
            return job
        time.sleep(.01)
    pytest.fail("Experiment did not finish within the test budget")


@pytest.mark.parametrize("example,domain", [
    ("spring", "manufacturing"), ("capsule-office", "office"),
    ("capsule-supply-manufacturing", "supply_chain"),
    ("capsule-supply-distribution", "supply_chain"),
])
def test_domains_import_run_query_export_and_compare(tmp_path: Path, example: str, domain: str) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        response = client.post("/api/workspace/examples/load", json={"example_id": example})
        assert response.status_code == 201, response.text
        scenario = response.json()
        assert scenario["summary"]["domain"] == domain
        job = client.post(f"/api/workspace/scenarios/{scenario['id']}/evaluate",
                          json={"reps": 2, "seed": 42, "request_key": "baseline"}).json()
        completed = _wait(client, job["id"])
        assert completed["result"]["domain"] == domain
        evidence = client.post(f"/api/workspace/jobs/{job['id']}/query", json={"topic": "dates"})
        assert evidence.status_code == 200 and evidence.json()["evidence"]
        assert evidence.json()["scenario_digest"] == scenario["digest"]
        assert client.get(f"/api/workspace/jobs/{job['id']}/export").json()["result"]
        compared = client.post("/api/workspace/compare", json={"baseline_id": job["id"], "candidate_id": job["id"]})
        assert all(value == 0 for value in compared.json()["delta"].values())


def test_agent_data_answers_and_schedule_share_transport_contract(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        contracts = client.get("/api/workspace/tool-contracts").json()
        records = contracts["event_example"]
        imported = client.post("/api/workspace/data/events", json={"records": records})
        assert imported.status_code == 201 and imported.json()["inserted"] == len(records)
        assert client.post("/api/workspace/data/events", json={"records": records}).json()["duplicates"] == len(records)
        malformed = client.post("/api/workspace/data/events", json={"records": [{}]})
        assert malformed.status_code == 422
        conflict = json.loads(json.dumps(records[0])); conflict["payload"]["quantity"] = 999
        assert client.post("/api/workspace/data/events", json={"records": [conflict]}).status_code == 409
        snapshot = client.post("/api/workspace/data/snapshots", json={"known_at": "2026-01-10T00:00:00Z"}).json()
        stock = next(row for row in snapshot["entities"] if row["entity_type"] == "stock")
        assert stock["values"]["quantity"] == 100
        draft = client.post("/api/workspace/drafts", json={"name": "Intake", "facts": {"process": "approve"}}).json()
        revision = client.post(f"/api/workspace/drafts/{draft['id']}/answers", json={"answers": [{"id": "resources", "answer": "one reviewer"}]}).json()
        assert revision["id"] != draft["id"]
        assert "resources" not in {row["id"] for row in revision["questions"]}
        assert client.post("/api/workspace/validate", json={"content": "{}"}).json()["valid"] is False
        scheduled = client.post("/api/workspace/schedules", json={"problem": contracts["scheduling_example"]})
        assert scheduled.status_code == 201, scheduled.text
        assert scheduled.json()["result"]["verified"] is True


def test_configured_policy_records_actual_dispatch_boundaries(tmp_path: Path) -> None:
    capsule = validate_content((ROOT / "examples/capsules/spring.twin.yaml").read_text())
    data = capsule.to_dict()
    data["experiment"]["dispatch_policy"] = {"default": "edd", "rules": []}
    validated = validate_content(json.dumps(data))
    result = evaluate_capsule(validated.to_dict(), tmp_path, 1, 42, lambda: False)
    decisions = result["policy_traces"][0]["decisions"]
    assert decisions and all(row["action"]["policy"] == "edd" for row in decisions)
    assert len({row["state_version"] for row in decisions}) == len(decisions)


def test_revision_order_is_total_and_correction_cycles_block_readiness(tmp_path: Path) -> None:
    base = json.loads((ROOT / "examples/data/operational-events.jsonl").read_text().splitlines()[0])
    versions = [{**base, "source_revision": revision, "payload": {"quantity": qty}}
                for revision, qty in (("1", 1), ("01", 2))]
    snapshots = []
    for i, records in enumerate((versions, versions[::-1])):
        store = SQLiteEventStore(tmp_path / f"events{i}.sqlite")
        store.ingest(parse_event(row) for row in records)
        snapshots.append(SnapshotBuilder(store).as_known_at(datetime(2026, 1, 10, tzinfo=UTC)))
    assert snapshots[0].snapshot_id == snapshots[1].snapshot_id
    store = SQLiteEventStore(tmp_path / "cycle.sqlite")
    store.ingest([parse_event({**base, "event_id": "a", "supersedes_event_id": "b"}),
                  parse_event({**base, "event_id": "b", "supersedes_event_id": "a"})])
    assert not SnapshotBuilder(store).as_known_at(datetime(2026, 1, 10, tzinfo=UTC)).ready


def test_remote_deployment_refuses_legacy_unbounded_compute(tmp_path: Path) -> None:
    settings = ServiceSettings(local_only=False, api_token="dedicated-test-token")
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path, settings=settings)) as client:
        response = client.post("/api/run", headers={"Authorization": "Bearer dedicated-test-token"}, json={"model": "spring", "reps": 1})
        assert response.status_code == 403
