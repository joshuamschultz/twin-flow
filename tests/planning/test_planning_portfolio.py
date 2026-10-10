"""TF-003 portfolio port contract (T-206, COMP-117, TF-005 SDD External Integrations).

Contract pinned here (the implementation follows these tests):

    from twinflow.application.portfolio_port import PortfolioApi, load_portfolio_api

* `PortfolioApi` is a `typing.Protocol` with the four TF-003 functions, exactly:
  `solve_portfolio(request, *, budget_seconds=None, seed=0, pipelines=None, k_lookup=None)
  -> PortfolioResultLike`; `get_pareto_menu(result, axes=None) -> tuple[MenuEntryLike, ...]`;
  `resolve_with_overrides(request, previous_plan, overrides, *, budget_seconds=None, seed=0)
  -> PortfolioResultLike`; `insert_rush_order(request, previous_plan, rush_job, *,
  budget_seconds=None, seed=0) -> SolveResult`.
* Results are pydantic models (stored with `model_dump(mode="json")`) with the TF-003 field
  names: result `candidates, menu, recommended, issues, elapsed_seconds`; candidate `id,
  pipeline, schedule (TF-002 Schedule), verified, objective_values, issues`; menu entry
  `candidate_id, supported, values`.
* `load_portfolio_api()` imports `twinflow.scheduling.portfolio.api` and returns it, or None
  when the import fails (TF-003 absent).

Every contract test runs against the typed fake and against the real TF-003 module; the real
case is skipped while `load_portfolio_api()` is None, so the same tests gate TF-003 the day
it lands.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from _fake_portfolio import FakePortfolioApi, rush_job_for, tiny_request
from pydantic import BaseModel

from twinflow.application.portfolio_port import load_portfolio_api
from twinflow.scheduling import (
    HoldOverride,
    PreviousAssignment,
    PreviousPlan,
    Schedule,
    SolveResult,
)

PORTFOLIO_MODULE = "twinflow.scheduling.portfolio.api"
FOUR_FUNCTIONS = (
    "solve_portfolio",
    "get_pareto_menu",
    "resolve_with_overrides",
    "insert_rush_order",
)
BUDGET = 5.0


def _real_api() -> Any:
    api = load_portfolio_api()
    if api is None:
        pytest.skip("TF-003 portfolio api not installed yet")
    return api


@pytest.fixture(params=["fake", "real"])
def api(request: pytest.FixtureRequest) -> Any:
    if request.param == "fake":
        return FakePortfolioApi()
    return _real_api()


def _previous_plan(schedule: Schedule) -> PreviousPlan:
    return PreviousPlan(
        plan_id="prev",
        problem_digest="0" * 64,
        assignments=tuple(
            PreviousAssignment(operation_id=a.operation_id, machine_id=a.machine_id, start=a.start)
            for a in schedule.assignments
        ),
    )


# --- load_portfolio_api -------------------------------------------------------------------------


def test_load_portfolio_api_returns_none_when_tf003_module_is_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, PORTFOLIO_MODULE, None)
    monkeypatch.setitem(sys.modules, "twinflow.scheduling.portfolio", None)

    assert load_portfolio_api() is None


def test_load_portfolio_api_when_present_exposes_the_four_functions() -> None:
    api = _real_api()

    for name in FOUR_FUNCTIONS:
        assert callable(getattr(api, name)), name


# --- contract, parametrized over fake and real ---------------------------------------------------


def test_portfolio_solve_portfolio_returns_pydantic_result_with_tf003_fields(api: Any) -> None:
    result = api.solve_portfolio(tiny_request(), budget_seconds=BUDGET, seed=0)

    assert isinstance(result, BaseModel)
    assert result.candidates
    assert isinstance(result.elapsed_seconds, float | int)
    assert isinstance(result.issues, tuple)
    ids = [candidate.id for candidate in result.candidates]
    assert len(set(ids)) == len(ids)
    assert result.recommended is None or result.recommended in ids


def test_portfolio_candidates_carry_tf002_schedules_and_objective_values(api: Any) -> None:
    result = api.solve_portfolio(tiny_request(), budget_seconds=BUDGET, seed=0)

    for candidate in result.candidates:
        assert isinstance(candidate.schedule, Schedule)
        assert isinstance(candidate.verified, bool)
        assert isinstance(candidate.pipeline, tuple)
        assert candidate.pipeline
        assert isinstance(candidate.issues, tuple)
        assert "makespan" in {str(kind) for kind in candidate.objective_values}


def test_portfolio_result_round_trips_through_json_dump(api: Any) -> None:
    result = api.solve_portfolio(tiny_request(), budget_seconds=BUDGET, seed=0)

    dumped = result.model_dump(mode="json")

    assert type(result).model_validate(dumped) == result
    assert set(dumped) >= {"candidates", "menu", "recommended", "issues", "elapsed_seconds"}
    assert set(dumped["candidates"][0]) >= {
        "id",
        "pipeline",
        "schedule",
        "verified",
        "objective_values",
        "issues",
    }


def test_portfolio_menu_entries_name_candidates_of_the_result(api: Any) -> None:
    result = api.solve_portfolio(tiny_request(), budget_seconds=BUDGET, seed=0)

    menu = api.get_pareto_menu(result)

    ids = {candidate.id for candidate in result.candidates}
    assert isinstance(menu, tuple)
    assert menu
    for entry in menu:
        assert entry.candidate_id in ids
        assert isinstance(entry.supported, bool)
        assert entry.values
    assert {entry.candidate_id for entry in result.menu} <= ids


def test_portfolio_menu_with_default_axes_equals_the_stored_menu(api: Any) -> None:
    result = api.solve_portfolio(tiny_request(), budget_seconds=BUDGET, seed=0)

    assert api.get_pareto_menu(result) == result.menu


def test_portfolio_solve_is_deterministic_for_a_fixed_seed(api: Any) -> None:
    request = tiny_request()

    first = api.solve_portfolio(request, budget_seconds=BUDGET, seed=7)
    second = api.solve_portfolio(request, budget_seconds=BUDGET, seed=7)

    assert [c.id for c in first.candidates] == [c.id for c in second.candidates]
    assert [c.schedule.assignments for c in first.candidates] == [
        c.schedule.assignments for c in second.candidates
    ]


def test_portfolio_resolve_with_hold_override_drops_the_held_job(api: Any) -> None:
    request = tiny_request()
    base = api.solve_portfolio(request, budget_seconds=BUDGET, seed=0)
    previous = _previous_plan(base.candidates[0].schedule)
    hold = HoldOverride(
        id="h1", fact_ref="operator", reason="customer hold", hardness="hard", job_id="J2"
    )

    revised = api.resolve_with_overrides(request, previous, (hold,), budget_seconds=BUDGET, seed=0)

    assert revised.candidates
    for candidate in revised.candidates:
        placed = {a.operation_id for a in candidate.schedule.assignments}
        assert "J2:10" not in placed
        assert {"J1:10", "J1:20"} <= placed


def test_portfolio_insert_rush_order_returns_solve_result_with_the_rush_job(api: Any) -> None:
    request = tiny_request()
    base = api.solve_portfolio(request, budget_seconds=BUDGET, seed=0)
    previous = _previous_plan(base.candidates[0].schedule)

    result = api.insert_rush_order(
        request, previous, rush_job_for(request), budget_seconds=BUDGET, seed=0
    )

    assert isinstance(result, SolveResult)
    assert result.verified
    assert "RUSH-1:10" in {a.operation_id for a in result.schedule.assignments}
