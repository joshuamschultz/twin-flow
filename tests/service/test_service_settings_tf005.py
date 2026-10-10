"""ServiceSettings for TF-005 (SDD "Settings added to ServiceSettings"; T-209, T-210).

Requirements: REQ-172, REQ-173, REQ-175. Decision: D-117 deletes the shared
``TWINFLOW_API_TOKEN`` / ``ServiceSettings.api_token`` outright (no shim).

Pinned choices beyond the orchestrator API:
- New numeric bounds must be positive (``ValueError``), like the existing ones.
- ``"*"`` in ``mcp_allowed_origins`` is refused, like the existing CORS/host rule
  (a wildcard defeats the DNS-rebinding defence of REQ-173).
- ``create_app`` loads the registry from ``settings.tokens_file`` and enforces it even on
  loopback; with no ``tokens_file`` the app stays in open local mode.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from twinflow.service.app import create_app
from twinflow.service.settings import ServiceSettings

SRC = Path(__file__).resolve().parents[2] / "src"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _token() -> str:
    return "-".join(("tf005", "settings", "fake", "bearer", "0123456789"))


def _registry(path: Path) -> Path:
    entry = {
        "caller_id": "arc",
        "token_sha256": hashlib.sha256(_token().encode()).hexdigest(),
        "scopes": ["read", "plan"],
        "not_after": None,
    }
    path.write_text(json.dumps({"tokens": [entry]}), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TWINFLOW_TOKENS_FILE",
        "TWINFLOW_SIGNING_KEY_FILE",
        "TWINFLOW_MCP_ALLOWED_ORIGINS",
        "TWINFLOW_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- fields


def test_new_fields_have_pinned_defaults() -> None:
    settings = ServiceSettings()

    assert settings.tokens_file is None
    assert settings.max_mcp_request_bytes == 16_777_216
    assert settings.max_tool_result_bytes == 1_048_576
    assert settings.max_solve_seconds == 120.0
    assert settings.calls_per_minute == 120
    assert settings.max_concurrent_solves_per_caller == 2
    assert settings.mcp_allowed_origins == ()
    assert settings.signing_key_file is None


def test_api_token_field_is_deleted() -> None:
    names = {field.name for field in dataclasses.fields(ServiceSettings)}

    assert "api_token" not in names


def test_shared_token_names_appear_nowhere_under_src() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "TWINFLOW_API_TOKEN" in text or "api_token" in text:
            offenders.append(str(path.relative_to(SRC)))

    assert offenders == []


@pytest.mark.parametrize(
    "field",
    [
        "max_mcp_request_bytes",
        "max_tool_result_bytes",
        "max_solve_seconds",
        "calls_per_minute",
        "max_concurrent_solves_per_caller",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_new_bounds_must_be_positive(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        ServiceSettings(**{field: value})  # type: ignore[arg-type]  # dynamic field under test


def test_wildcard_mcp_origin_is_refused() -> None:
    with pytest.raises(ValueError, match="wildcard"):
        ServiceSettings(mcp_allowed_origins=("*",))


# --------------------------------------------------------------------------- from_env


def test_from_env_reads_tokens_signing_key_and_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = _registry(tmp_path / "tokens.json")
    key = tmp_path / "signing.key"
    monkeypatch.setenv("TWINFLOW_TOKENS_FILE", str(tokens))
    monkeypatch.setenv("TWINFLOW_SIGNING_KEY_FILE", str(key))
    monkeypatch.setenv("TWINFLOW_MCP_ALLOWED_ORIGINS", " https://arc.example ,https://ops.example,")

    settings = ServiceSettings.from_env(host="127.0.0.1")

    assert settings.tokens_file == tokens
    assert settings.signing_key_file == key
    assert settings.mcp_allowed_origins == ("https://arc.example", "https://ops.example")


def test_from_env_loopback_without_registry_is_open_local_mode() -> None:
    settings = ServiceSettings.from_env(host="127.0.0.1")

    assert settings.local_only is True
    assert settings.tokens_file is None


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "service.example", "::"])  # noqa: S104 - refusal under test
def test_from_env_non_loopback_without_registry_refuses_to_start(host: str) -> None:
    with pytest.raises(ValueError):
        ServiceSettings.from_env(host=host)


def test_from_env_ignores_the_deleted_shared_token_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWINFLOW_API_TOKEN", "configured-remote-token-0123456789")

    with pytest.raises(ValueError):
        ServiceSettings.from_env(host="service.example")


def test_from_env_non_loopback_with_registry_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = _registry(tmp_path / "tokens.json")
    monkeypatch.setenv("TWINFLOW_TOKENS_FILE", str(tokens))

    settings = ServiceSettings.from_env(host="service.example")

    assert settings.local_only is False
    assert settings.tokens_file == tokens


# --------------------------------------------------------------------------- app wiring


def test_app_enforces_registry_from_tokens_file(tmp_path: Path) -> None:
    settings = ServiceSettings(tokens_file=_registry(tmp_path / "tokens.json"))
    app = create_app(EXAMPLES, workspace_root=tmp_path / "ws", settings=settings)

    with TestClient(app) as client:
        anonymous = client.get("/api/health")
        authorised = client.get("/api/health", headers={"Authorization": f"Bearer {_token()}"})
        probe = client.get("/health")

    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"].startswith("Bearer")
    assert authorised.status_code == 200
    assert probe.status_code == 200


def test_app_without_tokens_file_stays_open_on_loopback(tmp_path: Path) -> None:
    app = create_app(EXAMPLES, workspace_root=tmp_path / "ws", settings=ServiceSettings())

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
