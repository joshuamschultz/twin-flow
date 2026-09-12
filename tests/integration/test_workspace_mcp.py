"""MCP uses the same HTTP contract as the operator workspace."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from twinflow.mcp.server import create_server
from twinflow.sdk import TwinflowClient
from twinflow.service.app import create_app

ROOT = Path(__file__).resolve().parents[2]


def test_mcp_discovery_and_real_scenario_import(tmp_path: Path) -> None:
    mcp = pytest.importorskip("mcp")

    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as http:

        class LocalClient(TwinflowClient):
            def request(self, path, body=None):
                response = (
                    http.get("/api/workspace" + path)
                    if body is None
                    else http.post("/api/workspace" + path, json=body)
                )
                response.raise_for_status()
                return response.json()

        async def exercise():
            async with mcp.Client(create_server(LocalClient())) as client:
                tools = await client.list_tools()
                assert "evaluate" in {tool.name for tool in tools.tools}
                result = await client.call_tool("load_example", {"name": "spring"})
                assert not result.is_error
                result = await client.call_tool("list_scenarios", {})
                assert not result.is_error
                assert "Spring" in str(result)
                result = await client.call_tool("describe_capabilities", {})
                assert "production_writeback" in str(result)

        asyncio.run(exercise())
