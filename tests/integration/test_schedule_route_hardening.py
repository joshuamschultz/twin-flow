"""Hostile bodies on `POST /api/workspace/schedules` get typed refusals, never a 500."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from twinflow.service.app import create_app

ROOT = Path(__file__).resolve().parents[2]
ROUTE = "/api/workspace/schedules"


def _valid_request() -> dict[str, Any]:
    return json.loads((ROOT / "examples" / "scheduling" / "problem.json").read_text())


def _client(tmp_path: Path) -> TestClient:
    app = create_app(ROOT / "examples", workspace_root=tmp_path)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("backend", ["baseline", "ortools"])
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_time_limit_is_a_typed_422(tmp_path: Path, backend: str, literal: str) -> None:
    body = f'{{"request": {json.dumps(_valid_request())}, "backend": "{backend}", '
    body += f'"time_limit": {literal}}}'
    with _client(tmp_path) as client:
        response = client.post(ROUTE, content=body, headers={"content-type": "application/json"})

    assert response.status_code == 422, response.text
    assert "Traceback" not in response.text
    for item in response.json()["detail"]:
        assert "input" not in item
        assert "ctx" not in item


def test_deeply_nested_request_is_a_typed_422(tmp_path: Path) -> None:
    depth = 5000
    body = '{"request": ' + "[" * depth + "]" * depth + "}"
    with _client(tmp_path) as client:
        response = client.post(ROUTE, content=body, headers={"content-type": "application/json"})

    assert response.status_code == 422, response.text
    assert "Traceback" not in response.text


def test_deeply_nested_dict_request_is_a_typed_422(tmp_path: Path) -> None:
    depth = 5000
    body = '{"request": ' + '{"a": ' * depth + "1" + "}" * depth + "}"
    with _client(tmp_path) as client:
        response = client.post(ROUTE, content=body, headers={"content-type": "application/json"})

    assert response.status_code == 422, response.text
    assert "Traceback" not in response.text
