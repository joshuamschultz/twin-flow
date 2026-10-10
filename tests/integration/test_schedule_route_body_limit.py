"""Hostile bodies on `POST /api/workspace/schedules` get typed refusals, never a 500."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from twinflow.service import middleware
from twinflow.service.app import create_app

ROOT = Path(__file__).resolve().parents[2]
ROUTE = "/api/workspace/schedules"


def _valid_request() -> dict[str, Any]:
    return json.loads((ROOT / "examples" / "scheduling" / "problem.json").read_text())


def _client(tmp_path: Path) -> TestClient:
    app = create_app(ROOT / "examples", workspace_root=tmp_path)
    return TestClient(app, raise_server_exceptions=False)


def test_oversized_schedule_body_is_a_typed_413(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(middleware, "SCHEDULE_BODY_LIMIT_BYTES", 1024)
    padding = "x" * 4096
    with _client(tmp_path) as client:
        response = client.post(ROUTE, json={"request": {"pad": padding}})

    assert response.status_code == 413, response.text
    assert response.json()["detail"] == "Request body too large"


def test_schedule_body_limit_admits_the_req_084_maximum() -> None:
    assert middleware.SCHEDULE_BODY_LIMIT_BYTES == 64 * 1024 * 1024
