"""Kept design: a semantic refusal is a stored result with `verified=false`, not an error.

`POST /api/workspace/schedules` (201) and MCP `schedule_operations` return the record with
`verified: false` and typed issues for a cycle, an unknown reference or a horizon the plan
cannot meet; only malformed input is refused (422/413). The REST and MCP docs and the
`describe_capabilities` description say so plainly, so a caller never reads 201 or a
non-error tool result as "the plan is good".
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from twinflow.mcp.catalogue import SPECS
from twinflow.service.app import create_app

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PHRASE = "verified=false"
OPTION = {"machine_id": "m", "fixed_seconds": 1, "unit_seconds": 1, "preference_cost": 0}


def _request() -> dict[str, Any]:
    operations = [
        {"id": "a", "work_center_id": "w", "quantity": 1, "options": [dict(OPTION)]},
        {"id": "b", "work_center_id": "w", "quantity": 1, "options": [dict(OPTION)]},
    ]
    return {
        "schema_version": "2.0",
        "problem": {
            "id": "contract",
            "jobs": [{"id": "J", "operations": operations}],
            "machines": [{"id": "m", "work_center_id": "w"}],
            "work_centers": [{"id": "w"}],
        },
    }


def _cycle() -> dict[str, Any]:
    request = _request()
    a, b = request["problem"]["jobs"][0]["operations"]
    a["predecessors"] = [{"operation_id": "b"}]
    b["predecessors"] = [{"operation_id": "a"}]
    return request


def _unknown_reference() -> dict[str, Any]:
    request = copy.deepcopy(_request())
    request["problem"]["jobs"][0]["operations"][0]["options"][0]["machine_id"] = "nowhere"
    return request


@pytest.fixture
def client(tmp_path: Path) -> Any:
    with TestClient(create_app(ROOT / "examples", workspace_root=tmp_path)) as http:
        yield http


@pytest.mark.parametrize(
    ("request_body", "code"), [(_cycle(), "cycle"), (_unknown_reference(), "unknown_reference")]
)
def test_semantic_refusal_is_201_with_verified_false_and_typed_issue(
    client: Any, request_body: dict[str, Any], code: str
) -> None:
    response = client.post(
        "/api/workspace/schedules",
        json={"request": request_body, "backend": "baseline", "time_limit": 5},
    )
    assert response.status_code == 201
    record = response.json()
    assert record["verified"] is False
    assert code in {issue["code"] for issue in record["result"]["issues"]}


def test_malformed_request_is_422(client: Any) -> None:
    response = client.post(
        "/api/workspace/schedules",
        json={"request": {"schema_version": "1.0"}, "backend": "baseline", "time_limit": 5},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "tool", ["schedule_operations", "solve_schedule_snapshot", "describe_capabilities"]
)
def test_tool_descriptions_state_the_verified_false_contract(tool: str) -> None:
    description = SPECS[tool].description
    assert CONTRACT_PHRASE in description
    assert "malformed" in description


def test_agent_guide_states_the_contract_for_rest_and_mcp() -> None:
    guide = (ROOT / "docs" / "enterprise" / "AGENT-GUIDE.md").read_text()
    assert guide.count("201 does not mean the plan is good") >= 2
