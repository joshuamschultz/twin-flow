"""TF-005 ruling D: the `admin` scope, scoped planning writes, and the 90-day token default.

Pinned contract:

* `SCOPES == {"read", "plan", "admin"}`; a registry entry may list `admin`; unknown scopes
  are still refused. Scopes are not hierarchical.
* `put_planning_profile` and `put_k_table` need `admin` (catalogue `scope`, tools/list
  `_meta["twinflow/scope"]`), yet stay `state_modifying`. Every other tool keeps
  `read_only -> read`, `state_modifying -> plan`.
* A caller without `admin` gets MCP `forbidden` (audit outcome `denied`) and REST 403.
* `twinflow admin token new` defaults to 90 days; `--days N` sets N; `--no-expiry` stores
  null; `--days` with `--no-expiry` is refused with exit 2.
* AGENT-GUIDE documents the admin scope and the default expiry.
"""

# ruff: noqa: E402 -- the api and agent extras are optional: skip before importing them.

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / "tests" / "mcp", ROOT / "tests" / "planning"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _fake_portfolio import FakePortfolioApi, as_portfolio_api, tiny_profile
from _mcp_helpers import EXAMPLES, Services, build_services, error_payload, run_session

from twinflow.cli import admin
from twinflow.mcp.guard import ToolGuard
from twinflow.mcp.tools import CATALOGUE, build_server
from twinflow.service.app import create_app
from twinflow.service.audit import ToolAuditor
from twinflow.service.auth import (
    SCOPES,
    Caller,
    TokenRegistry,
    TokenRegistryError,
    add_token,
    required_scope,
)
from twinflow.service.settings import ServiceSettings
from twinflow.service.signing import generate_signing_key

ADMIN_TOOLS = frozenset({"put_planning_profile", "put_k_table"})
READ_PROFILE_TOOLS = frozenset({"get_planning_profile", "get_k_table"})
PROFILE_BODY: dict[str, Any] = {"profile": tiny_profile().model_dump(mode="json")}
TOLERANCE = timedelta(seconds=10)
GUIDE = ROOT / "docs" / "enterprise" / "AGENT-GUIDE.md"


# --- the scope vocabulary ----------------------------------------------------------------------


def test_scopes_are_read_plan_and_admin() -> None:
    assert frozenset({"read", "plan", "admin"}) == SCOPES


def test_registry_entry_may_list_admin_scope(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    token = add_token(path, caller_id="ops", scopes=["read", "admin"], days=None)

    caller = TokenRegistry.load(path).authenticate(token)

    assert caller is not None
    assert caller.scopes == frozenset({"read", "admin"})


def test_registry_still_refuses_unknown_scope(tmp_path: Path) -> None:
    with pytest.raises(TokenRegistryError):
        add_token(tmp_path / "tokens.json", caller_id="ops", scopes=["read", "root"], days=None)


def test_required_scope_for_classifications_is_unchanged() -> None:
    assert required_scope("read_only") == "read"
    assert required_scope("state_modifying") == "plan"


# --- the catalogue -----------------------------------------------------------------------------


def test_admin_tools_need_admin_scope_and_stay_state_modifying() -> None:
    specs = {spec.name: spec for spec in CATALOGUE}
    for name in ADMIN_TOOLS:
        assert specs[name].scope == "admin", name
        assert specs[name].classification == "state_modifying", name


def test_every_other_tool_scope_follows_its_classification() -> None:
    expected = {"read_only": "read", "state_modifying": "plan"}
    for spec in CATALOGUE:
        if spec.name not in ADMIN_TOOLS:
            assert spec.scope == expected[spec.classification], spec.name


def test_profile_read_tools_stay_read_scope() -> None:
    specs = {spec.name: spec for spec in CATALOGUE}
    for name in READ_PROFILE_TOOLS:
        assert specs[name].scope == "read", name


def test_tools_list_meta_publishes_the_admin_scope(tmp_path: Path) -> None:
    with build_services(tmp_path) as services:

        async def body(session: Any) -> dict[str, Any]:
            return {n: dict(t.meta or {}) for n, t in session.tools.items()}

        metas = run_session(build_server(services.tools), body)

    for name in ADMIN_TOOLS:
        assert metas[name]["twinflow/scope"] == "admin", name
        assert metas[name]["twinflow/classification"] == "state_modifying", name
    assert metas["get_planning_profile"]["twinflow/scope"] == "read"
    assert metas["solve_portfolio"]["twinflow/scope"] == "plan"


# --- the guard over MCP ------------------------------------------------------------------------


def _server_for(services: Services, scopes: set[str]) -> Any:
    guard = ToolGuard(
        auditor=ToolAuditor(services.workspace.repository),
        limiter=None,
        local_caller=Caller(caller_id="scoped", scopes=frozenset(scopes)),
    )
    return build_server(services.tools, guard)


def _audit_outcomes(root: Path) -> list[tuple[str, str]]:
    uri = f"file:{root / 'workspace' / 'workspace.sqlite3'}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute(
            "SELECT tool, outcome FROM audit_events "
            "WHERE operation = 'tool_call' AND caller_id = 'scoped' ORDER BY sequence"
        ).fetchall()
    return [(str(t), str(o)) for t, o in rows]


def _mcp_call(tmp_path: Path, scopes: set[str], tool: str, args: dict[str, Any]) -> Any:
    with build_services(tmp_path) as services:

        async def body(session: Any) -> Any:
            return await session.raw(tool, args)

        result = run_session(_server_for(services, scopes), body)
        return result, _audit_outcomes(tmp_path)


@pytest.mark.parametrize(
    ("tool", "args"),
    [("put_planning_profile", PROFILE_BODY), ("put_k_table", {"table": {}})],
)
def test_read_plan_caller_is_forbidden_on_admin_tool_and_audited_denied(
    tmp_path: Path, tool: str, args: dict[str, Any]
) -> None:
    result, audit = _mcp_call(tmp_path, {"read", "plan"}, tool, args)

    assert result.is_error
    assert error_payload(result)["code"] == "forbidden"
    assert audit == [(tool, "denied")]


def test_admin_caller_may_put_planning_profile(tmp_path: Path) -> None:
    result, audit = _mcp_call(
        tmp_path, {"read", "plan", "admin"}, "put_planning_profile", PROFILE_BODY
    )

    assert not result.is_error, error_payload(result)
    assert audit == [("put_planning_profile", "ok")]


def test_admin_caller_is_not_forbidden_on_put_k_table(tmp_path: Path) -> None:
    result, audit = _mcp_call(tmp_path, {"read", "plan", "admin"}, "put_k_table", {"table": {}})

    # The table body may be refused (TF-003 validation), but never for lack of scope.
    assert audit and audit[0][1] != "denied"
    if result.is_error:
        assert error_payload(result)["code"] != "forbidden"


def test_admin_only_token_cannot_call_a_plan_tool(tmp_path: Path) -> None:
    result, audit = _mcp_call(tmp_path, {"admin"}, "solve_portfolio", {"problem_id": "x"})

    assert error_payload(result)["code"] == "forbidden"
    assert audit == [("solve_portfolio", "denied")]


def test_admin_only_token_cannot_call_a_read_tool(tmp_path: Path) -> None:
    result, audit = _mcp_call(tmp_path, {"admin"}, "get_planning_profile", {})

    assert error_payload(result)["code"] == "forbidden"
    assert audit == [("get_planning_profile", "denied")]


def test_read_plan_caller_may_still_read_the_profile(tmp_path: Path) -> None:
    _result, audit = _mcp_call(tmp_path, {"read", "plan"}, "get_planning_profile", {})

    assert audit
    assert audit[0][1] != "denied"


# --- REST --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rest(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, dict[str, str]]]:
    base = tmp_path_factory.mktemp("admin-scope")
    secrets = base / "secrets"
    secrets.mkdir(mode=0o700)
    tokens_file = secrets / "tokens.json"
    tokens = {
        "planner": add_token(tokens_file, caller_id="p", scopes=["read", "plan"], days=None),
        "admin": add_token(tokens_file, caller_id="a", scopes=["read", "plan", "admin"], days=None),
    }
    key_file = secrets / "signing-key.pem"
    generate_signing_key(key_file)
    settings = ServiceSettings(
        tokens_file=tokens_file,
        signing_key_file=key_file,
        calls_per_minute=10_000,
        max_solve_seconds=30.0,
    )
    app = create_app(
        EXAMPLES,
        workspace_root=base / "workspace",
        settings=settings,
        portfolio_api=as_portfolio_api(FakePortfolioApi()),
    )
    with TestClient(app, base_url="http://testserver", follow_redirects=False) as client:
        yield client, tokens


def _put(rest: tuple[TestClient, dict[str, str]], who: str, path: str, body: dict[str, Any]) -> Any:
    client, tokens = rest
    return client.put(
        "/api/workspace" + path, json=body, headers={"Authorization": f"Bearer {tokens[who]}"}
    )


@pytest.mark.parametrize(
    ("path", "body"),
    [("/planning-profile", PROFILE_BODY), ("/k-table", {"table": {}})],
)
def test_rest_put_with_read_plan_token_is_403_forbidden(
    rest: tuple[TestClient, dict[str, str]], path: str, body: dict[str, Any]
) -> None:
    response = _put(rest, "planner", path, body)

    assert response.status_code == 403, response.text[:300]
    assert response.json()["code"] == "forbidden"


def test_rest_put_planning_profile_with_admin_token_succeeds(
    rest: tuple[TestClient, dict[str, str]],
) -> None:
    response = _put(rest, "admin", "/planning-profile", PROFILE_BODY)

    assert response.status_code == 200, response.text[:300]


# --- token expiry defaults ---------------------------------------------------------------------


def _mint(tmp_path: Path, *flags: str) -> dict[str, Any]:
    tokens_file = tmp_path / "tokens.json"
    code = admin.main(
        ["token", "new", "--caller", "arc", "--scopes", "read,plan", "--tokens-file"]
        + [str(tokens_file), *flags]
    )
    assert code == 0
    entries = json.loads(tokens_file.read_text())["tokens"]
    assert len(entries) == 1
    entry: dict[str, Any] = entries[0]
    return entry


def _not_after_close_to(entry: dict[str, Any], days: int) -> None:
    not_after = datetime.fromisoformat(entry["not_after"])
    assert abs(not_after - (datetime.now(UTC) + timedelta(days=days))) < TOLERANCE


def test_token_new_without_expiry_flag_defaults_to_90_days(tmp_path: Path) -> None:
    _not_after_close_to(_mint(tmp_path), 90)


def test_token_new_days_sets_that_many_days(tmp_path: Path) -> None:
    _not_after_close_to(_mint(tmp_path, "--days", "7"), 7)


def test_token_new_no_expiry_stores_null(tmp_path: Path) -> None:
    assert _mint(tmp_path, "--no-expiry")["not_after"] is None


def test_token_new_days_with_no_expiry_is_refused_with_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tokens_file = tmp_path / "tokens.json"
    argv = ["token", "new", "--caller", "arc", "--scopes", "read", "--days", "5", "--no-expiry"]
    argv += ["--tokens-file", str(tokens_file)]

    try:
        code = admin.main(argv)
    except SystemExit as exit_:
        code = exit_.code

    assert code == 2
    assert not tokens_file.exists()
    # Refused as a conflict between two known flags, not as an unknown flag.
    assert "unrecognized" not in capsys.readouterr().err


def test_token_new_accepts_admin_scope(tmp_path: Path) -> None:
    tokens_file = tmp_path / "tokens.json"

    code = admin.main(
        ["token", "new", "--caller", "ops", "--scopes", "read,plan,admin"]
        + ["--tokens-file", str(tokens_file)]
    )

    assert code == 0
    scopes = json.loads(tokens_file.read_text())["tokens"][0]["scopes"]
    assert scopes == ["admin", "plan", "read"]


# --- AGENT-GUIDE -------------------------------------------------------------------------------


def test_agent_guide_documents_admin_scope_and_default_expiry() -> None:
    text = GUIDE.read_text(encoding="utf-8")

    assert "`admin` scope" in text
    assert "put_planning_profile" in text
    assert "--no-expiry" in text
    assert "90" in text
