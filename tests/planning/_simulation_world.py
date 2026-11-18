"""Shared inputs for the TF-004 plan-source and planning-simulation tests (T-186, T-187).

* `small_shop()` and `erp_profile()`: the TF-005 ERP fixture `tests/erp/fixtures/small_shop.json`
  (25 jobs, 108 operations, calendars, alternates) under the class weights its customers use.
  `erp_profile(SEED_CALIBRATION)` adds the SDD seed-calibration `[simulation]` table (lognormal
  run factor 1.0/0.15, setup factor 1.1/0.3, no breakdowns, baseline `edd`), so the profile
  source is stochastic; the default profile is deterministic.
* `small_shop_rush()`: a rush order for item SPR-001 (standard routing RT-SPR-001).
* `small_shop_hold()`: a hard hold override on job MO-0003 (for `resolve_with_overrides`).
* `scenario_plan(workspace)`: a verified scenario `schedule_plans` record built the way the
  REST flow builds one (copied from `test_planning_service.py`, which T-189 rewrites).
* `MenuFake`: the typed TF-003 fake with a configurable candidate list, menu order and
  recommended candidate, for the D-108 ordering and cap tests.
* `real_portfolio_api()`: TF-003's module, or a skip naming it.
"""

from __future__ import annotations

import base64
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from _fake_portfolio import (
    DELAY_SECONDS,
    FakeCandidate,
    FakeMenuEntry,
    FakePortfolioApi,
    FakePortfolioResult,
)

from twinflow.application.linear_draft import LinearDraft
from twinflow.application.portfolio_port import PortfolioApi, load_portfolio_api
from twinflow.application.workspace import Workspace
from twinflow.erp.build import RushOrder
from twinflow.erp.profile import PlanningProfile, load_planning_profile
from twinflow.erp.schema import ErpSnapshot
from twinflow.scheduling import (
    Assignment,
    HoldOverride,
    ObjectiveKind,
    SolveRequest,
    apply_overrides,
    evaluate,
    solve,
)

ROOT = Path(__file__).resolve().parents[2]
SMALL_SHOP = ROOT / "tests" / "erp" / "fixtures" / "small_shop.json"
CLASS_WEIGHTS = '[erp.class_weights]\n"A-KEY" = 4\n"B-CORE" = 2\n"C-SPOT" = 1\n'
SEED_CALIBRATION = (
    '\n[simulation]\ndistribution_source = "profile"\nbaseline_rule = "edd"\n\n'
    "[[simulation.run_factors]]\n"
    'distribution = { kind = "lognormal", mean = 1.0, cv = 0.15 }\n\n'
    "[[simulation.setup_factors]]\n"
    'distribution = { kind = "lognormal", mean = 1.1, cv = 0.3 }\n'
)
TF003_SKIP = "TF-003 portfolio api not installed yet"


def small_shop() -> ErpSnapshot:
    """The TF-005 ERP small shop snapshot."""
    return ErpSnapshot.model_validate_json(SMALL_SHOP.read_text(encoding="utf-8"))


def erp_profile(simulation_toml: str = "", *, scheduling_toml: str = "") -> PlanningProfile:
    """The small shop's class weights, plus optional top-level and `[simulation]` TOML."""
    return load_planning_profile(scheduling_toml + CLASS_WEIGHTS + simulation_toml)


def small_shop_rush() -> RushOrder:
    """A rush order on item SPR-001 (standard routing RT-SPR-001), due three days out."""
    return RushOrder(
        order_id="RUSH-1",
        item_id="SPR-001",
        quantity=50.0,
        due=small_shop().as_of + dt.timedelta(days=3),
        customer_id="CUST-A01",
    )


def small_shop_hold() -> HoldOverride:
    """A hard hold of job MO-0003."""
    return HoldOverride(
        id="hold-mo3",
        fact_ref="operator",
        reason="customer hold",
        hardness="hard",
        job_id="MO-0003",
    )


def real_portfolio_api() -> PortfolioApi:
    """TF-003's portfolio module, or skip the test."""
    api = load_portfolio_api()
    if api is None:
        pytest.skip(TF003_SKIP)
    return api


# --- scenario plans ------------------------------------------------------------------------------


def _fact(value: Any, unit: str | None = None) -> dict[str, Any]:
    return {"status": "supplied", "value": value, "unit": unit, "source": "operator"}


def scenario_plan(workspace: Workspace) -> str:
    """A verified scenario schedule plan id (one stage, deterministic capsule: cv 0)."""
    stage: dict[str, Any] = {
        "id": "cut",
        "seconds_per_piece": _fact(4.0, "second/piece"),
        "machine_capacity": _fact(1, "machine"),
    }
    draft = LinearDraft.model_validate(
        {
            "product": _fact("finished"),
            "input_part": _fact("blank"),
            "as_of": _fact("2026-10-08T00:00:00Z"),
            "operators": _fact(1, "person"),
            "simple_flow_confirmed": _fact(True),
            "stages": [stage],
            "orders": [
                {
                    "id": "o1",
                    "quantity": _fact(1, "piece"),
                    "release_second": _fact(0.0, "second"),
                    "due_second": _fact(100.0, "second"),
                }
            ],
        }
    )
    scenario = workspace.import_model_draft(workspace.save_model_draft("demo", draft)["id"])
    content = base64.b64encode(b"Order,Part,Qty,Release,Due\nnew-1,finished,2,0,1000\n").decode()
    mapping = {
        "order_id": "Order",
        "product": "Part",
        "quantity": "Qty",
        "release_second": "Release",
        "due_second": "Due",
    }
    preview = workspace.preview_schedule_upload(scenario["id"], "orders.csv", content, mapping)
    snapshot = workspace.commit_schedule_upload(
        scenario["id"], "orders.csv", content, preview["original_file_digest"], mapping
    )
    plan = workspace.solve_schedule_snapshot(snapshot["id"], "baseline", 5)
    assert plan["verified"] is True, plan.get("issues")
    return str(plan["id"])


# --- a TF-003 fake with a chosen candidate list ---------------------------------------------------


def _shifted(assignment: Assignment, seconds: float) -> Assignment:
    return assignment.model_copy(
        update={
            "start": assignment.start + seconds,
            "setup_end": assignment.setup_end + seconds,
            "end": assignment.end + seconds,
        }
    )


class MenuFake(FakePortfolioApi):
    """`solve_portfolio` returns one candidate per `(id, delay steps, verified flag)`.

    Candidate `k` is the baseline schedule shifted by `k * DELAY_SECONDS` (feasible on a shop
    without calendars). `menu` lists candidate ids in menu order; `recommended` names one.
    """

    def __init__(
        self,
        candidates: tuple[tuple[str, int, bool], ...],
        *,
        menu: tuple[str, ...],
        recommended: str | None,
    ) -> None:
        super().__init__()
        self.candidate_spec = candidates
        self.menu_order = menu
        self.recommended_id = recommended

    def solve_portfolio(
        self,
        request: SolveRequest,
        *,
        budget_seconds: float | None = None,
        seed: int = 0,
        pipelines: tuple[str, ...] | None = None,
        k_lookup: object | None = None,
    ) -> FakePortfolioResult:
        self._enter("solve_portfolio", request, budget_seconds)
        base = solve(request)
        compiled = apply_overrides(request)
        candidates = []
        for candidate_id, steps, verified in self.candidate_spec:
            schedule = base.schedule.model_copy(
                update={
                    "assignments": tuple(
                        _shifted(a, steps * DELAY_SECONDS) for a in base.schedule.assignments
                    ),
                    "primary_value": None,
                }
            )
            values = evaluate(compiled, schedule, request.previous_plan)
            candidates.append(
                FakeCandidate(
                    id=candidate_id,
                    pipeline=("baseline", f"delay{steps}"),
                    schedule=schedule,
                    verified=verified,
                    objective_values=dict(values),
                )
            )
        by_id = {c.id: c for c in candidates}
        menu = tuple(
            FakeMenuEntry(
                candidate_id=cid,
                supported=True,
                values={
                    axis: by_id[cid].objective_values[axis]
                    for axis in (ObjectiveKind.MAKESPAN, ObjectiveKind.WEIGHTED_TARDINESS)
                },
            )
            for cid in self.menu_order
        )
        return FakePortfolioResult(
            candidates=tuple(candidates), menu=menu, recommended=self.recommended_id
        )
