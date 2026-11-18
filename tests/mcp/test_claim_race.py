"""Concurrent duplicates of one keyed workspace tool call run once (REQ-167, check 6).

`schedule_operations` and `solve_schedule_snapshot` claim their request key in the MCP
handler. Two calls from one caller with one key and one body, arriving while the first is
still solving, must solve once and both return the same record. The first call is held
inside its solve until the second has had time to reach the claim (forced interleaving).
"""

# ruff: noqa: E402 -- the agent extra is optional.

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from _mcp_helpers import build_services

from twinflow.mcp import _handlers
from twinflow.mcp import models as m
from twinflow.service.auth import Caller

CALLER = Caller(caller_id="arc", scopes=frozenset({"read", "plan"}))

ONE_OP_REQUEST: dict[str, Any] = {
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


class _HeldDecisions:
    """Delegates `schedule` to the real tools, holding the first call until released."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def schedule(self, request: Any, backend: str, time_limit: float) -> dict[str, Any]:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10)
        result: dict[str, Any] = self._real.schedule(request, backend, time_limit)
        return result


def test_concurrent_same_key_same_body_schedule_operations_solves_once(tmp_path: Path) -> None:
    with build_services(tmp_path) as services:
        held = _HeldDecisions(services.tools.decisions)
        tools = replace(services.tools, decisions=held)
        data = m.ScheduleOperationsInput(request=ONE_OP_REQUEST, request_key="dup")
        results: list[m.ScheduleOutput] = []

        def call() -> None:
            results.append(_handlers.schedule_operations(tools, CALLER, data))

        first = threading.Thread(target=call)
        second = threading.Thread(target=call)
        first.start()
        assert held.entered.wait(10)
        second.start()
        second.join(0.3)  # time for an unserialized second call to reach the solve
        held.release.set()
        first.join(10)
        second.join(10)

        assert held.calls == 1
        assert len(results) == 2
        assert results[0].id == results[1].id
        assert len(services.workspace.repository.list("schedules")) == 1
