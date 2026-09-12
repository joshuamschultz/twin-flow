from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from twinflow.application.backup import backup_workspace, restore_workspace
from twinflow.application.repository import WorkspaceRepository
from twinflow.application.workspace import Workspace
from twinflow.cli.admin import main as admin_main
from twinflow.service.app import create_app
from twinflow.service.settings import ServiceSettings


def _settings(max_request_bytes: int = 5_242_880) -> ServiceSettings:
    return ServiceSettings(
        api_token=_token(),
        cors_origins=("https://planner.example",),
        allowed_hosts=("testserver",),
        max_request_bytes=max_request_bytes,
    )


def _token() -> str:
    return "-".join(("secure", "test", "token", "123456789"))


def test_auth_protects_api_schema_and_docs_but_not_probes(tmp_path: Path) -> None:
    with TestClient(
        create_app("examples", workspace_root=tmp_path, settings=_settings())
    ) as client:
        for path in ("/api/health", "/api/workspace/schema", "/openapi.json", "/docs"):
            response = client.get(path)
            assert response.status_code == 401
            assert "a-secure-test-token" not in response.text
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ready"}
        response = client.get(
            "/api/workspace/schema",
            headers={
                "Authorization": f"Bearer {_token()}",
                "X-Correlation-ID": "case-9",
            },
        )
        assert response.status_code == 200
        assert response.headers["x-correlation-id"] == "case-9"


def test_cors_hosts_and_request_size_are_restrictive(tmp_path: Path) -> None:
    settings = _settings(max_request_bytes=100)
    with TestClient(create_app("examples", workspace_root=tmp_path, settings=settings)) as client:
        headers = {"Authorization": f"Bearer {_token()}"}
        allowed = client.get(
            "/api/health", headers={**headers, "Origin": "https://planner.example"}
        )
        assert allowed.headers["access-control-allow-origin"] == "https://planner.example"
        denied = client.get(
            "/api/health", headers={**headers, "Origin": "https://attacker.example"}
        )
        assert "access-control-allow-origin" not in denied.headers
        assert client.get("/health", headers={"Host": "attacker.example"}).status_code == 400
        oversized = client.post(
            "/api/workspace/scenarios",
            headers=headers,
            content=json.dumps({"name": "large", "content": "x" * 200}),
        )
        assert oversized.status_code == 413


def test_nonloopback_binding_requires_environment_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWINFLOW_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="non-loopback"):
        ServiceSettings.from_env(host="service.example")
    monkeypatch.setenv("TWINFLOW_API_TOKEN", "configured-remote-token")
    assert ServiceSettings.from_env(host="service.example").local_only is False


def test_idempotent_replay_succeeds_when_queue_is_full(tmp_path: Path) -> None:
    repository = WorkspaceRepository(tmp_path)
    first = {"id": "job-a", "status": "running"}
    assert repository.claim_job("same", "digest", first, max_active_jobs=1) == ("job-a", True)
    assert repository.claim_job(
        "same", "digest", {"id": "job-new", "status": "queued"}, max_active_jobs=1
    ) == ("job-a", False)
    with pytest.raises(ValueError, match="queue is full"):
        repository.claim_job(
            "different",
            "digest-2",
            {"id": "job-b", "status": "queued"},
            max_active_jobs=1,
        )


def test_terminal_transition_is_not_overwritten_and_restart_recovers(tmp_path: Path) -> None:
    repository = WorkspaceRepository(tmp_path)
    for job_id, status in (("running", "running"), ("cancel", "cancel_requested")):
        repository.create("jobs", job_id, {"id": job_id, "status": status})
    workspace = Workspace(tmp_path)
    try:
        assert repository.get("jobs", "running")["status"] == "interrupted"
        assert repository.get("jobs", "cancel")["status"] == "canceled"
        assert repository.transition_job("cancel", ("running",), {"status": "completed"}) is None
    finally:
        workspace.close()


def test_second_process_owner_is_refused(tmp_path: Path) -> None:
    first = Workspace(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="already owned"):
            Workspace(tmp_path)
    finally:
        first.close()


def test_backup_restore_round_trip_and_manifest_validation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repository = WorkspaceRepository(source)
    repository.create("scenarios", "scenario-1", {"id": "scenario-1", "name": "Proof"})
    repository.append_audit("2026-01-01T00:00:00Z", "import", "scenario", "scenario-1", "created")
    artifact = source / "artifacts" / "job-1" / "evidence.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"metric": 4}', encoding="utf-8")
    archive = tmp_path / "backup.zip"
    manifest = backup_workspace(source, archive)
    assert "artifacts/job-1/evidence.json" in manifest["files"]

    restored = tmp_path / "restored"
    restore_workspace(archive, restored)
    assert WorkspaceRepository(restored).get("scenarios", "scenario-1")["name"] == "Proof"
    assert (restored / "artifacts" / "job-1" / "evidence.json").read_text() == '{"metric": 4}'

    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w") as output:
        output.writestr("manifest.json", '{"schema_version": 1, "files": {}}')
        output.writestr("../escape", "bad")
    with pytest.raises(ValueError, match="Unsafe backup member"):
        restore_workspace(malicious, tmp_path / "unsafe")


def test_admin_cli_backup_and_restore(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "source"
    WorkspaceRepository(source).create("drafts", "d1", {"id": "d1"})
    archive = tmp_path / "backup.zip"
    assert admin_main(["backup", "--workspace", str(source), "--out", str(archive)]) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1
    restored = tmp_path / "restored"
    assert admin_main(["restore", str(archive), "--workspace", str(restored)]) == 0
    assert WorkspaceRepository(restored).get("drafts", "d1")["id"] == "d1"
