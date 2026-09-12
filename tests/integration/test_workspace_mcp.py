"""MCP uses the same HTTP contract as the operator workspace."""

import asyncio
import json
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
                result = await client.call_tool("load_example", {"name": "capsule-office"})
                assert not result.is_error
                scenario = http.get("/api/workspace/scenarios").json()[0]
                result = await client.call_tool(
                    "validate_scenario", {"content": json.dumps(scenario["capsule"])}
                )
                assert not result.is_error and "true" in str(result).lower()
                result = await client.call_tool(
                    "evaluate",
                    {
                        "scenario_id": scenario["id"],
                        "request_key": "mcp-office",
                        "reps": 1,
                        "seed": 42,
                    },
                )
                assert not result.is_error
                for _ in range(200):
                    job = http.get("/api/workspace/jobs").json()[0]
                    if job["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)
                assert job["status"] == "completed"
                result = await client.call_tool(
                    "query_evidence", {"job_id": job["id"], "topic": "dates"}
                )
                assert not result.is_error and "quote-001" in str(result)
                result = await client.call_tool(
                    "save_building_brief",
                    {"name": "Agent intake", "facts": {"process": "review request"}},
                )
                assert not result.is_error
                draft = http.get("/api/workspace/drafts").json()[0]
                result = await client.call_tool(
                    "answer_building_brief",
                    {
                        "draft_id": draft["id"],
                        "answers": [{"id": "resources", "answer": "one reviewer"}],
                    },
                )
                assert not result.is_error
                result = await client.call_tool(
                    "propose_action", {"job_id": job["id"], "action": {"type": "review"}}
                )
                assert not result.is_error

        asyncio.run(exercise())
