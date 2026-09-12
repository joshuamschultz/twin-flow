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
