from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from twinflow.application.backup import backup_workspace, restore_workspace
from twinflow.application.repository import WorkspaceRepository
from twinflow.application.workspace import Workspace
from twinflow.cli.admin import main as admin_main
from twinflow.service.app import create_app
from twinflow.service.middleware import ServiceBoundaryMiddleware
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
    with pytest.raises(ValueError, match="requires an API token"):
        ServiceSettings(local_only=False)
    monkeypatch.delenv("TWINFLOW_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="requires an API token"):
        ServiceSettings.from_env(host="service.example")
    monkeypatch.setenv("TWINFLOW_API_TOKEN", "configured-remote-token")
    assert ServiceSettings.from_env(host="service.example").local_only is False
    with pytest.raises(ValueError, match="wildcard"):
        ServiceSettings(cors_origins=("*",))


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
    for name in ("operational-data.sqlite", "actions.sqlite3"):
        with sqlite3.connect(source / name) as database:
            database.execute("CREATE TABLE proof (value TEXT)")
            database.execute("INSERT INTO proof VALUES ('included')")
    archive = tmp_path / "backup.zip"
    manifest = backup_workspace(source, archive)
    assert "artifacts/job-1/evidence.json" in manifest["files"]
    assert "operational-data.sqlite" in manifest["files"]
    assert "actions.sqlite3" in manifest["files"]

    restored = tmp_path / "restored"
    restore_workspace(archive, restored)
    assert WorkspaceRepository(restored).get("scenarios", "scenario-1")["name"] == "Proof"
    assert (restored / "artifacts" / "job-1" / "evidence.json").read_text() == '{"metric": 4}'
    for name in ("operational-data.sqlite", "actions.sqlite3"):
        with sqlite3.connect(restored / name) as database:
            assert database.execute("SELECT value FROM proof").fetchone() == ("included",)

    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w") as output:
        output.writestr("manifest.json", '{"schema_version": 1, "files": {}}')
        output.writestr("../escape", "bad")
    with pytest.raises(ValueError, match="Unsafe backup member"):
        restore_workspace(malicious, tmp_path / "unsafe")


def test_backup_requires_quiescence_and_safe_source_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    WorkspaceRepository(source)
    active = Workspace(source)
    try:
        with pytest.raises(ValueError, match="stop the service"):
            backup_workspace(source, tmp_path / "active.zip")
    finally:
        active.close()
    with pytest.raises(ValueError, match="outside"):
        backup_workspace(source, source / "artifacts" / "backup.zip")
    with pytest.raises(ValueError, match="does not exist"):
        backup_workspace(tmp_path / "missing", tmp_path / "missing.zip")

    external = tmp_path / "external.sqlite"
    with sqlite3.connect(external) as database:
        database.execute("CREATE TABLE proof (value TEXT)")
    (source / "operational-data.sqlite").symlink_to(external)
    with pytest.raises(ValueError, match="symlink database"):
        backup_workspace(source, tmp_path / "symlink.zip")


def test_restore_streams_members_and_rejects_bad_manifest_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    WorkspaceRepository(source)
    archive = tmp_path / "valid.zip"
    backup_workspace(source, archive)

    def forbidden_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("restore must stream members")

    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden_read)
    restore_workspace(archive, tmp_path / "streamed")
    monkeypatch.undo()

    invalid = tmp_path / "invalid-manifest.zip"
    with zipfile.ZipFile(invalid, "w") as output:
        output.writestr(
            "manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "files": {"workspace.sqlite3": {"sha256": "bad", "size": True}},
                }
            ),
        )
        output.writestr("workspace.sqlite3", b"not-a-database")
    with pytest.raises(ValueError, match="metadata is invalid"):
        restore_workspace(invalid, tmp_path / "invalid")

    target_directory = tmp_path / "target-directory"
    target_directory.mkdir()
    target_link = tmp_path / "target-link"
    target_link.symlink_to(target_directory, target_is_directory=True)
    with pytest.raises(ValueError, match="target must not be a symlink"):
        restore_workspace(archive, target_link)

    corrupted = tmp_path / "corrupted.zip"
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(corrupted, "w") as output:
        for info in original.infolist():
            content = original.read(info.filename)
            if info.filename == "workspace.sqlite3":
                content = b"x" + content[1:]
            output.writestr(info.filename, content)
    with pytest.raises(ValueError, match="digest mismatch"):
        restore_workspace(corrupted, tmp_path / "corrupted")


def test_chunked_body_limit_negative_length_and_post_start_failure() -> None:
    async def consuming_app(scope: Scope, receive: object, send: object) -> None:
        receive_message = cast(Callable[[], Awaitable[Message]], receive)
        send_message = cast(Callable[[Message], Awaitable[None]], send)
        while (await receive_message()).get("type") == "http.request":
            pass
        await send_message({"type": "http.response.start", "status": 200, "headers": []})
        await send_message({"type": "http.response.body", "body": b"ok"})

    chunked = _run_asgi(
        ServiceBoundaryMiddleware(consuming_app, api_token=None, max_request_bytes=5),
        [(b"abc", True), (b"def", False)],
    )
    assert cast(int, chunked[0]["status"]) == 413

    negative = _run_asgi(
        ServiceBoundaryMiddleware(consuming_app, api_token=None, max_request_bytes=5),
        [],
        headers=[(b"content-length", b"-1")],
    )
    assert cast(int, negative[0]["status"]) == 400

    sent: list[Message] = []

    async def failing_app(scope: Scope, receive: object, send: object) -> None:
        send_message = cast(Callable[[Message], Awaitable[None]], send)
        await send_message({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("after start")

    with pytest.raises(RuntimeError, match="after start"):
        _run_asgi(
            ServiceBoundaryMiddleware(failing_app, api_token=None, max_request_bytes=5),
            [],
            sent=sent,
        )
    assert sum(message["type"] == "http.response.start" for message in sent) == 1


def test_admin_cli_backup_and_restore(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "source"
    WorkspaceRepository(source).create("drafts", "d1", {"id": "d1"})
    archive = tmp_path / "backup.zip"
    assert admin_main(["backup", "--workspace", str(source), "--out", str(archive)]) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1
    restored = tmp_path / "restored"
    assert admin_main(["restore", str(archive), "--workspace", str(restored)]) == 0
    assert WorkspaceRepository(restored).get("drafts", "d1")["id"] == "d1"


def _run_asgi(
    app: object,
    chunks: list[tuple[bytes, bool]],
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    sent: list[Message] | None = None,
) -> list[Message]:
    messages = [
        {"type": "http.request", "body": body, "more_body": more} for body, more in chunks
    ] or [{"type": "http.request", "body": b"", "more_body": False}]
    output = sent if sent is not None else []

    async def run() -> None:
        index = 0

        async def receive() -> Message:
            nonlocal index
            if index >= len(messages):
                return {"type": "http.disconnect"}
            message = cast(Message, messages[index])
            index += 1
            return message

        async def send(message: Message) -> None:
            output.append(message)

        scope = cast(
            Scope,
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "method": "POST",
                "scheme": "http",
                "path": "/upload",
                "raw_path": b"/upload",
                "query_string": b"",
                "headers": headers or [],
                "client": ("127.0.0.1", 1),
                "server": ("testserver", 80),
                "root_path": "",
                "http_version": "1.1",
            },
        )
        callable_app = cast(Callable[[Scope, object, object], Awaitable[None]], app)
        await callable_app(scope, receive, send)

    asyncio.run(run())
    return output
