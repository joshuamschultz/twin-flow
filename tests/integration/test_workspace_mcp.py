"""The MCP tools run the operator workspace use cases in process (no REST hop, D-116)."""

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from twinflow.application.decision_tools import DecisionTools
from twinflow.application.planning import PlanningService
from twinflow.application.workspace import Workspace
from twinflow.sdk import TwinflowClient
from twinflow.service.app import create_app

ROOT = Path(__file__).resolve().parents[2]
ONE_OP_REQUEST = {
    "schema_version": "2.0",
    "problem": {
        "id": "one-op",
        "jobs": [
            {
                "id": "J",
                "operations": [
                    {
                        "id": "cut",
                        "work_center_id": "machining",
                        "quantity": 2,
                        "options": [
                            {
                                "machine_id": "m1",
                                "fixed_seconds": 1.0,
                                "unit_seconds": 2.0,
                                "preference_cost": 0.0,
                            }
                        ],
                    }
                ],
            }
        ],
        "machines": [{"id": "m1", "work_center_id": "machining"}],
        "work_centers": [{"id": "machining"}],
    },
}


def _local_client(http: TestClient) -> TwinflowClient:
    """The real SDK, with its HTTP transport replaced by the in-process test client."""

    class LocalClient(TwinflowClient):
        def request(self, path, body=None):
            response = (
                http.get("/api/workspace" + path)
                if body is None
                else http.post("/api/workspace" + path, json=body)
            )
            response.raise_for_status()
            return response.json()

    return LocalClient()


@contextmanager
def _tools(root: Path) -> Iterator[tuple[Any, Workspace]]:
    from twinflow.mcp.spec import ToolServices
    from twinflow.mcp.tools import build_server

    workspace = Workspace(root, ROOT / "examples")
    planning = PlanningService(workspace, portfolio=None)
    services = ToolServices(
        workspace=workspace,
        decisions=DecisionTools(workspace),
        planning=planning,
        artifacts=None,
        models_root=None,
        local_mode=True,
    )
    try:
        yield build_server(services), workspace
    finally:
        planning.close()
        workspace.close()


def test_mcp_discovery_and_real_scenario_import(tmp_path: Path) -> None:
    mcp = pytest.importorskip("mcp")

    async def exercise(server: Any, workspace: Workspace) -> None:
        async with mcp.Client(server) as client:
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
            scenario = workspace.repository.list("scenarios")[0]
            result = await client.call_tool(
                "validate_scenario", {"content": json.dumps(scenario["capsule"])}
            )
            assert not result.is_error and "true" in str(result).lower()
            result = await client.call_tool(
                "evaluate",
                {"scenario_id": scenario["id"], "request_key": "mcp-office", "reps": 1, "seed": 42},
            )
            assert not result.is_error
            for _ in range(500):
                job = workspace.repository.list("jobs")[0]
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
            draft = workspace.repository.list("drafts")[0]
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

    with _tools(tmp_path) as (server, workspace):
        asyncio.run(exercise(server, workspace))


def test_sdk_schedule_sends_a_v2_request(tmp_path: Path) -> None:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as http:
        client = _local_client(http)

        record = client.schedule(request=ONE_OP_REQUEST, backend="ortools", time_limit=5)

        assert record["schema_version"] == "2.0"
        assert record["request"] == ONE_OP_REQUEST
        assert record["verified"] is True
        assert record["objective_values"]["makespan"] == 5.0
        assert http.get("/api/workspace/schedules").json()[0]["id"] == record["id"]


def test_mcp_schedule_operations_takes_a_v2_request(tmp_path: Path) -> None:
    mcp = pytest.importorskip("mcp")

    async def exercise(server: Any) -> None:
        async with mcp.Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            arguments = tools["schedule_operations"].input_schema["properties"]
            assert "request" in arguments and "problem" not in arguments
            assert "linear" not in (tools["solve_schedule_snapshot"].description or "")
            result = await client.call_tool(
                "schedule_operations",
                {
                    "request": ONE_OP_REQUEST,
                    "backend": "baseline",
                    "time_limit": 5,
                    "request_key": "one-op",
                },
            )
            assert not result.is_error, result

    with _tools(tmp_path) as (server, workspace):
        asyncio.run(exercise(server))
        stored = workspace.repository.list("schedules")
    assert [row["schema_version"] for row in stored] == ["2.0"]
    assert stored[0]["verified"] is True
