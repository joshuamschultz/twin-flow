"""T-218: MCP overhead for `get_plan` over streamable HTTP stays under 50 ms at p95.

Overhead = p95 of the full HTTP tool call minus p95 of the same `PlanningService.get_plan`
called in process on the same workspace, each over 200 calls (PRD success metric).
"""

# ruff: noqa: E402 -- tests/planning and tests/erp helpers join sys.path before import.

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from mcp import ClientSession
from twinflow.application.planning import PlanningService
from twinflow.application.workspace import Workspace

ROOT = Path(__file__).resolve().parents[2]
for _tests_dir in ("planning", "erp"):
    _path = str(ROOT / "tests" / _tests_dir)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _fake_portfolio import FakePortfolioApi, as_portfolio_api
from _http_server import running_service
from fixtures.generate import small_shop

CALLS = 200
BUDGET_MS = 50.0
PROFILE_TOML = '[erp.class_weights]\n"A-KEY" = 4\n"B-CORE" = 2\n"C-SPOT" = 1\n'
PAGE = {"offset": 0, "limit": 100}


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[-1]


async def _structured(session: ClientSession, tool: str, arguments: dict[str, Any]) -> Any:
    result = await session.call_tool(tool, arguments)
    assert not result.is_error, result
    return result.structured_content


async def _http_samples(url: str, headers: dict[str, str]) -> tuple[str, list[float]]:
    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await _structured(session, "put_planning_profile", {"profile_toml": PROFILE_TOML})
                snapshot = small_shop().model_dump(mode="json")
                built = await _structured(session, "build_problem_from_erp", {"snapshot": snapshot})
                solved = await _structured(
                    session,
                    "solve_portfolio",
                    {"problem_id": built["problem_id"], "request_key": "latency"},
                )
                plan_id = solved["plan_ids"][0]
                samples = []
                for _ in range(CALLS):
                    started = time.perf_counter()
                    await _structured(session, "get_plan", {"plan_id": plan_id, **PAGE})
                    samples.append((time.perf_counter() - started) * 1000)
    return plan_id, samples


def _in_process_samples(workspace_root: Path, plan_id: str) -> list[float]:
    workspace = Workspace(workspace_root, ROOT / "examples")
    planning = PlanningService(workspace, portfolio=None)
    try:
        samples = []
        for _ in range(CALLS):
            started = time.perf_counter()
            planning.get_plan(plan_id, **PAGE)
            samples.append((time.perf_counter() - started) * 1000)
        return samples
    finally:
        planning.close()
        workspace.close()


@pytest.mark.slow
def test_mcp_get_plan_overhead_is_under_50_ms_at_p95(tmp_path: Path) -> None:
    portfolio = as_portfolio_api(FakePortfolioApi())
    with running_service(
        tmp_path, tokens={"planner": ["read", "plan"]}, portfolio_api=portfolio
    ) as service:
        plan_id, http = asyncio.run(_http_samples(service.mcp_url, service.bearer("planner")))
        workspace_root = service.workspace_root
    local = _in_process_samples(workspace_root, plan_id)

    overhead = _p95(http) - _p95(local)
    print(f"get_plan p95: http {_p95(http):.1f} ms, in-process {_p95(local):.1f} ms")
    assert overhead < BUDGET_MS, f"MCP overhead {overhead:.1f} ms at p95"
