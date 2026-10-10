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


def test_unexpected_solver_failure_does_not_leak_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from twinflow.application.decision_tools import DecisionTools

    def boom(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise TypeError("internal detail sorted_interval_list.Domain")

    monkeypatch.setattr(DecisionTools, "schedule", boom)
    with _client(tmp_path) as client:
        response = client.post(ROUTE, json={"request": _valid_request()})

    assert response.status_code == 422, response.text
    assert "internal detail" not in response.text
    assert "Domain" not in response.text
