"""MCP streamable HTTP mounted in the service app (T-212, COMP-119, COMP-120).

Requirements: REQ-161 (one process serves `/api` and `/mcp`; tools run in process),
REQ-173 (`/mcp` refuses a Host or Origin outside the allowlist).

Contract pinned here (orchestrator design for T-212):

* `create_app(models_root, workspace_root=..., settings=...)` serves the MCP streamable
  HTTP endpoint at exactly `/mcp` (a POST to `/mcp` is answered there, not redirected to
  `/mcp/`), stateless (`stateless_http=True`: every POST stands alone, no session id is
  needed) with JSON responses (`json_response=True`), and runs the MCP session manager
  inside the FastAPI lifespan (so the tests enter `TestClient` with `with`).
* `TransportSecuritySettings(enable_dns_rebinding_protection=True,
  allowed_hosts=settings.allowed_hosts plus each host's `host:*` port form,
  allowed_origins=settings.mcp_allowed_origins)`.
* Host refusal status: the app's outer `TrustedHostMiddleware` answers a foreign Host with
  400 before the MCP SDK's own check (421, `mcp.server.transport_security`) can run. Either
  is a refusal before any tool runs, so the test accepts exactly {400, 421} and also pins
  that an allowed host with a port (`testserver:8000`) is served (the SDK needs the
  `host:*` form for that).
* Origin refusal status: 403 (the SDK's `Invalid Origin header`; nothing in front of it
  checks Origin). An origin in `settings.mcp_allowed_origins` is served.
* `/api/health` reports `"mcp": "available"`; with the `agent` extra unimportable
  (`sys.modules["mcp"] = None` in a fresh interpreter) it reports `"mcp": "unavailable"`,
  the app still starts, and `/mcp` is not mounted (404).
* The tool guard receives the HTTP caller from `ServiceBoundaryMiddleware` (scope checks use
  the token's scopes; audit rows carry its caller id) and a real `CallerLimiter`
  (`settings.calls_per_minute`, burst 20).

MCP JSON-RPC is sent as raw POSTs (initialize, then tools/list or tools/call) with
`Accept: application/json, text/event-stream`, `Content-Type: application/json` and the
negotiated `MCP-Protocol-Version`. Full official-client conformance is T-213.
"""

# ruff: noqa: E402 -- the agent extra is optional: skip before importing anything needing `mcp`.

from __future__ import annotations

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")

import json
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from mcp import types
from twinflow.mcp.tools import CATALOGUE
from twinflow.service.app import create_app
from twinflow.service.auth import add_token
from twinflow.service.settings import ServiceSettings

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
ALLOWED_ORIGIN = "http://localhost:5173"
EVIL = "evil.example"
JSON_RPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
PROTOCOL_HEADER = "MCP-Protocol-Version"
SUBPROCESS_TIMEOUT_SECONDS = 120


# --- harness ------------------------------------------------------------------------------------


def _settings(**overrides: Any) -> ServiceSettings:
    base: dict[str, Any] = {
        "mcp_allowed_origins": (ALLOWED_ORIGIN,),
        "calls_per_minute": 10_000,
    }
    base.update(overrides)
    return ServiceSettings(**base)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """The app in open local mode (no registry: every caller is `local`)."""
    app = create_app(EXAMPLES, workspace_root=tmp_path / "workspace", settings=_settings())
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _initialize_body() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": types.LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "twinflow-tests", "version": "1"},
        },
    }


def _post(client: TestClient, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    return client.post("/mcp", json=body, headers={**JSON_RPC_HEADERS, **(headers or {})})


def _initialize(client: TestClient, headers: dict[str, str] | None = None) -> str:
    """Initialize and return the negotiated protocol version."""
    response = _post(client, _initialize_body(), headers)
    assert response.status_code == 200, (response.status_code, response.text[:500])
    result = response.json()["result"]
    assert result["serverInfo"]["name"] == "Twinflow", result
    return str(result["protocolVersion"])


def _rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
    *,
    request_id: int = 2,
) -> dict[str, Any]:
    """One JSON-RPC call after initialize; returns the `result` object."""
    version = _initialize(client, headers)
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    response = _post(client, body, {**(headers or {}), PROTOCOL_HEADER: version})
    assert response.status_code == 200, (response.status_code, response.text[:500])
    payload = response.json()
    assert "result" in payload, payload
    result: dict[str, Any] = payload["result"]
    return result


def _tool_error_code(result: dict[str, Any]) -> str:
    assert result.get("isError") is True, result
    structured = result.get("structuredContent")
    if structured is None:
        structured = json.loads(result["content"][0]["text"])
    return str(structured["code"])


def _audit_rows(workspace: Path) -> list[tuple[str, str, str]]:
    """(caller_id, tool, outcome) of every tool-call audit row."""
    uri = f"file:{workspace / 'workspace.sqlite3'}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute(
            "SELECT caller_id, tool, outcome FROM audit_events "
            "WHERE operation = 'tool_call' ORDER BY sequence"
        ).fetchall()
    return [(str(c), str(t), str(o)) for c, t, o in rows]


# --- one process serves /api and /mcp (REQ-161) -----------------------------------------------


def test_create_app_serves_api_health_and_mcp_initialize_in_one_process(
    client: TestClient,
) -> None:
    health = client.get("/api/health")
    version = _initialize(client)

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert version  # negotiated by the mounted MCP server, not by a proxy


def test_mcp_endpoint_is_exactly_slash_mcp_without_redirect(client: TestClient) -> None:
    response = _post(client, _initialize_body())

    assert response.status_code == 200, (response.status_code, response.headers.get("location"))
    assert response.headers["content-type"].startswith("application/json")


def test_api_health_reports_mcp_available(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mcp": "available"}


def test_tools_list_over_http_equals_the_catalogue(client: TestClient) -> None:
    result = _rpc(client, "tools/list", {})

    names = [tool["name"] for tool in result["tools"]]
    assert sorted(names) == sorted(spec.name for spec in CATALOGUE)
    assert len(names) == len(set(names))


def test_tools_call_over_http_runs_the_tool_in_process(client: TestClient) -> None:
    result = _rpc(client, "tools/call", {"name": "describe_capabilities", "arguments": {}})

    assert result.get("isError") is False, result
    assert result["structuredContent"]["tool_timeouts"].keys() == {spec.name for spec in CATALOGUE}


def test_api_health_reports_mcp_unavailable_and_mcp_unmounted_without_agent_extra(
    tmp_path: Path,
) -> None:
    script = textwrap.dedent(
        """
        import json, sys
        sys.modules["mcp"] = None  # the agent extra is not installed
        from starlette.testclient import TestClient
        from twinflow.service.app import create_app

        app = create_app(sys.argv[1], workspace_root=sys.argv[2])
        with TestClient(app, follow_redirects=False) as client:
            health = client.get("/api/health")
            mcp = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
            print(json.dumps({"health_status": health.status_code, "health": health.json(),
                              "mcp_status": mcp.status_code}))
        """
    )

    completed = subprocess.run(  # noqa: S603 - fixed argv: this interpreter, literal args
        [sys.executable, "-c", script, str(EXAMPLES), str(tmp_path / "workspace")],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        cwd=ROOT,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr[-3000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["health_status"] == 200
    assert report["health"] == {"status": "ok", "mcp": "unavailable"}
    assert report["mcp_status"] == 404


# --- Host and Origin allowlists (REQ-173) -----------------------------------------------------


def test_mcp_refuses_a_host_outside_the_allowlist(client: TestClient) -> None:
    allowed = _post(client, _initialize_body())
    refused = _post(client, _initialize_body(), {"Host": EVIL})

    assert allowed.status_code == 200
    assert refused.status_code in (400, 421), refused.status_code
    assert "jsonrpc" not in refused.text


def test_mcp_serves_an_allowed_host_with_a_port(client: TestClient) -> None:
    response = _post(client, _initialize_body(), {"Host": "testserver:8000"})

    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert response.json()["result"]["serverInfo"]["name"] == "Twinflow"


def test_mcp_refuses_an_origin_outside_the_allowlist_with_403(client: TestClient) -> None:
    refused = _post(client, _initialize_body(), {"Origin": f"http://{EVIL}"})

    assert refused.status_code == 403, (refused.status_code, refused.text[:300])
    assert "jsonrpc" not in refused.text


def test_mcp_serves_an_origin_in_the_allowlist(client: TestClient) -> None:
    response = _post(client, _initialize_body(), {"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 200, (response.status_code, response.text[:300])


def test_mcp_refuses_tools_call_from_a_foreign_origin_and_runs_no_tool(
    client: TestClient, tmp_path: Path
) -> None:
    version = _initialize(client)
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "describe_capabilities", "arguments": {}},
    }
    refused = _post(client, body, {"Origin": f"http://{EVIL}", PROTOCOL_HEADER: version})

    assert refused.status_code == 403
    assert _audit_rows(tmp_path / "workspace") == []


# --- the guard: HTTP caller and real limiter --------------------------------------------------


@pytest.fixture
def registry_client(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, str], Path]]:
    """The app with a token registry: `reader` (read) and `planner` (read, plan)."""
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    tokens_file = secrets / "tokens.json"
    tokens = {
        "reader": add_token(tokens_file, caller_id="reader", scopes=["read"], days=None),
        "planner": add_token(tokens_file, caller_id="planner", scopes=["read", "plan"], days=None),
    }
    workspace = tmp_path / "workspace"
    settings = _settings(tokens_file=tokens_file, calls_per_minute=1)
    app = create_app(EXAMPLES, workspace_root=workspace, settings=settings)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client, tokens, workspace


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_http_guard_uses_the_token_scopes_and_audits_the_token_caller(
    registry_client: tuple[TestClient, dict[str, str], Path],
) -> None:
    client, tokens, workspace = registry_client
    params = {"name": "put_planning_profile", "arguments": {"profile_toml": "[erp]\n"}}

    result = _rpc(client, "tools/call", params, _bearer(tokens["reader"]))

    assert _tool_error_code(result) == "forbidden"
    assert _audit_rows(workspace) == [("reader", "put_planning_profile", "denied")]


def test_http_guard_applies_the_configured_call_rate(
    registry_client: tuple[TestClient, dict[str, str], Path],
) -> None:
    client, tokens, workspace = registry_client
    headers = _bearer(tokens["reader"])
    version = _initialize(client, headers)
    codes: list[str | None] = []
    for request_id in range(2, 27):  # burst 20 at 1 call/minute: the 21st call is refused
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": "describe_capabilities", "arguments": {}},
        }
        response = _post(client, body, {**headers, PROTOCOL_HEADER: version})
        assert response.status_code == 200, response.text[:300]
        result = response.json()["result"]
        codes.append(_tool_error_code(result) if result.get("isError") else None)

    assert codes[:20] == [None] * 20, codes
    assert set(codes[20:]) == {"rate_limited"}, codes
    outcomes = [outcome for caller, _, outcome in _audit_rows(workspace) if caller == "reader"]
    assert outcomes.count("rate_limited") == 5
