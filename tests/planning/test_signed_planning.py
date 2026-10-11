"""Planning uses only signed artifacts; profile tools; published signing key (T-221).

Components: COMP-127 (signed artifacts in planning), COMP-117 (`PlanningService`), COMP-118
(catalogue). Requirements: REQ-184 (verify before every use, fail closed with
`artifact_signature_invalid`), REQ-185 (profile tools; planning takes a profile only by
reference), REQ-186 (no private key material in any output, log or audit row;
`describe_capabilities` publishes the public key and key id).

Contract pinned here (orchestrator design for T-221):

* `put_planning_profile` (TOML or typed) validates, signs, activates and writes exactly one
  audit row (`tool = put_planning_profile`, `outcome = ok`); `get_planning_profile`
  returns `digest`, `sequence` and `key_id`. The artifact digest equals
  `twinflow.erp.profile.profile_digest` of the same profile.
* `build_problem_from_erp`: `profile_digest` None uses the active signed profile; a stored
  digest uses that version; an unknown digest is `not_found`. Its input schema has no
  `profile` property.
* Problem and plan records (sqlite `records`, kinds `problems` and `plans`) carry
  `profile_digest`, `profile_sequence`, `k_table_digest`, `k_table_sequence` (the K-table
  pair None when no K-table was ever put) and `profile_signed` (problem records).
* Tampering the stored profile record between build and solve makes `solve_portfolio` fail
  with `artifact_signature_invalid` before TF-003 is called. Restoring an older, validly
  signed active pointer (rollback) makes the next build fail the same way.
* `describe_capabilities` returns `signing_public_key` (lowercase hex of the 32-byte raw
  Ed25519 public key) and `signing_key_id` (the signer's key id); both None without a
  signer. Its `max_solve_seconds` is the planning service's configured cap, and the
  timeout of every solve-capped tool (`ToolSpec.timeout_cap == "solve"`) is that cap + 15 s.
* Without an artifact store: `ToolServices(local_mode=True)` builds with the built-in
  default profile (`load_planning_profile("")`) and records `profile_signed: false`;
  `local_mode=False` refuses the build with `no_planning_profile`.
* `PlanningService.max_solve_seconds` and `PlanningService.portfolio_available` are public,
  read-only properties.
* Canary: no tool output, audit row, log record or byte of the workspace database contains
  the private key's PEM text or raw private bytes (hex, base64 or binary).
"""

# ruff: noqa: E402 -- the agent extra is optional: skip before importing anything needing `mcp`.

from __future__ import annotations

import pytest

pytest.importorskip("mcp")
pytest.importorskip("cryptography")

import base64
import hashlib
import json
import logging
import re
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _fake_portfolio import FakePortfolioApi, as_portfolio_api, tiny_profile, tiny_shop
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)
from pydantic import BaseModel, ConfigDict

from mcp import types
from twinflow.application.artifacts import ArtifactSignatureInvalid
from twinflow.application.decision_tools import DecisionTools
from twinflow.application.planning import PlanningService
from twinflow.application.planning_artifacts import PlanningArtifacts
from twinflow.application.workspace import Workspace
from twinflow.erp.profile import load_planning_profile, profile_digest
from twinflow.mcp.tools import CATALOGUE, ToolServices, build_server

ROOT = Path(__file__).resolve().parents[2]
_MCP_TESTS = str(ROOT / "tests" / "mcp")
if _MCP_TESTS not in sys.path:
    sys.path.insert(0, _MCP_TESTS)

from _mcp_helpers import (
    EXAMPLES,
    McpSession,
    error_payload,
    run_session,
    signed_artifact_store,
)

MAX_SOLVE_SECONDS = 30.0
GRACE_SECONDS = 15.0
KEY_FILE = "service-signing-key.pem"  # where `signed_artifact_store` writes the key
PROFILE_TOML = "time_budget_seconds = 10.0\n\n[erp]\nrush_class = 2\n\n[erp.class_weights]\nA = 4\n"
UNKNOWN_DIGEST = "0" * 64


# --- the world under test ---------------------------------------------------------------------


@dataclass
class World:
    """In-process services over one workspace, with the TF-003 fake's spy and the key file."""

    workspace: Workspace
    planning: PlanningService
    fake: FakePortfolioApi
    services: ToolServices
    base: Path
    outputs: list[str] = field(default_factory=list)

    @property
    def database(self) -> Path:
        return self.base / "workspace" / "workspace.sqlite3"

    @property
    def key_path(self) -> Path:
        return self.base / "keys" / KEY_FILE


@contextmanager
def _world(base: Path, *, signed: bool = True, **service_flags: Any) -> Iterator[World]:
    """Real services; `signed=False` has no artifact store; flags go to `ToolServices`."""
    workspace = Workspace(base / "workspace", EXAMPLES)
    fake = FakePortfolioApi()
    planning = PlanningService(
        workspace, portfolio=as_portfolio_api(fake), max_solve_seconds=MAX_SOLVE_SECONDS
    )
    try:
        artifacts = signed_artifact_store(workspace, base / "keys") if signed else None
        services = ToolServices(
            workspace=workspace,
            decisions=DecisionTools(workspace),
            planning=planning,
            artifacts=artifacts,
            models_root=None,
            **service_flags,
        )
        yield World(workspace, planning, fake, services, base)
    finally:
        planning.close()
        workspace.close()


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    with _world(tmp_path) as opened:
        yield opened


def _session(world: World, body: Any) -> Any:
    """Run `body(session)` against the catalogue; record every result's text for the canary."""

    async def recorded(session: McpSession) -> Any:
        original = session.raw

        async def raw(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            result = await original(name, arguments)
            world.outputs.append(json.dumps(result.model_dump(mode="json"), default=str))
            return result

        session.raw = raw  # type: ignore[method-assign]  # test-local recording wrapper
        return await body(session)

    return run_session(build_server(world.services), recorded)


async def _error_code(session: McpSession, name: str, arguments: dict[str, Any]) -> str:
    result = await session.raw(name, arguments)
    assert result.is_error, f"{name} unexpectedly succeeded: {result.structured_content}"
    return str(error_payload(result)["code"])


def _shop() -> dict[str, Any]:
    return tiny_shop().model_dump(mode="json")


def _db(world: World) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{world.database}?mode=ro", uri=True)


def _record(world: World, kind: str, record_id: str) -> dict[str, Any]:
    with closing(_db(world)) as db:
        row = db.execute(
            "SELECT body FROM records WHERE kind = ? AND id = ?", (kind, record_id)
        ).fetchone()
    assert row is not None, f"no {kind} record {record_id}"
    body: dict[str, Any] = json.loads(row[0])
    return body


def _audit(world: World) -> list[tuple[str, str, str]]:
    with closing(_db(world)) as db:
        rows = db.execute(
            "SELECT caller_id, tool, outcome FROM audit_events "
            "WHERE operation = 'tool_call' ORDER BY sequence"
        ).fetchall()
    return [(str(c), str(t), str(o)) for c, t, o in rows]


def _write_body(world: World, kind: str, record_id: str, body: dict[str, Any]) -> None:
    """Edit a stored row directly, the way an attacker with file access would."""
    with closing(sqlite3.connect(world.database)) as db:
        changed = db.execute(
            "UPDATE records SET body = ? WHERE kind = ? AND id = ?",
            (json.dumps(body), kind, record_id),
        ).rowcount
        db.commit()
    assert changed == 1, f"tamper target {kind}/{record_id} not found"


def _other_profile() -> dict[str, Any]:
    return tiny_profile(time_budget_seconds=20.0).model_dump(mode="json")


# --- profile tools (REQ-185) -----------------------------------------------------------------


def test_put_planning_profile_toml_signs_activates_and_audits_once(world: World) -> None:
    async def body(session: McpSession) -> tuple[dict[str, Any], dict[str, Any]]:
        ref = await session.call("put_planning_profile", {"profile_toml": PROFILE_TOML})
        active = await session.call("get_planning_profile", {})
        return ref, active

    ref, active = _session(world, body)

    assert ref["digest"] == profile_digest(tiny_profile())
    assert (active["digest"], active["sequence"], active["key_id"]) == (
        ref["digest"],
        1,
        _key_id(world),
    )
    assert active["artifact"] == tiny_profile().model_dump(mode="json")
    puts = [row for row in _audit(world) if row[1] == "put_planning_profile"]
    assert puts == [("local", "put_planning_profile", "ok")]


def test_build_problem_input_schema_has_no_inline_profile(world: World) -> None:
    async def body(session: McpSession) -> dict[str, Any]:
        return dict(session.tools["build_problem_from_erp"].input_schema)

    schema = _session(world, body)

    assert "profile" not in schema["properties"]
    assert "profile_digest" in schema["properties"]


def test_build_uses_the_active_profile_or_the_named_signed_version(world: World) -> None:
    async def body(session: McpSession) -> dict[str, Any]:
        first = await session.call("put_planning_profile", {"profile_toml": PROFILE_TOML})
        second = await session.call("put_planning_profile", {"profile": _other_profile()})
        active_build = await session.call("build_problem_from_erp", {"snapshot": _shop()})
        named_build = await session.call(
            "build_problem_from_erp", {"snapshot": _shop(), "profile_digest": first["digest"]}
        )
        return {"first": first, "second": second, "active": active_build, "named": named_build}

    out = _session(world, body)

    assert out["active"]["profile_digest"] == out["second"]["digest"]
    assert out["named"]["profile_digest"] == out["first"]["digest"]
    active_problem = _record(world, "problems", out["active"]["problem_id"])
    named_problem = _record(world, "problems", out["named"]["problem_id"])
    assert (active_problem["profile_digest"], active_problem["profile_sequence"]) == (
        out["second"]["digest"],
        2,
    )
    assert (named_problem["profile_digest"], named_problem["profile_sequence"]) == (
        out["first"]["digest"],
        1,
    )


def test_build_with_an_unknown_profile_digest_is_not_found(world: World) -> None:
    async def body(session: McpSession) -> str:
        await session.call("put_planning_profile", {"profile_toml": PROFILE_TOML})
        return await _error_code(
            session,
            "build_problem_from_erp",
            {"snapshot": _shop(), "profile_digest": UNKNOWN_DIGEST},
        )

    assert _session(world, body) == "not_found"


# --- provenance on problem and plan records (REQ-184) ------------------------------------------


def test_problem_and_plan_records_carry_signed_profile_and_k_table_provenance(
    world: World,
) -> None:
    async def body(session: McpSession) -> dict[str, Any]:
        ref = await session.call("put_planning_profile", {"profile_toml": PROFILE_TOML})
        built = await session.call("build_problem_from_erp", {"snapshot": _shop()})
        portfolio = await session.call(
            "solve_portfolio", {"problem_id": built["problem_id"], "request_key": "prov-1"}
        )
        return {"ref": ref, "built": built, "portfolio": portfolio}

    out = _session(world, body)
    expected = {
        "profile_digest": out["ref"]["digest"],
        "profile_sequence": 1,
        "k_table_digest": None,
        "k_table_sequence": None,
    }

    problem = _record(world, "problems", out["built"]["problem_id"])
    assert {key: problem.get(key, "<missing>") for key in expected} == expected
    assert problem.get("profile_signed") is True
    for plan_id in out["portfolio"]["plan_ids"]:
        plan = _record(world, "plans", plan_id)
        assert {key: plan.get(key, "<missing>") for key in expected} == expected, plan_id


# --- fail closed (REQ-184) -------------------------------------------------------------------


def test_tampered_profile_between_build_and_solve_fails_solve_without_calling_tf003(
    world: World,
) -> None:
    async def build(session: McpSession) -> str:
        await session.call("put_planning_profile", {"profile_toml": PROFILE_TOML})
        built = await session.call("build_problem_from_erp", {"snapshot": _shop()})
        return str(built["problem_id"])

    problem_id = _session(world, build)
    stored = _record(world, "planning_profiles", "00000001")
    tampered_payload = stored["payload"].replace('"rush_class":2', '"rush_class":4')
    assert tampered_payload != stored["payload"], stored["payload"]
    _write_body(world, "planning_profiles", "00000001", {**stored, "payload": tampered_payload})

    async def solve(session: McpSession) -> str:
        return await _error_code(
            session, "solve_portfolio", {"problem_id": problem_id, "request_key": "after-tamper"}
        )

    assert _session(world, solve) == "artifact_signature_invalid"
    assert sum(world.fake.calls.values()) == 0


@pytest.mark.parametrize("field", ["request", "profile"])
def test_problem_record_profile_edited_between_build_and_solve_fails_closed(
    world: World, field: str
) -> None:
    """The solve runs `request.profile`; both copies must stay bound to the signed profile."""

    async def build(session: McpSession) -> str:
        await session.call("put_planning_profile", {"profile_toml": PROFILE_TOML})
        built = await session.call("build_problem_from_erp", {"snapshot": _shop()})
        return str(built["problem_id"])

    problem_id = _session(world, build)
    stored = _record(world, "problems", problem_id)
    if field == "request":
        profile = stored["request"]["profile"]
        stored["request"]["profile"] = {**profile, "time_budget_seconds": 299.0}
        assert stored["request"]["profile"] != profile
    else:
        scheduling = stored["profile"]["scheduling"]
        stored["profile"]["scheduling"] = {**scheduling, "time_budget_seconds": 299.0}
        assert stored["profile"]["scheduling"] != scheduling
    _write_body(world, "problems", problem_id, stored)

    async def solve(session: McpSession) -> str:
        return await _error_code(
            session, "solve_portfolio", {"problem_id": problem_id, "request_key": "edited"}
        )

    assert _session(world, solve) == "artifact_signature_invalid"
    assert sum(world.fake.calls.values()) == 0


def test_rolled_back_active_pointer_fails_the_next_build(world: World) -> None:
    async def first_put(session: McpSession) -> None:
        await session.call("put_planning_profile", {"profile_toml": PROFILE_TOML})

    _session(world, first_put)
    old_pointer = _record(world, "artifact_pointers", "planning_profiles")

    async def second_put(session: McpSession) -> None:
        await session.call("put_planning_profile", {"profile": _other_profile()})

    _session(world, second_put)
    _write_body(world, "artifact_pointers", "planning_profiles", old_pointer)

    async def build(session: McpSession) -> str:
        return await _error_code(session, "build_problem_from_erp", {"snapshot": _shop()})

    assert _session(world, build) == "artifact_signature_invalid"


# --- unsigned profiles: loopback default only ---------------------------------------------------


def test_without_a_store_local_mode_builds_with_the_default_profile_marked_unsigned(
    tmp_path: Path,
) -> None:
    with _world(tmp_path, signed=False, local_mode=True) as world:

        async def body(session: McpSession) -> dict[str, Any]:
            return await session.call("build_problem_from_erp", {"snapshot": _shop()})

        built = _session(world, body)

        assert built["status"] == "ready"
        assert built["profile_digest"] == profile_digest(load_planning_profile(""))
        problem = _record(world, "problems", built["problem_id"])
        assert problem["profile_signed"] is False


def test_without_a_store_a_non_local_service_refuses_the_build(tmp_path: Path) -> None:
    with _world(tmp_path, signed=False, local_mode=False) as world:

        async def body(session: McpSession) -> str:
            return await _error_code(session, "build_problem_from_erp", {"snapshot": _shop()})

        assert _session(world, body) == "no_planning_profile"
        assert sum(world.fake.calls.values()) == 0


# --- published key, budgets and service properties (REQ-186, REQ-169, REQ-187) -------------


def _private_key(world: World) -> Ed25519PrivateKey:
    key = load_pem_private_key(world.key_path.read_bytes(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def _public_raw(world: World) -> bytes:
    return _private_key(world).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _key_id(world: World) -> str:
    return hashlib.sha256(_public_raw(world)).hexdigest()[:16]


def test_describe_capabilities_publishes_the_public_key_hex_and_key_id(world: World) -> None:
    async def body(session: McpSession) -> dict[str, Any]:
        return await session.call("describe_capabilities", {})

    capabilities = _session(world, body)

    assert capabilities["signing_public_key"] == _public_raw(world).hex()
    assert capabilities["signing_key_id"] == _key_id(world)


def test_describe_capabilities_without_a_signer_publishes_no_key(tmp_path: Path) -> None:
    with _world(tmp_path, signed=False, local_mode=True) as world:

        async def body(session: McpSession) -> dict[str, Any]:
            return await session.call("describe_capabilities", {})

        capabilities = _session(world, body)

    assert capabilities["signing_public_key"] is None
    assert capabilities["signing_key_id"] is None


def test_describe_capabilities_budgets_follow_the_configured_solve_cap(world: World) -> None:
    async def body(session: McpSession) -> dict[str, Any]:
        return await session.call("describe_capabilities", {})

    capabilities = _session(world, body)
    # Tools whose timeout follows the solve cap; the simulate tools also hold a solve slot
    # but follow max_simulate_seconds (TF-004 D-105), so they are selected out by cap.
    solve_tools = {spec.name for spec in CATALOGUE if spec.timeout_cap == "solve"}

    assert solve_tools == {"solve_portfolio", "resolve_with_overrides", "insert_rush_order"}
    assert capabilities["max_solve_seconds"] == MAX_SOLVE_SECONDS
    assert {name: capabilities["tool_timeouts"][name] for name in solve_tools} == dict.fromkeys(
        solve_tools, MAX_SOLVE_SECONDS + GRACE_SECONDS
    )


def test_planning_service_exposes_read_only_budget_and_portfolio_properties(world: World) -> None:
    absent = PlanningService(world.workspace, portfolio=None, max_solve_seconds=12.0)
    try:
        assert world.planning.max_solve_seconds == MAX_SOLVE_SECONDS
        assert world.planning.portfolio_available is True
        assert absent.max_solve_seconds == 12.0
        assert absent.portfolio_available is False
        with pytest.raises(AttributeError):
            world.planning.max_solve_seconds = 1.0  # type: ignore[misc]  # read-only property
        with pytest.raises(AttributeError):
            world.planning.portfolio_available = False  # type: ignore[misc]  # read-only property
    finally:
        absent.close()


# --- canary: no private key material anywhere (REQ-186) ----------------------------------------


def _needles(world: World) -> tuple[list[str], bytes]:
    """Text forms of the private key (PEM body lines, hex, base64) and its raw bytes."""
    pem = world.key_path.read_text(encoding="ascii")
    raw = _private_key(world).private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    body_lines = [line for line in pem.splitlines() if line and not line.startswith("-----")]
    texts = [
        "PRIVATE KEY",
        *body_lines,
        raw.hex(),
        raw.hex().upper(),
        base64.b64encode(raw).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
    ]
    return texts, raw


def test_no_private_key_material_in_outputs_audit_rows_logs_or_database(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def body(session: McpSession) -> None:
        await session.call("describe_capabilities", {})
        await session.call("put_planning_profile", {"profile_toml": PROFILE_TOML})
        await session.call("get_planning_profile", {})
        await session.call("validate_snapshot", {"snapshot": _shop()})
        built = await session.call("build_problem_from_erp", {"snapshot": _shop()})
        portfolio = await session.call(
            "solve_portfolio", {"problem_id": built["problem_id"], "request_key": "canary"}
        )
        await session.call("get_plan", {"plan_id": portfolio["plan_ids"][0], "limit": 5})
        await session.call("get_pareto_menu", {"portfolio_id": portfolio["portfolio_id"]})
        await session.raw("put_planning_profile", {"profile_toml": "not = [valid"})
        await session.raw("build_problem_from_erp", {"snapshot": {}})

    _session(world, body)
    texts, raw = _needles(world)

    with closing(_db(world)) as db:
        audit_text = json.dumps(db.execute("SELECT * FROM audit_events").fetchall(), default=str)
    log_text = "\n".join(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)
    haystacks = {
        "tool outputs": "\n".join(world.outputs),
        "audit rows": audit_text,
        "log records": log_text,
    }
    assert world.outputs, "the canary saw no tool output"
    for where, haystack in haystacks.items():
        leaked = [needle for needle in texts if needle in haystack]
        assert leaked == [], f"private key material in {where}"
    database_bytes = b"".join(
        path.read_bytes() for path in world.database.parent.glob("workspace.sqlite3*")
    )
    assert raw not in database_bytes
    assert not any(needle.encode("ascii") in database_bytes for needle in texts)


# --- TF-004: the `[simulation]` table is signed with the profile (T-185, REQ-139, D-104) -----

SIMULATION_TOML = (
    PROFILE_TOML
    + '\n[simulation]\ndistribution_source = "profile"\nbaseline_rule = "edd"\n\n'
    + "[[simulation.run_factors]]\n"
    + 'distribution = { kind = "lognormal", mean = 1.0, cv = 0.15 }\n\n'
    + "[[simulation.setup_factors]]\n"
    + 'distribution = { kind = "lognormal", mean = 1.1, cv = 0.3 }\n'
)


class _PreSimulationProfile(BaseModel):
    """A profile body exactly as TF-005 stored it: `scheduling` and `erp`, no `simulation`.

    Put through the real `SignedArtifactStore`, so the record is validly signed and its
    digest is the SHA-256 of the pre-TF-004 canonical payload.
    """

    model_config = ConfigDict(extra="allow")


def _pre_simulation_body() -> dict[str, Any]:
    body = tiny_profile().model_dump(mode="json")
    body.pop("simulation", None)
    assert set(body) == {"scheduling", "erp"}
    return body


def _put_pre_simulation_profile(world: World) -> str:
    """Store a TF-005-era signed profile; return its signed digest."""
    assert world.services.artifacts is not None
    ref = world.services.artifacts.put(
        "planning_profiles",
        _PreSimulationProfile.model_validate(_pre_simulation_body()),
        caller_id="local",
    )
    canonical = json.dumps(_pre_simulation_body(), sort_keys=True, separators=(",", ":"))
    assert ref.digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return str(ref.digest)


def _record_count(world: World, kind: str) -> int:
    with closing(_db(world)) as db:
        row = db.execute("SELECT COUNT(*) FROM records WHERE kind = ?", (kind,)).fetchone()
    return int(row[0])


def test_put_planning_profile_signs_the_simulation_table_and_get_returns_it(
    world: World,
) -> None:
    expected = load_planning_profile(SIMULATION_TOML)

    async def body(session: McpSession) -> tuple[dict[str, Any], dict[str, Any]]:
        ref = await session.call("put_planning_profile", {"profile_toml": SIMULATION_TOML})
        active = await session.call("get_planning_profile", {})
        return ref, active

    ref, active = _session(world, body)

    assert ref["digest"] == profile_digest(expected)
    assert active["digest"] == ref["digest"]
    assert active["artifact"]["simulation"] == expected.simulation.model_dump(mode="json")
    assert active["artifact"]["simulation"]["distribution_source"] == "profile"
    stored = json.loads(_record(world, "planning_profiles", "00000001")["payload"])
    assert stored["simulation"] == expected.simulation.model_dump(mode="json")


def test_put_planning_profile_typed_profile_round_trips_the_simulation_table(
    world: World,
) -> None:
    profile = load_planning_profile(SIMULATION_TOML)

    async def body(session: McpSession) -> dict[str, Any]:
        await session.call("put_planning_profile", {"profile": profile.model_dump(mode="json")})
        return await session.call("get_planning_profile", {})

    active = _session(world, body)

    assert active["digest"] == profile_digest(profile)
    assert active["artifact"] == profile.model_dump(mode="json")


def test_profiles_differing_only_in_simulation_are_distinct_signed_versions(
    world: World,
) -> None:
    async def body(session: McpSession) -> dict[str, Any]:
        plain = await session.call("put_planning_profile", {"profile_toml": PROFILE_TOML})
        calibrated = await session.call("put_planning_profile", {"profile_toml": SIMULATION_TOML})
        named = await session.call(
            "build_problem_from_erp", {"snapshot": _shop(), "profile_digest": plain["digest"]}
        )
        return {"plain": plain, "calibrated": calibrated, "named": named}

    out = _session(world, body)

    assert out["plain"]["digest"] != out["calibrated"]["digest"]
    assert (out["plain"]["sequence"], out["calibrated"]["sequence"]) == (1, 2)
    assert out["named"]["profile_digest"] == out["plain"]["digest"]


def test_put_planning_profile_invalid_simulation_table_is_refused_and_stores_nothing(
    world: World,
) -> None:
    bad = PROFILE_TOML + "\n[simulation]\nreplications = 101\n"

    async def body(session: McpSession) -> dict[str, Any]:
        result = await session.raw("put_planning_profile", {"profile_toml": bad})
        assert result.is_error, "an over-cap [simulation] table was signed"
        return error_payload(result)

    payload = _session(world, body)

    assert payload["code"] == "invalid_input"
    # The cross-field cap check reports at the table, not as an unknown table.
    assert re.search(r"simulation(\.[a-z_]+)?: value_error", str(payload["message"])), payload
    assert _record_count(world, "planning_profiles") == 0


def test_pre_simulation_signed_profile_fails_get_planning_profile_closed(world: World) -> None:
    _put_pre_simulation_profile(world)

    async def body(session: McpSession) -> str:
        return await _error_code(session, "get_planning_profile", {})

    assert _session(world, body) == "artifact_signature_invalid"


def test_pre_simulation_signed_profile_fails_the_build_instead_of_loading_defaults(
    world: World,
) -> None:
    old_digest = _put_pre_simulation_profile(world)

    async def body(session: McpSession) -> tuple[str, str]:
        active = await _error_code(session, "build_problem_from_erp", {"snapshot": _shop()})
        named = await _error_code(
            session, "build_problem_from_erp", {"snapshot": _shop(), "profile_digest": old_digest}
        )
        return active, named

    assert _session(world, body) == ("artifact_signature_invalid", "artifact_signature_invalid")
    assert _record_count(world, "problems") == 0
    assert sum(world.fake.calls.values()) == 0


def test_pre_simulation_signed_profile_is_refused_by_planning_artifacts_with_a_clear_message(
    world: World,
) -> None:
    old_digest = _put_pre_simulation_profile(world)
    # Local mode must not rescue it with the built-in default either (D-126: no fallback).
    artifacts = PlanningArtifacts(world.services.artifacts, local_mode=True)

    for digest in (None, old_digest):
        with pytest.raises(ArtifactSignatureInvalid) as caught:
            artifacts.profile(digest)
        assert caught.value.code == "artifact_signature_invalid"
        assert "digest" in str(caught.value), str(caught.value)


def test_problem_built_under_a_pre_simulation_profile_fails_its_solve_closed(
    world: World,
) -> None:
    """A TF-005-era problem names the old signed digest; its re-verification must fail."""
    old_digest = _put_pre_simulation_profile(world)

    async def build(session: McpSession) -> str:
        await session.call("put_planning_profile", {"profile_toml": PROFILE_TOML})
        built = await session.call("build_problem_from_erp", {"snapshot": _shop()})
        return str(built["problem_id"])

    problem_id = _session(world, build)
    # Rewrite the problem as TF-005 stored it: built under sequence 1, the pre-TF-004 body.
    stored = _record(world, "problems", problem_id)
    stored.update(profile=_pre_simulation_body(), profile_digest=old_digest, profile_sequence=1)
    _write_body(world, "problems", problem_id, stored)

    async def solve(session: McpSession) -> str:
        return await _error_code(
            session, "solve_portfolio", {"problem_id": problem_id, "request_key": "pre-sim"}
        )

    assert _session(world, solve) == "artifact_signature_invalid"
    assert sum(world.fake.calls.values()) == 0
