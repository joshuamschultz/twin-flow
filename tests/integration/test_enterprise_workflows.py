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
        time.sleep(0.01)
    pytest.fail("Experiment did not finish within the test budget")


@pytest.mark.parametrize(
    "example,domain",
    [
        ("spring", "manufacturing"),
        ("capsule-office", "office"),
        ("capsule-supply-manufacturing", "supply_chain"),
        ("capsule-supply-distribution", "supply_chain"),
    ],
)
def test_domains_import_run_query_export_and_compare(
    tmp_path: Path, example: str, domain: str
) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        response = client.post("/api/workspace/examples/load", json={"example_id": example})
        assert response.status_code == 201, response.text
        scenario = response.json()
        assert scenario["summary"]["domain"] == domain
        job = client.post(
            f"/api/workspace/scenarios/{scenario['id']}/evaluate",
            json={"reps": 2, "seed": 42, "request_key": "baseline"},
        ).json()
        completed = _wait(client, job["id"])
        assert completed["result"]["domain"] == domain
        evidence = client.post(f"/api/workspace/jobs/{job['id']}/query", json={"topic": "dates"})
        assert evidence.status_code == 200 and evidence.json()["evidence"]
        assert evidence.json()["scenario_digest"] == scenario["digest"]
        assert client.get(f"/api/workspace/jobs/{job['id']}/export").json()["result"]
        compared = client.post(
            "/api/workspace/compare", json={"baseline_id": job["id"], "candidate_id": job["id"]}
        )
        assert all(value == 0 for value in compared.json()["delta"].values())


def test_agent_data_answers_and_schedule_share_transport_contract(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        contracts = client.get("/api/workspace/tool-contracts").json()
        records = contracts["event_example"]
        imported = client.post("/api/workspace/data/events", json={"records": records})
        assert imported.status_code == 201 and imported.json()["inserted"] == len(records)
        assert client.post("/api/workspace/data/events", json={"records": records}).json()[
            "duplicates"
        ] == len(records)
        malformed = client.post("/api/workspace/data/events", json={"records": [{}]})
        assert malformed.status_code == 422
        conflict = json.loads(json.dumps(records[0]))
        conflict["payload"]["quantity"] = 999
        assert (
            client.post("/api/workspace/data/events", json={"records": [conflict]}).status_code
            == 409
        )
        snapshot = client.post(
            "/api/workspace/data/snapshots", json={"known_at": "2026-01-10T00:00:00Z"}
        ).json()
        stock = next(row for row in snapshot["entities"] if row["entity_type"] == "stock")
        assert stock["values"]["quantity"] == 100
        draft = client.post(
            "/api/workspace/drafts", json={"name": "Intake", "facts": {"process": "approve"}}
        ).json()
        revision = client.post(
            f"/api/workspace/drafts/{draft['id']}/answers",
            json={"answers": [{"id": "resources", "answer": "one reviewer"}]},
        ).json()
        assert revision["id"] != draft["id"]
        assert "resources" not in {row["id"] for row in revision["questions"]}
        assert (
            client.post("/api/workspace/validate", json={"content": "{}"}).json()["valid"] is False
        )
        scheduled = client.post(
            "/api/workspace/schedules", json={"problem": contracts["scheduling_example"]}
        )
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
    versions = [
        {**base, "source_revision": revision, "payload": {"quantity": qty}}
        for revision, qty in (("1", 1), ("01", 2))
    ]
    snapshots = []
    for i, records in enumerate((versions, versions[::-1])):
        store = SQLiteEventStore(tmp_path / f"events{i}.sqlite")
        store.ingest(parse_event(row) for row in records)
        snapshots.append(SnapshotBuilder(store).as_known_at(datetime(2026, 1, 10, tzinfo=UTC)))
    assert snapshots[0].snapshot_id == snapshots[1].snapshot_id
    store = SQLiteEventStore(tmp_path / "cycle.sqlite")
    store.ingest(
        [
            parse_event({**base, "event_id": "a", "supersedes_event_id": "b"}),
            parse_event({**base, "event_id": "b", "supersedes_event_id": "a"}),
        ]
    )
    assert not SnapshotBuilder(store).as_known_at(datetime(2026, 1, 10, tzinfo=UTC)).ready


def test_remote_deployment_refuses_legacy_unbounded_compute(tmp_path: Path) -> None:
    settings = ServiceSettings(local_only=False, api_token="dedicated-test-token")  # noqa: S106 - fixture
    with TestClient(
        create_app(ROOT / "examples", workspace_root=tmp_path, settings=settings)
    ) as client:
        response = client.post(
            "/api/run",
            headers={"Authorization": "Bearer dedicated-test-token"},
            json={"model": "spring", "reps": 1},
        )
        assert response.status_code == 403


def test_exact_review_and_dry_run_receipt_are_evidence_bound(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as client:
        scenario = client.post(
            "/api/workspace/examples/load", json={"example_id": "capsule-office"}
        ).json()
        job = client.post(
            f"/api/workspace/scenarios/{scenario['id']}/evaluate",
            json={"reps": 1, "seed": 42, "request_key": "baseline"},
        ).json()
        _wait(client, job["id"])
        proposal = client.post(
            "/api/workspace/proposals",
            json={"job_id": job["id"], "action": {"type": "reschedule", "priority": 2}},
        ).json()
        path = f"/api/workspace/proposals/{proposal['id']}"
        assert (
            client.post(
                path + "/approve", json={"reviewed_digest": "changed", "reviewer": "test"}
            ).status_code
            == 409
        )
        approval = client.post(
            path + "/approve",
            json={"reviewed_digest": proposal["proposal"]["digest"], "reviewer": "test"},
        ).json()
        body = {
            "approval_id": approval["approval_id"],
            "request_key": "dry-1",
            "current_revision": proposal["proposal"]["expected_operational_revision"],
        }
        first = client.post(path + "/dry-run", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["entry"]["status"] == "sent"
        assert first.json()["receipts"][0]["detail"] == "dry run; no external operation performed"
        replay = client.post(path + "/dry-run", json=body).json()
        assert replay["entry"]["entry_id"] == first.json()["entry"]["entry_id"]
        assert replay["receipts"] == []


def test_single_file_cli_uses_embedded_experiment_and_retains_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from twinflow.cli.main import main

    capsule = validate_content((ROOT / "examples/capsules/office.twin.yaml").read_text()).to_dict()
    capsule["experiment"].update(replications=2, seed=17)
    source = tmp_path / "office.json"
    source.write_text(json.dumps(capsule))
    output = tmp_path / "evidence"
    assert main(["scenario", "run", str(source), "--out", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["replications"] == 2 and result["seed"] == 17
    assert (output / "result.json").is_file()
    assert main(["scenario", "run", str(source), "--out", str(output)]) == 1


def test_manufacturing_capsule_evaluate_honors_artifact_and_limits(tmp_path: Path) -> None:
    from twinflow.plan.driver import RunResult

    capsule = validate_content((ROOT / "examples/capsules/spring.twin.yaml").read_text())
    result = capsule.evaluate(seed=42, artifact_dir=str(tmp_path), limits={"max_events": 1})
    assert isinstance(result, RunResult)
    assert result.outcome == "incomplete_at_horizon"
    assert result.termination_reason == "event_limit"
    assert result.event_log_path.is_relative_to(tmp_path)
    assert result.event_log_path.is_file()


def test_evidence_query_pages_and_repository_quotas(tmp_path: Path) -> None:
    from twinflow.application.repository import WorkspaceRepository

    repo = WorkspaceRepository(tmp_path / "quota", max_records_per_kind=1)
    repo.create("drafts", "first", {"id": "first"})
    with pytest.raises(ValueError, match="quota"):
        repo.create("drafts", "second", {"id": "second"})
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path / "app")) as client:
        scenario = client.post(
            "/api/workspace/examples/load", json={"example_id": "capsule-office"}
        ).json()
        job = client.post(
            f"/api/workspace/scenarios/{scenario['id']}/evaluate",
            json={"reps": 2, "seed": 42, "request_key": "page"},
        ).json()
        _wait(client, job["id"])
        path = f"/api/workspace/jobs/{job['id']}/query"
        first = client.post(path, json={"topic": "dates", "limit": 1}).json()
        second = client.post(
            path, json={"topic": "dates", "limit": 1, "offset": first["next_offset"]}
        ).json()
        assert first["total"] == 2 and len(first["evidence"]) == 1
        assert first["evidence"] != second["evidence"] and second["next_offset"] is None


def test_concurrent_event_retries_are_idempotent(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    source = json.loads(
        (ROOT / "examples/data/operational-events.jsonl").read_text().splitlines()[0]
    )
    event = parse_event(source)
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.ingest([event]), range(8)))
    assert sum(result.inserted for result in results) == 1
    assert sum(result.duplicates for result in results) == 7
