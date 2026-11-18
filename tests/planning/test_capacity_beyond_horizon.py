"""Plans flag jobs that finish past the planning window (TF-005 review ruling A, plan side).

Contract pinned here (the implementation follows these tests):

* The stored `problems` record carries `horizon_seconds` (the planning window length,
  `profile.erp.horizon_days` real days in seconds), as `BuildResult.horizon_seconds` does.
* Every stored PLAN record (each portfolio candidate, and a rush plan) gets one soft issue
  per job whose planned end (max assignment `end` over its operations) is past
  `horizon_seconds`. PINNED issue shape (a dict in the plan's `issues`): `code ==
  "capacity_beyond_horizon"`, `severity == "soft"`, `job_id` = the job id, `lineage` = the
  job's entry in the problem's `lineage_index` (JSON form; None for a job not in it, i.e. the
  rush job). Jobs ending inside the window get no such issue.
* The overloaded shop (one machine, more work than the window) plans verified, not UNKNOWN.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _fake_portfolio import FakePortfolioApi, tiny_shop

from twinflow.application.planning import PlanningService
from twinflow.application.workspace import Workspace
from twinflow.erp.build import RushOrder
from twinflow.erp.digest import snapshot_digest
from twinflow.erp.profile import ErpPolicy, PlanningProfile
from twinflow.erp.schema import ErpSnapshot
from twinflow.scheduling import SchedulingProfile

CALLER = "arc"
HORIZON_DAYS = 1
HORIZON_SECONDS = HORIZON_DAYS * 86_400.0
QUANTITY = 3_000.0  # per order: op 10 = 60 + 10 + 2 x 3000 s, op 20 = 3000 s on M1
ORDERS = ("PO-1", "PO-2", "PO-3", "PO-4")


def _overloaded_shop() -> ErpSnapshot:
    """`tiny_shop` on ONE machine (M1) that works 08:00-16:00 UTC on weekdays.

    as_of is Monday 12:00 UTC, so the 1-day window holds Mon 12-16 + Tue 08-12 = 8 h of
    work; four orders need about 10 h. The expansion cap (4 days) holds 32 h.
    """
    data = tiny_shop().model_dump(mode="json")
    template = data["production_orders"][0]
    orders = []
    for order_id in ORDERS:
        ops = [
            op
            | {
                "qty_to_produce": QUANTITY,
                "qty_remaining": QUANTITY,
                "lineage": {"table": "order_ops", "key": [order_id, op["op_id"]]},
            }
            for op in template["operations"]
        ]
        orders.append(
            template
            | {
                "id": order_id,
                "quantity": QUANTITY,
                "operations": ops,
                "lineage": {"table": "orders", "key": [order_id]},
            }
        )
    window = {"start_minute": 480, "end_minute": 960}
    calendar = {
        "id": "day",
        "week": [{"weekday": d, "windows": [window]} for d in range(5)],
        "exceptions": [],
        "lineage": {"table": "calendars", "key": ["day"]},
    }
    machines = [m for m in data["machines"] if m["id"] == "M1"]
    centers = [wc | {"calendar_id": "day"} for wc in data["work_centers"]]
    data |= {
        "production_orders": orders,
        "machines": machines,
        "work_centers": centers,
        "calendars": [calendar],
        "digest": None,
    }
    snapshot = ErpSnapshot.model_validate(data)
    return snapshot.model_copy(update={"digest": snapshot_digest(snapshot)})


def _profile() -> PlanningProfile:
    return PlanningProfile(
        scheduling=SchedulingProfile(time_budget_seconds=10.0),
        erp=ErpPolicy(horizon_days=HORIZON_DAYS),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    ws = Workspace(tmp_path)
    yield ws
    ws.close()


@pytest.fixture
def service(workspace: Workspace) -> Iterator[PlanningService]:
    planning = PlanningService(workspace, portfolio=FakePortfolioApi())
    yield planning
    planning.close()


@pytest.fixture
def problem_id(service: PlanningService) -> str:
    record = service.build_problem(_overloaded_shop(), _profile())
    assert record.status == "ready", record.issues
    assert record.problem_id is not None
    return record.problem_id


def _plan(workspace: Workspace, plan_id: str) -> dict[str, Any]:
    return workspace.repository.get("plans", plan_id)


def _job_ends(plan: dict[str, Any]) -> dict[str, float]:
    ends: dict[str, float] = {}
    for row in plan["assignments"]:
        ends[row["job_id"]] = max(ends.get(row["job_id"], 0.0), row["end"])
    return ends


def _flagged(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found = [i for i in plan["issues"] if i["code"] == "capacity_beyond_horizon"]
    by_job = {i["job_id"]: i for i in found}
    assert len(by_job) == len(found), "one issue per job"
    return by_job


def _assert_late_jobs_flagged(
    plan: dict[str, Any], lineage_index: dict[str, Any], *, some_on_time: bool = True
) -> None:
    ends = _job_ends(plan)
    late = {job_id for job_id, end in ends.items() if end > HORIZON_SECONDS}
    assert late, "the overloaded shop must finish some job past the window"
    if some_on_time:
        assert late != set(ends), "some job must still finish inside the window"
    flagged = _flagged(plan)
    assert set(flagged) == late
    for job_id, found in flagged.items():
        assert found["severity"] == "soft"
        assert found["lineage"] == lineage_index.get(job_id), job_id


def test_problem_record_carries_the_planning_window_length(
    workspace: Workspace, problem_id: str
) -> None:
    record = workspace.repository.get("problems", problem_id)
    assert record["horizon_seconds"] == HORIZON_SECONDS


def test_portfolio_plans_of_an_overloaded_shop_are_verified_not_unknown(
    service: PlanningService, problem_id: str
) -> None:
    summary = service.solve_portfolio(problem_id, request_key="s1", caller_id=CALLER)
    baseline = summary.statuses[0]
    assert baseline.status in {"FEASIBLE", "OPTIMAL"}
    assert baseline.verified is True


def test_portfolio_plans_flag_every_job_that_ends_past_the_window(
    service: PlanningService, workspace: Workspace, problem_id: str
) -> None:
    summary = service.solve_portfolio(problem_id, request_key="s1", caller_id=CALLER)
    lineage_index = workspace.repository.get("problems", problem_id)["lineage_index"]
    assert summary.plan_ids
    for plan_id in summary.plan_ids:
        # The fake's delayed candidate may fail verification on a shift calendar; the flag
        # still follows that plan's own assignments.
        # (the delayed candidate shifts every job, so only the baseline keeps some on time).
        plan = _plan(workspace, plan_id)
        on_time_expected = plan_id.endswith(":baseline")
        _assert_late_jobs_flagged(plan, lineage_index, some_on_time=on_time_expected)


def test_rush_plan_flags_late_jobs_too(
    service: PlanningService, workspace: Workspace, problem_id: str
) -> None:
    summary = service.solve_portfolio(problem_id, request_key="s1", caller_id=CALLER)
    shop = _overloaded_shop()
    rush = RushOrder(
        order_id="RUSH-1",
        item_id="WIDGET",
        quantity=2.0,
        due=shop.as_of + dt.timedelta(hours=6),
        customer_id="CUST-A",
    )
    record = service.insert_rush_order(
        summary.plan_ids[0], rush, request_key="r1", caller_id=CALLER
    )
    plan = _plan(workspace, record.plan_id)
    assert plan["verified"] is True
    lineage_index = workspace.repository.get("problems", problem_id)["lineage_index"]
    _assert_late_jobs_flagged(plan, lineage_index)


def test_a_plan_inside_the_window_has_no_capacity_issue(
    service: PlanningService, workspace: Workspace
) -> None:
    # One order fits easily in the first afternoon.
    shop = _overloaded_shop()
    data = shop.model_dump(mode="json") | {"digest": None}
    data["production_orders"] = data["production_orders"][:1]
    for op in data["production_orders"][0]["operations"]:
        op |= {"qty_to_produce": 10.0, "qty_remaining": 10.0}
    data["production_orders"][0]["quantity"] = 10.0
    small = ErpSnapshot.model_validate(data)
    record = service.build_problem(small, _profile())
    assert record.problem_id is not None
    summary = service.solve_portfolio(record.problem_id, request_key="s2", caller_id=CALLER)
    plan = _plan(workspace, summary.plan_ids[0])
    assert max(_job_ends(plan).values()) <= HORIZON_SECONDS
    assert _flagged(plan) == {}
