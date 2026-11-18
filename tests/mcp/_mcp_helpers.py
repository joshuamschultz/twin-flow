"""Shared harness for the MCP tool catalogue tests (T-207, COMP-118).

Import it as `from _mcp_helpers import ...` (pytest's default `prepend` import mode puts
this directory on `sys.path`; test basenames stay unique across `tests/`).

What lives here:

* `build_services(base, portfolio=...)`: a real `Workspace`, `DecisionTools` and
  `PlanningService` over `base / "workspace"`, plus a `SignedArtifactStore` whose Ed25519
  key lives in `base / "keys"` (outside the workspace, mode 0600). TF-003 is replaced by the
  planning tests' `FakePortfolioApi` (an external, optional dependency; real TF-002
  solving underneath), so every plan read tool has real plans to read.
* `run_session(server, body)`: one in-memory MCP client session (`mcp.Client(server)`,
  response cache off) against an `MCPServer` built by `twinflow.mcp.tools.build_server`.
* `McpSession.call`: calls a tool and asserts success, structured content, and that the
  structured content validates against the tool's published `outputSchema` (REQ-164).
  Every successful call in the suite goes through it.
* `record_counts(workspace)`: rows of the sqlite `records` table grouped by kind, plus the
  idempotency `requests` table; `audit_events` is deliberately not counted (REQ-165).
"""

# ruff: noqa: E402 -- the agent extra is optional: skip (like the other MCP tests) before
# importing anything that needs `mcp`.

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

import asyncio
import json
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import jsonschema

import mcp
from mcp import types
from twinflow.application.decision_tools import DecisionTools
from twinflow.application.planning import PlanningService
from twinflow.application.workspace import Workspace
from twinflow.mcp.tools import ToolServices, build_server

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"

# Reuse the planning tests' typed TF-003 double and tiny ERP inputs (one source of truth).
_PLANNING_TESTS = str(ROOT / "tests" / "planning")
if _PLANNING_TESTS not in sys.path:
    sys.path.insert(0, _PLANNING_TESTS)

from _fake_portfolio import FakePortfolioApi, as_portfolio_api, tiny_profile, tiny_shop

T = TypeVar("T")

MAX_SOLVE_SECONDS = 90.0  # not the default, so describe_capabilities must report the real one
JOB_WAIT_SECONDS = 30.0


def signed_artifact_store(workspace: Workspace, key_dir: Path) -> Any:
    """A `SignedArtifactStore` over the workspace, signing with a fresh Ed25519 key.

    Imported lazily: COMP-127 is built concurrently, and the catalogue tests must fail RED
    on `twinflow.mcp.tools`, not on the signing module.
    """
    from twinflow.application.artifacts import SignedArtifactStore
    from twinflow.service.signing import ServiceSigner, generate_signing_key

    key_dir.mkdir(mode=0o700, parents=True)
    key_path = key_dir / "service-signing-key.pem"
    generate_signing_key(key_path)  # writes mode 0600 (tests/service/test_signing.py)
    signer = ServiceSigner.load(key_path, workspace_root=workspace.repository.root)
    return SignedArtifactStore(workspace.repository, signer, clock=lambda: datetime.now(UTC))


@dataclass
class Services:
    """The tool services plus the handles the tests close and inspect."""

    tools: ToolServices
    workspace: Workspace
    planning: PlanningService
    root: Path


@contextmanager
def build_services(base: Path, *, with_portfolio: bool = True) -> Iterator[Services]:
    """Real services over `base / "workspace"`; closed on exit."""
    workspace = Workspace(base / "workspace", EXAMPLES)
    portfolio = as_portfolio_api(FakePortfolioApi()) if with_portfolio else None
    planning = PlanningService(workspace, portfolio=portfolio, max_solve_seconds=MAX_SOLVE_SECONDS)
    try:
        artifacts = signed_artifact_store(workspace, base / "keys")
        tools = ToolServices(
            workspace=workspace,
            decisions=DecisionTools(workspace),
            planning=planning,
            artifacts=artifacts,
            models_root=None,
        )
        yield Services(tools=tools, workspace=workspace, planning=planning, root=base)
    finally:
        planning.close()
        workspace.close()


def build(services: Services) -> Any:
    """The MCP server under test, with the default (local) guard."""
    return build_server(services.tools)


# --- the in-memory client session ------------------------------------------------------------


def describe(result: types.CallToolResult) -> str:
    """A tool result as text for assertion messages."""
    return json.dumps(result.model_dump(mode="json", by_alias=True), default=str)[:4000]


class McpSession:
    """One connected client with the listed tools, and checked calls."""

    def __init__(self, client: mcp.Client, tools: dict[str, types.Tool]) -> None:
        self.client = client
        self.tools = tools
        self.instructions = client.instructions

    async def raw(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Call a tool and return the result unchecked (errors included)."""
        return await self.client.call_tool(name, arguments)

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool; assert success and an output-schema-valid structured result."""
        result = await self.raw(name, arguments)
        assert not result.is_error, f"{name} failed: {describe(result)}"
        assert_valid_structured_content(self.tools[name], result)
        assert result.structured_content is not None
        return result.structured_content


def assert_valid_structured_content(tool: types.Tool, result: types.CallToolResult) -> None:
    """REQ-164: structured content present and valid against the published outputSchema."""
    assert result.structured_content is not None, f"{tool.name}: no structuredContent"
    schema = tool.output_schema
    assert schema, f"{tool.name}: no outputSchema"
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(result.structured_content), key=str)
    assert not errors, f"{tool.name}: structuredContent violates outputSchema: {errors[:3]}"


async def _list_all_tools(client: mcp.Client) -> dict[str, types.Tool]:
    tools: dict[str, types.Tool] = {}
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        for tool in page.tools:
            assert tool.name not in tools, f"duplicate tool {tool.name}"
            tools[tool.name] = tool
        cursor = page.next_cursor
        if not cursor:
            return tools


def run_session(server: Any, body: Callable[[McpSession], Awaitable[T]]) -> T:
    """Run `body` inside one in-memory MCP client session against `server`."""

    async def main() -> T:
        async with mcp.Client(server, cache=None) as client:
            return await body(McpSession(client, await _list_all_tools(client)))

    return asyncio.run(main())


# --- workspace inspection -----------------------------------------------------------------------


def record_counts(workspace: Workspace) -> dict[str, int]:
    """Rows per `records` kind plus idempotency claims; audit events are not counted."""
    uri = f"file:{workspace.repository.path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        counts = {
            str(kind): int(count)
            for kind, count in db.execute("SELECT kind, COUNT(*) FROM records GROUP BY kind")
        }
        counts["<requests>"] = int(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
    return counts


def error_payload(result: types.CallToolResult) -> dict[str, Any]:
    """The pinned tool-error body: structuredContent, else the first text block as JSON."""
    if result.structured_content is not None:
        return result.structured_content
    texts = [block.text for block in result.content if isinstance(block, types.TextContent)]
    assert texts, f"tool error carries neither structuredContent nor text: {describe(result)}"
    payload = json.loads(texts[0])
    assert isinstance(payload, dict), payload
    return payload


async def wait_for_job(session: McpSession, job_id: str) -> dict[str, Any]:
    """Poll `get_job` until the job is terminal (bounded wait)."""
    deadline = asyncio.get_running_loop().time() + JOB_WAIT_SECONDS
    while True:
        job = await session.call("get_job", {"job_id": job_id})
        if job["status"] in ("completed", "failed", "canceled", "cancelled"):
            return job
        assert asyncio.get_running_loop().time() < deadline, f"job {job_id} never finished"
        await asyncio.sleep(0.05)


__all__ = [
    "EXAMPLES",
    "MAX_SOLVE_SECONDS",
    "McpSession",
    "Services",
    "build",
    "build_services",
    "describe",
    "error_payload",
    "record_counts",
    "run_session",
    "tiny_profile",
    "tiny_shop",
    "wait_for_job",
]
