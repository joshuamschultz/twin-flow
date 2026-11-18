"""Simulation tools over streamable HTTP with the official SDK client (T-188, COMP-140).

Requirements: REQ-139 (replication and budget caps published), REQ-140 (`simulate_plan`
for any verified ERP plan, the new `simulate_portfolio`, `compare_plans` with a
`simulation` block, keyed and idempotent, a solve slot, `describe_capabilities`), REQ-141
(`query_evidence` pages schedule rows of a stored simulation). Decisions: D-105
(synchronous, capped, idempotent, by reference), D-106 (schedule evidence from simulation
records). TF-005 REQ-171 (`simulation_link_pending`, `wait_seconds`, 10-rep cap) is
superseded by REQ-140.

Contract pinned here (SDD Data Model "Tool inputs", "Outputs", "Evidence rows"):

* `simulate_plan` input `{plan_id, request_key, reps: int 1..1000 | None = None, seed = 42,
  time_budget_seconds: float > 0 finite | None = None, baseline = False,
  profile_digest: str | None = None}`; no `wait_seconds`. Output `SimulateResult{plan_id,
  status: completed | truncated | blocked, simulation_id?, issues, summary?, baseline?,
  validity, evidence_kind: "simulation"}` (no scenario `job`).
* `simulate_portfolio` input `{portfolio_id, request_key, reps?, seed = 42,
  time_budget_seconds?, baseline = True, profile_digest?}`. Output `PortfolioSimulation`.
* Both are `state_modifying` with `idempotentHint`, hold a solve slot and time out at
  `max_simulate_seconds + 15`. `compare_plans` stays `read_only` and takes
  `simulation_ids` (same length as `plan_ids`, or None).
* `query_evidence` takes exactly one of `job_id` and `simulation_id`; topic `schedule`
  with a `simulation_id` pages operation rows then job rows; other topics keep `job_id`.
* `describe_capabilities` publishes, top level and named like the `ServiceSettings`
  fields: `max_simulate_seconds`, `max_simulation_replications`,
  `max_simulation_candidates`, `max_simulation_workers`, plus `simulation_sources`
  (`["deterministic", "profile", "capsule"]`) and `baseline_rules`
  (`twinflow.policy.DISPATCH_NAMES`).

The real `create_app` runs under uvicorn (`_http_server`) with the planning tests'
`FakePortfolioApi` (real TF-002 solving underneath) and the TF-005 `small_shop` fixture;
the profile has no `[simulation]` table, so the deterministic source applies. These
tests are about the surface, not the simulation numbers.
"""

# ruff: noqa: E402 -- the agent extra is optional: skip before importing anything needing `mcp`.

from __future__ import annotations

import pytest

pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

import asyncio
import math
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from pydantic import ValidationError

from mcp import ClientSession, types
from twinflow.mcp import models as m
from twinflow.mcp.catalogue import SPECS
from twinflow.mcp.guard import ToolGuard
from twinflow.policy import DISPATCH_NAMES
from twinflow.service.limits import CallerLimiter

ROOT = Path(__file__).resolve().parents[2]
for _tests_dir in ("planning", "erp"):  # the TF-003 double and the small_shop generator
    _path = str(ROOT / "tests" / _tests_dir)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _fake_portfolio import FakePortfolioApi, as_portfolio_api
from _http_server import RunningService, running_service
from _mcp_helpers import (
    assert_valid_structured_content,
    build_services,
    describe,
    error_payload,
)
from fixtures.generate import small_shop

PLANNER = "planner"
PROFILE_TOML = '[erp.class_weights]\n"A-KEY" = 4\n"B-CORE" = 2\n"C-SPOT" = 1\n'
CLIENT_TIMEOUT_SECONDS = 120.0
SIMULATE_GRACE_SECONDS = 15.0
MAX_SOLVE_SECONDS = 90.0  # differs from the simulate cap, so the two timeouts are told apart
DEFAULT_SIMULATE_SECONDS = 120.0
EVIDENCE_PAGE = 25  # few pages: the caller rate limit (burst) must not cut paging short
SIMULATE_TOOLS = ("simulate_plan", "simulate_portfolio")
SIMULATION_SOURCES = ["deterministic", "profile", "capsule"]


# --- the recorded flow -------------------------------------------------------------------------


@dataclass
class Flow:
    """Everything one client session saw, by step name, plus the seeded ids."""

    service: RunningService
    tools: dict[str, types.Tool] = field(default_factory=dict)
    results: dict[str, types.CallToolResult] = field(default_factory=dict)
    ids: dict[str, Any] = field(default_factory=dict)
    evidence_pages: list[dict[str, Any]] = field(default_factory=list)

    def result(self, step: str) -> types.CallToolResult:
        assert step in self.results, f"step {step!r} never ran (an earlier step failed)"
        return self.results[step]

    def ok(self, step: str) -> dict[str, Any]:
        """The structured content of a step that must have succeeded, schema checked."""
        result = self.result(step)
        assert not result.is_error, f"{step} failed: {describe(result)}"
        tool = step.split("#")[0]
        assert_valid_structured_content(self.tools[tool], result)
        assert result.structured_content is not None
        return result.structured_content

    def error_code(self, step: str) -> str:
        result = self.result(step)
        assert result.is_error, f"{step} should have failed: {describe(result)}"
        return str(error_payload(result)["code"])


class Recorder:
    """Calls tools through the official client session and records each result."""

    def __init__(self, session: ClientSession, flow: Flow) -> None:
        self.session = session
        self.flow = flow

    async def call(self, step: str, tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result = await self.session.call_tool(tool, arguments)
        self.flow.results[step] = result
        return result

    async def seed(self, step: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """A seeding call through an existing tool; it must succeed."""
        result = await self.call(step, tool, arguments)
        assert not result.is_error, f"seed step {step} failed: {describe(result)}"
        assert result.structured_content is not None
        return result.structured_content

    async def maybe(self, step: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """A call under test: recorded either way; its content when it succeeded."""
        result = await self.call(step, tool, arguments)
        return None if result.is_error else result.structured_content


async def _list_tools(session: ClientSession) -> dict[str, types.Tool]:
    tools: dict[str, types.Tool] = {}
    cursor: str | None = None
    while True:
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await session.list_tools(params=params)
        for tool in page.tools:
            tools[tool.name] = tool
        cursor = page.next_cursor
        if not cursor:
            return tools


def _verified_plans(portfolio: dict[str, Any]) -> list[str]:
    """The portfolio's plans whose stored record verified, in portfolio order."""
    return [row["plan_id"] for row in portfolio["statuses"] if row["verified"]]


async def _seed_planning(rec: Recorder) -> None:
    """Profile, problem, portfolio and one completed scenario job (all existing tools)."""
    ids = rec.flow.ids
    await rec.seed("put_planning_profile", "put_planning_profile", {"profile_toml": PROFILE_TOML})
    built = await rec.seed(
        "build_problem_from_erp",
        "build_problem_from_erp",
        {"snapshot": small_shop().model_dump(mode="json")},
    )
    assert built["status"] == "ready", built
    portfolio = await rec.seed(
        "solve_portfolio",
        "solve_portfolio",
        {"problem_id": built["problem_id"], "request_key": "sim-seed-solve"},
    )
    plan_ids = list(portfolio["plan_ids"])
    assert len(plan_ids) >= 2, portfolio
    # Only verified plans simulate (D-087: an unverified plan is blocked / plan_unverified).
    # The fake's delayed candidate does not verify on small_shop, so a second solve of the
    # same problem supplies the second verified plan.
    again = await rec.seed(
        "solve_portfolio#second",
        "solve_portfolio",
        {"problem_id": built["problem_id"], "request_key": "sim-seed-solve-2", "seed": 1},
    )
    verified = _verified_plans(portfolio) + _verified_plans(again)
    assert len(verified) >= 2, f"need two verified plans of one problem, got {verified}"
    ids.update(portfolio=portfolio["portfolio_id"], plans=plan_ids, p1=verified[0], p2=verified[1])
    office = await rec.seed("load_example", "load_example", {"name": "capsule-office"})
    job = await rec.seed(
        "evaluate",
        "evaluate",
        {"scenario_id": office["id"], "request_key": "sim-seed-job", "reps": 1, "seed": 42},
    )
    ids["job"] = job["id"]
    await _wait_for_job(rec, job["id"])


async def _wait_for_job(rec: Recorder, job_id: str) -> None:
    with anyio.fail_after(60):
        while True:
            job = await rec.seed("get_job", "get_job", {"job_id": job_id})
            if job["status"] in ("completed", "failed", "canceled"):
                assert job["status"] == "completed", job
                return
            await asyncio.sleep(0.05)


async def _simulate(rec: Recorder) -> None:
    ids = rec.flow.ids
    p1, p2 = ids["p1"], ids["p2"]
    first = {"plan_id": p1, "request_key": "sim-plan-1"}
    simulated = await rec.maybe("simulate_plan", "simulate_plan", first)
    await rec.call("simulate_plan#replay", "simulate_plan", first)
    await rec.call("simulate_plan#conflict", "simulate_plan", {**first, "seed": 7})
    await rec.call(
        "simulate_plan#baseline",
        "simulate_plan",
        {"plan_id": p1, "request_key": "sim-plan-baseline", "baseline": True},
    )
    second = await rec.maybe(
        "simulate_plan#p2", "simulate_plan", {"plan_id": p2, "request_key": "sim-plan-2"}
    )
    other_seed = await rec.maybe(
        "simulate_plan#p2-seed7",
        "simulate_plan",
        {"plan_id": p2, "request_key": "sim-plan-2-seed7", "seed": 7},
    )
    await rec.call(
        "simulate_plan#wait_seconds",
        "simulate_plan",
        {"plan_id": p1, "request_key": "sim-wait", "wait_seconds": 5},
    )
    await rec.call(
        "simulate_plan#reps1001",
        "simulate_plan",
        {"plan_id": p1, "request_key": "sim-reps", "reps": 1001},
    )
    await rec.call(
        "simulate_plan#budget0",
        "simulate_plan",
        {"plan_id": p1, "request_key": "sim-budget", "time_budget_seconds": 0},
    )
    await rec.call(
        "simulate_portfolio",
        "simulate_portfolio",
        {"portfolio_id": ids["portfolio"], "request_key": "sim-portfolio"},
    )
    for name, content in (("s1", simulated), ("s2", second), ("s2_seed7", other_seed)):
        if content is not None:
            ids[name] = content.get("simulation_id")


async def _compare(rec: Recorder) -> None:
    ids = rec.flow.ids
    plans = [ids["p1"], ids["p2"]]
    await rec.call("compare_plans#plain", "compare_plans", {"plan_ids": plans})
    await rec.call(
        "compare_plans#short_ids", "compare_plans", {"plan_ids": plans, "simulation_ids": ["x"]}
    )
    if not (ids.get("s1") and ids.get("s2")):
        return
    await rec.call(
        "compare_plans",
        "compare_plans",
        {"plan_ids": plans, "simulation_ids": [ids["s1"], ids["s2"]]},
    )
    await rec.call(
        "compare_plans#swapped",
        "compare_plans",
        {"plan_ids": plans, "simulation_ids": [ids["s2"], ids["s1"]]},
    )
    if ids.get("s2_seed7"):
        await rec.call(
            "compare_plans#seed",
            "compare_plans",
            {"plan_ids": plans, "simulation_ids": [ids["s1"], ids["s2_seed7"]]},
        )


async def _evidence(rec: Recorder) -> None:
    ids = rec.flow.ids
    job_id, s1 = ids["job"], ids.get("s1") or "sim-missing"
    await rec.call(
        "query_evidence#both",
        "query_evidence",
        {"job_id": job_id, "simulation_id": s1, "topic": "schedule"},
    )
    await rec.call("query_evidence#neither", "query_evidence", {"topic": "metrics"})
    await rec.call(
        "query_evidence#schedule_by_job", "query_evidence", {"job_id": job_id, "topic": "schedule"}
    )
    await rec.call(
        "query_evidence#metrics_by_job",
        "query_evidence",
        {"job_id": job_id, "topic": "metrics", "offset": 0, "limit": 5},
    )
    await rec.call(
        "query_evidence#limit1001",
        "query_evidence",
        {"simulation_id": s1, "topic": "schedule", "limit": 1001},
    )
    full = await rec.maybe(
        "query_evidence",
        "query_evidence",
        {"simulation_id": s1, "topic": "schedule", "offset": 0, "limit": 1000},
    )
    if full is None:
        return
    await _page_evidence(rec, s1)
    job_rows = [row for row in full["evidence"] if row.get("row") == "job"]
    if job_rows:
        ids["evidence_job"] = job_rows[0]["job_id"]
        await rec.call(
            "query_evidence#entity",
            "query_evidence",
            {"simulation_id": s1, "topic": "schedule", "entity_id": ids["evidence_job"]},
        )


async def _page_evidence(rec: Recorder, simulation_id: str) -> None:
    offset: int | None = 0
    while offset is not None:
        page = await rec.maybe(
            f"query_evidence#page{offset}",
            "query_evidence",
            {
                "simulation_id": simulation_id,
                "topic": "schedule",
                "offset": offset,
                "limit": EVIDENCE_PAGE,
            },
        )
        if page is None:
            return
        rec.flow.evidence_pages.append(page)
        assert len(rec.flow.evidence_pages) <= 1000, "evidence pagination never ended"
        offset = page["next_offset"]


async def _run(flow: Flow) -> None:
    headers = flow.service.bearer(PLANNER)
    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(flow.service.mcp_url, http_client=http_client) as streams:
            read_stream, write_stream = streams
            async with ClientSession(
                read_stream, write_stream, read_timeout_seconds=CLIENT_TIMEOUT_SECONDS
            ) as session:
                await session.initialize()
                flow.tools = await _list_tools(session)
                rec = Recorder(session, flow)
                await rec.call("describe_capabilities", "describe_capabilities", {})
                await _seed_planning(rec)
                await _simulate(rec)
                await _compare(rec)
                await _evidence(rec)


@pytest.fixture(scope="module")
def flow(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Flow]:
    base = tmp_path_factory.mktemp("simulation-tools")
    with running_service(
        base,
        tokens={PLANNER: ["read", "plan", "admin"]},
        portfolio_api=as_portfolio_api(FakePortfolioApi()),
        settings_overrides={"max_solve_seconds": MAX_SOLVE_SECONDS},
    ) as service:
        recorded = Flow(service=service)
        asyncio.run(_run(recorded))
        yield recorded


# --- schema helpers ----------------------------------------------------------------------------


def _branch(prop: dict[str, Any], kind: str) -> dict[str, Any]:
    """The JSON-schema branch of `prop` whose type is `kind` (direct or under anyOf)."""
    for candidate in [prop, *prop.get("anyOf", [])]:
        if candidate.get("type") == kind:
            return candidate
    raise AssertionError(f"no {kind!r} branch in {prop}")


def _nullable(prop: dict[str, Any]) -> bool:
    return any(branch.get("type") == "null" for branch in prop.get("anyOf", []))


def _input(flow: Flow, tool: str) -> dict[str, Any]:
    assert tool in flow.tools, f"tool {tool!r} is not listed"
    schema: dict[str, Any] = flow.tools[tool].input_schema
    return schema


# --- tools/list: classification and hints (REQ-140) --------------------------------------------


@pytest.mark.parametrize("tool", SIMULATE_TOOLS)
def test_simulate_tools_are_state_modifying_idempotent_and_need_plan_scope(
    flow: Flow, tool: str
) -> None:
    assert tool in flow.tools, sorted(flow.tools)
    listed = flow.tools[tool]
    meta = listed.meta or {}
    hints = listed.annotations

    assert meta.get("twinflow/classification") == "state_modifying"
    assert meta.get("twinflow/scope") == "plan"
    assert hints is not None
    assert (hints.read_only_hint, hints.idempotent_hint, hints.destructive_hint) == (
        False,
        True,
        False,
    )


def test_compare_plans_stays_read_only(flow: Flow) -> None:
    listed = flow.tools["compare_plans"]

    assert (listed.meta or {}).get("twinflow/classification") == "read_only"
    assert listed.annotations is not None and listed.annotations.read_only_hint is True


# --- tools/list: input schemas per the Data Model ------------------------------------------------


def test_simulate_plan_input_schema_matches_the_data_model(flow: Flow) -> None:
    schema = _input(flow, "simulate_plan")
    props = schema["properties"]

    assert set(props) == {
        "plan_id",
        "request_key",
        "reps",
        "seed",
        "time_budget_seconds",
        "baseline",
        "profile_digest",
    }
    assert set(schema.get("required", [])) == {"plan_id", "request_key"}
    reps = _branch(props["reps"], "integer")
    assert (reps.get("minimum"), reps.get("maximum")) == (1, 1000)
    assert _nullable(props["reps"]) and props["reps"].get("default") is None
    assert props["seed"].get("default") == 42
    assert props["baseline"].get("default") is False
    budget = _branch(props["time_budget_seconds"], "number")
    assert budget.get("exclusiveMinimum") == 0
    assert _nullable(props["time_budget_seconds"])
    assert _nullable(props["profile_digest"])


def test_simulate_plan_has_no_wait_seconds(flow: Flow) -> None:
    assert "wait_seconds" not in _input(flow, "simulate_plan")["properties"]
    assert flow.error_code("simulate_plan#wait_seconds") == "invalid_input"


def test_simulate_portfolio_input_schema_matches_the_data_model(flow: Flow) -> None:
    schema = _input(flow, "simulate_portfolio")
    props = schema["properties"]

    assert set(props) == {
        "portfolio_id",
        "request_key",
        "reps",
        "seed",
        "time_budget_seconds",
        "baseline",
        "profile_digest",
    }
    assert set(schema.get("required", [])) == {"portfolio_id", "request_key"}
    assert (_branch(props["reps"], "integer").get("maximum")) == 1000
    assert props["seed"].get("default") == 42
    assert props["baseline"].get("default") is True
    assert _branch(props["time_budget_seconds"], "number").get("exclusiveMinimum") == 0


def test_compare_plans_input_takes_optional_simulation_ids(flow: Flow) -> None:
    schema = _input(flow, "compare_plans")
    props = schema["properties"]

    assert set(props) == {"plan_ids", "simulation_ids"}
    assert set(schema.get("required", [])) == {"plan_ids"}
    plan_ids = _branch(props["plan_ids"], "array")
    assert (plan_ids.get("minItems"), plan_ids.get("maxItems")) == (2, 10)
    assert _nullable(props["simulation_ids"])
    assert _branch(props["simulation_ids"], "array").get("maxItems") == 10


def test_query_evidence_input_takes_job_id_or_simulation_id(flow: Flow) -> None:
    schema = _input(flow, "query_evidence")
    props = schema["properties"]

    assert {"job_id", "simulation_id", "topic", "entity_id", "offset", "limit"} <= set(props)
    required = set(schema.get("required", []))
    assert "job_id" not in required and "simulation_id" not in required
    assert _branch(props["limit"], "integer").get("maximum") == 1000


# --- input bounds (validated by the guard: invalid_input, nothing runs) ------------------------


def test_simulate_plan_bounds_reps_at_1000_and_refuses_a_zero_budget(flow: Flow) -> None:
    props = _input(flow, "simulate_plan")["properties"]

    # The bound is 1000 (REQ-139 replaces the fixed 10); 1001 and a zero budget are refused.
    assert _branch(props["reps"], "integer").get("maximum") == 1000
    assert flow.error_code("simulate_plan#reps1001") == "invalid_input"
    assert "time_budget_seconds" in props
    assert flow.error_code("simulate_plan#budget0") == "invalid_input"


def test_simulate_input_accepts_a_finite_budget_and_refuses_a_non_finite_one() -> None:
    valid = {"plan_id": "p", "request_key": "k"}

    accepted = m.SimulateInput.model_validate({**valid, "time_budget_seconds": 30.0, "reps": 1000})

    assert accepted.time_budget_seconds == 30.0
    for bad in (math.inf, math.nan):
        with pytest.raises(ValidationError):
            m.SimulateInput.model_validate({**valid, "time_budget_seconds": bad})


def test_compare_plans_takes_same_length_simulation_ids_and_refuses_another_length(
    flow: Flow,
) -> None:
    assert flow.ok("compare_plans")["simulation"] is not None  # same length: served
    assert flow.error_code("compare_plans#short_ids") == "invalid_input"


# --- simulate_plan results (REQ-140, D-105) ---------------------------------------------------


def test_simulate_plan_on_an_erp_plan_completes_with_a_stored_simulation(flow: Flow) -> None:
    simulated = flow.ok("simulate_plan")

    assert simulated["plan_id"] == flow.ids["p1"]
    assert simulated["status"] in ("completed", "truncated")
    assert isinstance(simulated["simulation_id"], str) and simulated["simulation_id"]
    assert simulated["summary"]["simulation_id"] == simulated["simulation_id"]
    assert simulated["summary"]["plan_id"] == flow.ids["p1"]
    assert simulated["baseline"] is None
    codes = {issue["code"] for issue in simulated["issues"]}
    assert "simulation_link_pending" not in codes
    assert "job" not in simulated  # the scenario job shape (TF-005 REQ-171) is gone
    assert simulated["validity"] == "provisional"
    assert simulated["evidence_kind"] == "simulation"


def test_simulate_plan_key_replays_its_result_and_refuses_other_input(flow: Flow) -> None:
    assert flow.ok("simulate_plan#replay") == flow.ok("simulate_plan")
    assert flow.error_code("simulate_plan#conflict") == "idempotency_conflict"


def test_simulate_plan_with_baseline_reports_the_baseline_and_paired_deltas(flow: Flow) -> None:
    simulated = flow.ok("simulate_plan#baseline")

    assert simulated["status"] in ("completed", "truncated")
    assert simulated["baseline"] is not None
    assert simulated["baseline"]["kind"] == "baseline"
    assert simulated["summary"]["baseline_delta"], simulated["summary"]


# --- simulate_portfolio (REQ-140, D-108) ----------------------------------------------------


def test_simulate_portfolio_simulates_the_candidates_and_the_baseline(flow: Flow) -> None:
    result = flow.ok("simulate_portfolio")
    plan_ids = set(flow.ids["plans"])

    assert result["portfolio_id"] == flow.ids["portfolio"]
    assert result["status"] in ("completed", "truncated")
    assert isinstance(result["evaluation_id"], str) and result["evaluation_id"]
    assert result["candidates"], result
    assert {candidate["plan_id"] for candidate in result["candidates"]} <= plan_ids
    assert result["baseline"] is not None  # baseline defaults to True for portfolios
    assert result["ranking"] and set(result["ranking"]) <= plan_ids
    assert result["seed"] == 42


def test_simulate_portfolio_result_is_provisional_portfolio_evidence(flow: Flow) -> None:
    result = flow.ok("simulate_portfolio")

    assert result["validity"] == "provisional"
    assert result["evidence_kind"] == "portfolio_simulation"


# --- compare_plans with simulation_ids (REQ-138, REQ-140) -------------------------------------


def test_compare_plans_with_simulation_ids_adds_the_simulation_block(flow: Flow) -> None:
    compared = flow.ok("compare_plans")
    p1, p2 = flow.ids["p1"], flow.ids["p2"]
    block = compared["simulation"]

    assert block is not None, compared
    assert block["seed"] == 42
    assert block["reference_plan_id"] == p1
    assert p2 in block["deltas"] and set(block["deltas"]) <= {p1, p2}
    assert sorted(block["ranking"]) == sorted([p1, p2])


def test_compare_plans_without_simulation_ids_has_no_simulation_block(flow: Flow) -> None:
    compared = flow.ok("compare_plans#plain")

    assert compared["simulation"] is None


def test_compare_plans_with_another_plans_simulation_is_incomparable(flow: Flow) -> None:
    assert flow.error_code("compare_plans#swapped") == "incomparable_simulations"


def test_compare_plans_with_simulations_of_other_seeds_is_incomparable(flow: Flow) -> None:
    assert flow.error_code("compare_plans#seed") == "incomparable_simulations"


# --- query_evidence (REQ-141, D-106) ----------------------------------------------------------


def test_query_evidence_needs_exactly_one_of_job_id_and_simulation_id(flow: Flow) -> None:
    assert flow.ok("query_evidence")["simulation_id"] == flow.ids["s1"]  # one id: served
    assert flow.error_code("query_evidence#both") == "invalid_input"
    assert flow.error_code("query_evidence#neither") == "invalid_input"


def test_query_evidence_schedule_topic_is_served_by_simulation_id_not_job_id(flow: Flow) -> None:
    assert flow.ok("query_evidence")["topic"] == "schedule"
    assert flow.error_code("query_evidence#schedule_by_job") == "invalid_input"


# Deliberately green before T-188: other topics must stay unchanged (D-106).


def test_query_evidence_other_topics_still_take_a_job_id(flow: Flow) -> None:
    page = flow.ok("query_evidence#metrics_by_job")

    assert page["topic"] == "metrics"
    assert page["job_id"] == flow.ids["job"]
    assert page["evidence"]


def test_query_evidence_schedule_limit_is_at_most_1000(flow: Flow) -> None:
    assert flow.ok("query_evidence")["offset"] == 0  # limit 1000 is accepted
    assert flow.error_code("query_evidence#limit1001") == "invalid_input"


def test_query_evidence_schedule_returns_operation_rows_then_job_rows(flow: Flow) -> None:
    page = flow.ok("query_evidence")
    rows = page["evidence"]
    kinds = [row["row"] for row in rows]

    assert page["simulation_id"] == flow.ids["s1"]
    assert page["topic"] == "schedule"
    assert page["validity"] == "provisional"
    assert page["next_offset"] is None and page["total"] == len(rows)
    assert "operation" in kinds and "job" in kinds
    assert kinds == sorted(kinds, key=lambda kind: kind != "operation")  # ops first
    assert {row["plan_id"] for row in rows} == {flow.ids["p1"]}
    operation = next(row for row in rows if row["row"] == "operation")
    assert {"operation_id", "job_id", "machine_id", "start_p50", "end_p90", "slip_mean"} <= set(
        operation
    )
    job = next(row for row in rows if row["row"] == "job")
    assert {"job_id", "completion_p50", "lateness_p90", "on_time_share"} <= set(job)


def test_query_evidence_schedule_pages_cover_every_row_once(flow: Flow) -> None:
    full = flow.ok("query_evidence")
    pages = flow.evidence_pages
    rows = [row for page in pages for row in page["evidence"]]

    assert len(pages) >= 2, "the evidence fits one page; EVIDENCE_PAGE no longer pages"
    assert [page["offset"] for page in pages] == list(range(0, full["total"], EVIDENCE_PAGE))
    assert all(page["next_offset"] is not None for page in pages[:-1])
    assert pages[-1]["next_offset"] is None
    assert rows == full["evidence"]


def test_query_evidence_schedule_filters_by_entity_id(flow: Flow) -> None:
    page = flow.ok("query_evidence#entity")
    job_id = flow.ids["evidence_job"]

    assert page["evidence"], page
    assert {row["job_id"] for row in page["evidence"]} == {job_id}


# --- describe_capabilities (REQ-139, REQ-140) -------------------------------------------------


def test_describe_capabilities_publishes_the_simulation_caps_sources_and_rules(
    flow: Flow,
) -> None:
    capabilities = flow.ok("describe_capabilities")

    assert capabilities["max_simulate_seconds"] == DEFAULT_SIMULATE_SECONDS
    assert capabilities["max_simulation_replications"] == 200
    assert capabilities["max_simulation_candidates"] == 20
    assert capabilities["max_simulation_workers"] == 4
    assert capabilities["simulation_sources"] == SIMULATION_SOURCES
    assert capabilities["baseline_rules"] == list(DISPATCH_NAMES)


def test_simulate_tools_time_out_at_the_simulate_cap_plus_grace(flow: Flow) -> None:
    timeouts = flow.ok("describe_capabilities")["tool_timeouts"]

    for tool in SIMULATE_TOOLS:
        assert timeouts[tool] == DEFAULT_SIMULATE_SECONDS + SIMULATE_GRACE_SECONDS, tool
    # the solve tools keep following the solve cap: the two caps are independent
    assert timeouts["solve_portfolio"] == MAX_SOLVE_SECONDS + SIMULATE_GRACE_SECONDS


def test_configured_simulation_settings_reach_capabilities_and_timeouts(tmp_path: Path) -> None:
    overrides = {
        "max_simulate_seconds": 40.0,
        "max_simulation_replications": 150,
        "max_simulation_candidates": 7,
        "max_simulation_workers": 3,
    }

    async def body(service: RunningService) -> dict[str, Any]:
        headers = service.bearer(PLANNER)
        async with create_mcp_http_client(headers=headers) as http_client:
            async with streamable_http_client(service.mcp_url, http_client=http_client) as streams:
                read_stream, write_stream = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool("describe_capabilities", {})
                    assert not result.is_error, describe(result)
                    assert result.structured_content is not None
                    return result.structured_content

    with running_service(
        tmp_path, tokens={PLANNER: ["read", "plan"]}, settings_overrides=overrides
    ) as service:
        capabilities = asyncio.run(body(service))

    for name, value in overrides.items():
        assert capabilities[name] == value, name
    for tool in SIMULATE_TOOLS:
        assert capabilities["tool_timeouts"][tool] == 40.0 + SIMULATE_GRACE_SECONDS, tool


# --- solve slot (D-105): simulate tools hold one of the caller's solve slots -------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        # control: an existing solve-type tool, so the mechanism itself is proven here
        ("solve_portfolio", {"problem_id": "prob-x", "request_key": "slot-k"}),
        ("simulate_plan", {"plan_id": "plan-x", "request_key": "slot-k"}),
        ("simulate_portfolio", {"portfolio_id": "pf-x", "request_key": "slot-k"}),
    ],
)
def test_simulate_tools_are_refused_while_the_callers_solve_slots_are_taken(
    tmp_path: Path, tool: str, arguments: dict[str, Any]
) -> None:
    limiter = CallerLimiter(calls_per_minute=10_000, max_concurrent_solves_per_caller=1)
    with build_services(tmp_path) as services:
        guard = ToolGuard.local(services.workspace.repository, limiter=limiter)
        spec = SPECS[tool]

        with limiter.solve_slot("local"):  # the caller's only slot is busy
            busy = asyncio.run(guard.call(spec, services.tools, arguments, None))
        read = asyncio.run(guard.call(SPECS["erp_schema"], services.tools, {}, None))

    assert spec.solve_slot is True
    assert busy.is_error and busy.body["code"] == "rate_limited", busy.body
    assert not read.is_error  # a non-solve tool never needs a slot
