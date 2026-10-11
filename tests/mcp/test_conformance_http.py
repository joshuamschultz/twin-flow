"""MCP conformance over streamable HTTP with the official SDK client (T-213, COMP-124).

Requirements: REQ-163 (the catalogue), REQ-164 (input and output schemas; structured content
validates), REQ-167 (idempotent solves), REQ-168 (size limit, pagination), REQ-179 (the
official client over streamable HTTP drives a full build-solve-explain-compare flow),
REQ-185 (the planning profile is put through the tool, signed and activated), REQ-177
(planning results are provisional evidence).

The real `create_app` runs under uvicorn on a loopback ephemeral port (`_http_server`) with a
token registry and a service signing key outside the workspace. The client is
`mcp.client.streamable_http.streamable_http_client` + `mcp.ClientSession`, over an HTTP
client that sends `Authorization: Bearer <plan-scope token>`.

TF-003 is selected at runtime: `load_portfolio_api()` when TF-003 is installed, otherwise
the planning tests' `FakePortfolioApi` (real TF-002 solving underneath). The flow runs once
per module in one client session; each test asserts one behaviour of the recorded results.
"""

# ruff: noqa: E402 -- the agent extra is optional: skip before importing anything needing `mcp`.

from __future__ import annotations

import pytest

pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

import asyncio
import datetime as dt
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from mcp import ClientSession, types
from twinflow.application.portfolio_port import PortfolioApi, load_portfolio_api
from twinflow.mcp.tools import CATALOGUE

ROOT = Path(__file__).resolve().parents[2]
for _tests_dir in ("planning", "erp"):  # the TF-003 double and the small_shop generator
    _path = str(ROOT / "tests" / _tests_dir)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _fake_portfolio import FakePortfolioApi, as_portfolio_api
from _http_server import RunningService, running_service
from _mcp_helpers import assert_valid_structured_content, describe, error_payload
from fixtures.generate import small_shop

PLANNER = "planner"
PROFILE_TOML = '[erp.class_weights]\n"A-KEY" = 4\n"B-CORE" = 2\n"C-SPOT" = 1\n'
SOLVE_KEY = "conformance-solve"
PAGE_LIMIT = 7
MAX_MCP_BODY = 16 * 1024 * 1024
CLIENT_TIMEOUT_SECONDS = 60.0
# Every build, solve, plan, comparison and simulation result (REQ-177).
PLANNING_RESULTS = (
    "validate_snapshot",
    "build_problem_from_erp",
    "solve_portfolio",
    "get_pareto_menu",
    "resolve_with_overrides",
    "insert_rush_order",
    "compare_plans",
    "get_plan",
    "simulate_plan",
)


# --- the recorded flow -------------------------------------------------------------------------


@dataclass
class Flow:
    """Everything the single client session saw, by step name."""

    service: RunningService
    fake: FakePortfolioApi | None
    initialize: types.InitializeResult | None = None
    tools: dict[str, types.Tool] = field(default_factory=dict)
    results: dict[str, types.CallToolResult] = field(default_factory=dict)
    pages: list[dict[str, Any]] = field(default_factory=list)
    full_plan: dict[str, Any] = field(default_factory=dict)
    solve_calls_before_replay: int | None = None
    solve_calls_after_replay: int | None = None
    seconds: float = 0.0

    def ok(self, step: str) -> dict[str, Any]:
        """The structured content of a step that must have succeeded."""
        result = self.results[step]
        assert not result.is_error, f"{step} failed: {describe(result)}"
        assert result.structured_content is not None, f"{step}: no structuredContent"
        return result.structured_content


class Recorder:
    """Calls tools through the official client session and records each result."""

    def __init__(self, session: ClientSession, flow: Flow) -> None:
        self.session = session
        self.flow = flow

    async def call(self, step: str, tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result = await self.session.call_tool(tool, arguments)
        assert isinstance(result, types.CallToolResult), (step, type(result))
        self.flow.results[step] = result
        return result

    async def ok(self, step: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.call(step, tool, arguments)
        return self.flow.ok(step)

    def solve_calls(self) -> int | None:
        fake = self.flow.fake
        return None if fake is None else fake.calls["solve_portfolio"]


async def _list_tools(session: ClientSession) -> dict[str, types.Tool]:
    tools: dict[str, types.Tool] = {}
    cursor: str | None = None
    while True:
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await session.list_tools(params=params)
        for tool in page.tools:
            assert tool.name not in tools, f"duplicate tool {tool.name}"
            tools[tool.name] = tool
        cursor = page.next_cursor
        if not cursor:
            return tools


def _rush_order(snapshot: Any) -> dict[str, Any]:
    """A rush order for an item that has a standard routing in the snapshot."""
    routing = snapshot.routings[0]
    due = snapshot.as_of + dt.timedelta(days=14)
    return {
        "order_id": "RUSH-CONF-1",
        "item_id": routing.item_id,
        "quantity": 10,
        "due": due.isoformat(),
    }


def _overrides(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One lock and one prefer_machine override on real operations of two different jobs."""
    scheduled = [row for row in assignments if row["job_id"] is not None]
    locked = scheduled[0]
    preferred = next(row for row in scheduled if row["job_id"] != locked["job_id"])
    return [
        {
            "kind": "lock",
            "id": "ovr-lock",
            "fact_ref": f"plan:{locked['operation_id']}",
            "reason": "planner keeps this operation where it is",
            "hardness": "hard",
            "operation_id": locked["operation_id"],
            "machine_id": locked["machine_id"],
        },
        {
            "kind": "prefer_machine",
            "id": "ovr-prefer",
            "fact_ref": f"plan:{preferred['operation_id']}",
            "reason": "operator prefers this machine",
            "hardness": "hard",
            "operation_id": preferred["operation_id"],
            "machine_id": preferred["machine_id"],
        },
    ]


async def _page_through(rec: Recorder, plan_id: str) -> None:
    offset: int | None = 0
    while offset is not None:
        step = f"get_plan@{offset}"
        page = await rec.ok(
            step, "get_plan", {"plan_id": plan_id, "offset": offset, "limit": PAGE_LIMIT}
        )
        rec.flow.pages.append(page)
        assert len(rec.flow.pages) <= 1000, "get_plan pagination never ended"
        offset = page["next_offset"]
    rec.flow.results["get_plan"] = rec.flow.results[step]


async def _planning_flow(rec: Recorder) -> None:
    snapshot = small_shop()
    snapshot_json = snapshot.model_dump(mode="json")
    await rec.ok("put_planning_profile", "put_planning_profile", {"profile_toml": PROFILE_TOML})
    await rec.ok("get_planning_profile", "get_planning_profile", {})
    await rec.ok("validate_snapshot", "validate_snapshot", {"snapshot": snapshot_json})
    built = await rec.ok(
        "build_problem_from_erp", "build_problem_from_erp", {"snapshot": snapshot_json}
    )
    assert built["status"] == "ready", built
    solve_input = {"problem_id": built["problem_id"], "request_key": SOLVE_KEY}
    portfolio = await rec.ok("solve_portfolio", "solve_portfolio", solve_input)
    rec.flow.solve_calls_before_replay = rec.solve_calls()
    await rec.call("solve_portfolio#replay", "solve_portfolio", solve_input)
    rec.flow.solve_calls_after_replay = rec.solve_calls()
    await rec.call("solve_portfolio#conflict", "solve_portfolio", {**solve_input, "seed": 1})
    portfolio_id = portfolio["portfolio_id"]
    await rec.ok("get_pareto_menu", "get_pareto_menu", {"portfolio_id": portfolio_id})
    plan_id = portfolio["recommended_plan_id"] or portfolio["plan_ids"][0]
    await rec.call("explain_plan", "explain_plan", {"plan_id": plan_id})
    full = await rec.ok(
        "get_plan#full", "get_plan", {"plan_id": plan_id, "offset": 0, "limit": 1000}
    )
    rec.flow.full_plan = full
    resolve_input = {
        "plan_id": plan_id,
        "overrides": _overrides(list(full["assignments"])),
        "request_key": "conformance-resolve",
    }
    await rec.ok("resolve_with_overrides", "resolve_with_overrides", resolve_input)
    rush_input = {
        "plan_id": plan_id,
        "rush": _rush_order(snapshot),
        "request_key": "conformance-rush",
    }
    await rec.ok("insert_rush_order", "insert_rush_order", rush_input)
    await rec.ok("compare_plans", "compare_plans", {"plan_ids": list(portfolio["plan_ids"][:2])})
    await _page_through(rec, plan_id)
    simulate_input = {"plan_id": plan_id, "request_key": "conformance-simulate"}
    await rec.ok("simulate_plan", "simulate_plan", simulate_input)


async def _run(flow: Flow) -> None:
    headers = flow.service.bearer(PLANNER)
    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(flow.service.mcp_url, http_client=http_client) as streams:
            read_stream, write_stream = streams
            async with ClientSession(
                read_stream, write_stream, read_timeout_seconds=CLIENT_TIMEOUT_SECONDS
            ) as session:
                flow.initialize = await session.initialize()
                flow.tools = await _list_tools(session)
                await _planning_flow(Recorder(session, flow))


def _portfolio_api() -> tuple[PortfolioApi, FakePortfolioApi | None]:
    """The real TF-003 functions when installed, else the contract fake."""
    real = load_portfolio_api()
    if real is not None:
        return real, None
    fake = FakePortfolioApi()
    return as_portfolio_api(fake), fake


@pytest.fixture(scope="module")
def flow(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Flow]:
    portfolio_api, fake = _portfolio_api()
    base = tmp_path_factory.mktemp("conformance-http")
    with running_service(
        base, tokens={PLANNER: ["read", "plan", "admin"]}, portfolio_api=portfolio_api
    ) as service:
        recorded = Flow(service=service, fake=fake)
        started = time.perf_counter()
        asyncio.run(_run(recorded))
        recorded.seconds = time.perf_counter() - started
        yield recorded


# --- initialize and tools/list (REQ-163, REQ-164) -----------------------------------------------


def test_initialize_negotiates_with_the_twinflow_server(flow: Flow) -> None:
    assert flow.initialize is not None
    assert flow.initialize.server_info.name == "Twinflow"
    assert flow.initialize.protocol_version
    assert flow.initialize.capabilities.tools is not None


def test_tools_list_names_equal_the_catalogue(flow: Flow) -> None:
    assert set(flow.tools) == {spec.name for spec in CATALOGUE}
    assert len(flow.tools) == len(CATALOGUE)


def test_every_listed_tool_publishes_object_input_and_output_schemas(flow: Flow) -> None:
    missing = {
        name: (tool.input_schema.get("type"), (tool.output_schema or {}).get("type"))
        for name, tool in flow.tools.items()
        if tool.input_schema.get("type") != "object"
        or not tool.output_schema
        or tool.output_schema.get("type") != "object"
    }

    assert missing == {}


def test_every_successful_call_validates_against_its_output_schema(flow: Flow) -> None:
    checked = 0
    for step, result in flow.results.items():
        if result.is_error:
            continue
        tool = step.split("#")[0].split("@")[0]
        assert_valid_structured_content(flow.tools[tool], result)
        checked += 1

    assert checked >= len(PLANNING_RESULTS) + 2  # every planning step plus profile put and get


# --- the signed planning profile (REQ-185) ------------------------------------------------------


def test_put_planning_profile_signs_and_activates_the_profile(flow: Flow) -> None:
    put = flow.ok("put_planning_profile")

    assert put["kind"] == "planning_profiles"
    assert len(put["digest"]) == 64 and int(put["digest"], 16) >= 0
    assert put["sequence"] == 1
    assert isinstance(put["key_id"], str) and len(put["key_id"]) == 16


def test_the_put_profile_is_the_verified_active_profile(flow: Flow) -> None:
    put = flow.ok("put_planning_profile")
    active = flow.ok("get_planning_profile")

    assert (active["kind"], active["digest"], active["sequence"], active["key_id"]) == (
        put["kind"],
        put["digest"],
        put["sequence"],
        put["key_id"],
    )
    weights = active["artifact"]["erp"]["class_weights"]
    assert weights == {"A-KEY": 4, "B-CORE": 2, "C-SPOT": 1}


def test_built_problem_names_the_activated_profile(flow: Flow) -> None:
    assert (
        flow.ok("build_problem_from_erp")["profile_digest"]
        == flow.ok("put_planning_profile")["digest"]
    )


# --- build, solve, menu, explain (REQ-179) ------------------------------------------------------


def test_validate_snapshot_reports_no_hard_issue_for_small_shop(flow: Flow) -> None:
    report = flow.ok("validate_snapshot")

    assert report["hard_count"] == 0, report["issues"]
    assert not [issue for issue in report["issues"] if issue["severity"] == "hard"]
    assert report["snapshot_digest"]


def test_build_problem_from_erp_is_ready_with_a_problem_id(flow: Flow) -> None:
    built = flow.ok("build_problem_from_erp")

    assert built["status"] == "ready"
    assert built["problem_id"]
    assert built["snapshot_digest"] == flow.ok("validate_snapshot")["snapshot_digest"]


def test_solve_portfolio_returns_a_portfolio_of_plans_for_the_problem(flow: Flow) -> None:
    portfolio = flow.ok("solve_portfolio")

    assert portfolio["portfolio_id"]
    assert portfolio["problem_id"] == flow.ok("build_problem_from_erp")["problem_id"]
    assert len(portfolio["plan_ids"]) >= 2, portfolio
    assert len(set(portfolio["plan_ids"])) == len(portfolio["plan_ids"])


def test_get_pareto_menu_points_at_plans_of_the_portfolio(flow: Flow) -> None:
    menu = flow.ok("get_pareto_menu")
    portfolio = flow.ok("solve_portfolio")

    assert menu["portfolio_id"] == portfolio["portfolio_id"]
    assert menu["entries"], menu
    assert {entry["plan_id"] for entry in menu["entries"]} <= set(portfolio["plan_ids"])


def test_explain_plan_is_capability_unavailable(flow: Flow) -> None:
    result = flow.results["explain_plan"]

    assert result.is_error is True, describe(result)
    assert error_payload(result)["code"] == "capability_unavailable"


# --- overrides, rush, compare ----------------------------------------------------------------


def test_resolve_with_overrides_stores_a_new_portfolio(flow: Flow) -> None:
    resolved = flow.ok("resolve_with_overrides")
    original = flow.ok("solve_portfolio")

    assert resolved["portfolio_id"] != original["portfolio_id"]
    assert resolved["plan_ids"], resolved
    assert not set(resolved["plan_ids"]) & set(original["plan_ids"])


def test_resolve_with_overrides_reached_the_solver_with_both_overrides(flow: Flow) -> None:
    if flow.fake is None:
        pytest.skip("the real TF-003 has no spy surface")
    assert flow.fake.overrides is not None
    assert [override.kind for override in flow.fake.overrides] == ["lock", "prefer_machine"]


def test_insert_rush_order_returns_a_plan_with_a_relaxations_list(flow: Flow) -> None:
    rush = flow.ok("insert_rush_order")

    assert rush["plan_id"]
    assert rush["plan_id"] not in flow.ok("solve_portfolio")["plan_ids"]
    assert isinstance(rush["relaxations"], list)


def test_compare_plans_ranks_both_plans_of_the_problem(flow: Flow) -> None:
    compared = flow.ok("compare_plans")
    plan_ids = flow.ok("solve_portfolio")["plan_ids"][:2]

    assert compared["problem_id"] == flow.ok("build_problem_from_erp")["problem_id"]
    assert sorted(compared["ranking"]) == sorted(plan_ids)
    assert {row["plan_id"] for row in compared["plans"]} == set(plan_ids)


# --- get_plan pagination (REQ-168) ---------------------------------------------------------------


def test_get_plan_pages_cover_every_assignment_once_with_consistent_totals(flow: Flow) -> None:
    pages = flow.pages
    total = pages[0]["total"]
    rows = [row for page in pages for row in page["assignments"]]

    assert len(pages) >= 2, "the plan fits one page; PAGE_LIMIT no longer exercises paging"
    assert {page["total"] for page in pages} == {total}
    assert [page["offset"] for page in pages] == list(range(0, total, PAGE_LIMIT))
    assert all(len(page["assignments"]) <= PAGE_LIMIT for page in pages)
    assert all(page["next_offset"] is not None for page in pages[:-1])
    assert pages[-1]["next_offset"] is None
    assert len(rows) == total
    assert len({row["operation_id"] for row in rows}) == total  # no duplicates
    assert rows == flow.full_plan["assignments"]  # same stored order as one big page


# --- simulation link (TF-004 REQ-140 supersedes TF-005 REQ-171) ----------------------------------


def test_simulate_plan_on_an_erp_plan_completes_with_a_stored_simulation(flow: Flow) -> None:
    simulated = flow.ok("simulate_plan")

    assert simulated["status"] in ("completed", "truncated")
    assert isinstance(simulated["simulation_id"], str) and simulated["simulation_id"]
    assert "simulation_link_pending" not in {issue["code"] for issue in simulated["issues"]}
    assert simulated["evidence_kind"] == "simulation"
    assert "job" not in simulated


# --- provisional evidence (REQ-177) -----------------------------------------------------------


@pytest.mark.parametrize("step", PLANNING_RESULTS)
def test_every_planning_result_is_provisional_evidence(flow: Flow, step: str) -> None:
    result = flow.ok(step)

    assert result["validity"] == "provisional"
    assert isinstance(result["evidence_kind"], str) and result["evidence_kind"]


# --- idempotency (REQ-167) ------------------------------------------------------------------------


def test_replaying_solve_portfolio_with_the_same_key_returns_the_same_portfolio(
    flow: Flow,
) -> None:
    replay = flow.ok("solve_portfolio#replay")

    assert replay == flow.ok("solve_portfolio")


def test_replaying_solve_portfolio_does_not_solve_again(flow: Flow) -> None:
    if flow.fake is None:
        pytest.skip("the real TF-003 has no call counter")
    assert flow.solve_calls_before_replay == 1
    assert flow.solve_calls_after_replay == flow.solve_calls_before_replay


def test_same_key_with_different_input_is_an_idempotency_conflict(flow: Flow) -> None:
    result = flow.results["solve_portfolio#conflict"]

    assert result.is_error is True, describe(result)
    assert error_payload(result)["code"] == "idempotency_conflict"


# --- size limit (REQ-168) -------------------------------------------------------------------------


def _assert_size_refusal(response: httpx.Response) -> None:
    assert response.status_code == 413, (response.status_code, response.text[:300])
    assert response.json()["code"] == "size_limit"


def test_mcp_body_of_16_mib_plus_one_byte_is_refused_413(flow: Flow) -> None:
    body = b" " * (MAX_MCP_BODY + 1)
    headers = {
        **flow.service.bearer(PLANNER),
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    response = httpx.post(flow.service.mcp_url, content=body, headers=headers, timeout=30)

    _assert_size_refusal(response)


def test_chunked_mcp_body_over_16_mib_is_refused_413(flow: Flow) -> None:
    """Without Content-Length the middleware counts the streamed bytes itself."""
    chunk = b" " * (1024 * 1024)

    def stream() -> Iterator[bytes]:
        for _ in range(16):
            yield chunk
        yield b" "

    headers = {
        **flow.service.bearer(PLANNER),
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    response = httpx.post(flow.service.mcp_url, content=stream(), headers=headers, timeout=30)

    _assert_size_refusal(response)
