"""`simulate_plan` for the planning service (COMP-117, REQ-171).

A scenario schedule plan is simulated through today's `Workspace.submit_schedule_plan` job
claim, then optionally waited on. An ERP plan cannot be simulated yet: the result is
`blocked` with `simulation_link_pending` and nothing is written.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

from twinflow.application._planning_models import NotFound, SimulateResult
from twinflow.application.workspace import Workspace
from twinflow.scheduling import ScheduleIssue

MAX_SIMULATION_REPS = 10
MAX_WAIT_SECONDS = 120.0
JOB_POLL_SECONDS = 0.05
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "canceled", "interrupted"})


def check_simulation_settings(reps: int, wait_seconds: float) -> None:
    """Reps 1..10 and a finite wait of 0..120 seconds."""
    if not 1 <= reps <= MAX_SIMULATION_REPS:
        raise ValueError(f"reps must be between 1 and {MAX_SIMULATION_REPS}")
    if not (math.isfinite(wait_seconds) and 0 <= wait_seconds <= MAX_WAIT_SECONDS):
        raise ValueError(f"wait_seconds must be between 0 and {MAX_WAIT_SECONDS:g}")


def simulate(
    workspace: Workspace,
    plan_id: str,
    *,
    job_key: str,
    reps: int,
    seed: int,
    wait_seconds: float,
) -> SimulateResult:
    """Submit (or replay) the simulation job of a scenario plan; block an ERP plan."""
    if _exists(workspace, "plans", plan_id):
        pending = ScheduleIssue(
            code="simulation_link_pending",
            path="$.plan_id",
            message="ERP plans cannot be simulated until the simulation link ships",
        )
        return SimulateResult(plan_id=plan_id, status="blocked", issues=(pending,), job=None)
    if not _exists(workspace, "schedule_plans", plan_id):
        raise NotFound("Unknown plan id")
    job = workspace.submit_schedule_plan(plan_id, reps, seed, job_key)
    job = _wait_for_job(workspace, job, wait_seconds)
    return SimulateResult(plan_id=plan_id, status=str(job["status"]), issues=(), job=job)


def _exists(workspace: Workspace, kind: str, record_id: str) -> bool:
    try:
        workspace.repository.get(kind, record_id)
    except KeyError:
        return False
    return True


def _wait_for_job(
    workspace: Workspace, job: Mapping[str, Any], wait_seconds: float
) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    current = dict(job)
    while current["status"] not in TERMINAL_JOB_STATUSES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(JOB_POLL_SECONDS, remaining))
        current = workspace.repository.get("jobs", str(current["id"]))
    return current
