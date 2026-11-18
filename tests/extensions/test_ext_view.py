"""T-234: the policy view, the state a `decide(state)` file reads (COMP-154, REQ-202)."""

from __future__ import annotations

import dataclasses
import math

import pytest

from twinflow.engines.step.engine import StepEngine
from twinflow.engines.step.model import StepModel
from twinflow.extensions.view import (
    DispatchState,
    ModelFacts,
    PolicyState,
    dispatch_state,
    policy_state,
)
from twinflow.state.records import Snapshot

PRD_NAMES = (
    "products",
    "stock",
    "days_of_cover",
    "demand_rate",
    "family",
    "margin",
    "max_storage",
    "setup",
    "period",
    "days_left",
    "actions",
)


def _state(model: StepModel, day: int = 10) -> PolicyState:
    engine = StepEngine(model)
    start = engine.start(0)
    snapshot = Snapshot(
        t=float(day), period=day, stock=dict(start.stock), setup={model.line_id: "BLK-M"}
    )
    return policy_state(snapshot, ModelFacts.from_step_model(model), engine.actions(snapshot))


def test_every_prd_name_is_present(press_model: StepModel) -> None:
    state = _state(press_model)
    for name in PRD_NAMES:
        assert hasattr(state, name), name
    assert state.products == press_model.product_ids


def test_values_come_from_the_model_and_the_snapshot(press_model: StepModel) -> None:
    state = _state(press_model, day=10)
    first = press_model.products[0]
    assert state.stock["GRY-S"] == 480.0
    assert state.demand_rate["GRY-S"] == first.demand_rate
    assert state.family["NAT-L"] == "natural"
    assert state.abc["GRY-XL"] == "C"
    assert state.margin["GRY-S"] == pytest.approx(first.price - first.unit_cost)
    assert state.price["GRY-S"] == first.price and state.unit_cost["GRY-S"] == first.unit_cost
    assert state.max_storage["GRY-S"] == 1500 and state.run_rate["GRY-S"] == 450
    assert state.holding_cost["GRY-S"] == pytest.approx(0.01 * 7.2)
    assert state.lost_sale_cost["GRY-S"] == pytest.approx(0.30 * 12.0)
    assert state.changeover["across_family"] == {"cost": 1200.0, "output_lost": 0.5}
    assert state.setup == "BLK-M"  # the product id the one line is set up for (Phase 2 note)
    assert state.actions == (*press_model.product_ids, "idle")
    assert state.features == {}


def test_days_left_and_days_of_cover(press_model: StepModel) -> None:
    state = _state(press_model, day=10)
    assert state.period == 10 and state.horizon == 60 and state.days_left == 50
    for product in state.products:
        assert state.days_of_cover[product] == state.stock[product] / state.demand_rate[product]
    assert state.days_of_cover["GRY-S"] == 8.0


def test_no_demand_means_endless_cover() -> None:
    facts = ModelFacts(products=("A",), demand_rate={"A": 0.0}, horizon=5)
    state = policy_state(Snapshot(t=0.0, period=0, stock={"A": 3.0}), facts, ("A", "idle"))
    assert state.days_of_cover["A"] == math.inf


def test_the_view_is_read_only(press_model: StepModel) -> None:
    state = _state(press_model)
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.period = 3  # type: ignore[misc]
    with pytest.raises(TypeError):
        state.stock["GRY-S"] = 0.0  # type: ignore[index]
    with pytest.raises(TypeError):
        state.changeover["within_family"]["cost"] = 0.0  # type: ignore[index]
    with pytest.raises(AttributeError):
        state.products.append("X")  # type: ignore[attr-defined]


def test_replace_recomputes_cover_and_days_left(press_model: StepModel) -> None:
    state = _state(press_model, day=10)
    seen = state.replace(stock={**state.stock, "GRY-S": 120.0}, period=58)
    assert seen.days_of_cover["GRY-S"] == 2.0 and seen.days_left == 2
    assert state.stock["GRY-S"] == 480.0  # the original is unchanged
    with pytest.raises(ValueError, match="no field"):
        state.replace(colour="red")


def test_features_are_passed_through(press_model: StepModel) -> None:
    engine = StepEngine(press_model)
    start = engine.start(0)
    facts = ModelFacts.from_step_model(press_model)
    state = policy_state(start, facts, engine.actions(start), features={"risk": 0.25})
    assert state.features == {"risk": 0.25}


def test_dispatch_view_lists_the_queue_and_rule_names() -> None:
    queue = [
        {"order_id": "WO-1", "thing": "bracket", "qty": 5, "due_date": None, "priority": 0},
        {"order_id": "WO-2", "thing": "plate", "qty": 2, "due_date": 7200.0, "priority": 1},
    ]
    state = dispatch_state("mill", queue, time=3600.0, current="fifo")
    assert isinstance(state, DispatchState)
    assert state.location == "mill" and state.time == 3600.0 and state.current == "fifo"
    assert state.actions == ("fifo", "edd", "spt", "critical_ratio")
    assert [job["order_id"] for job in state.queue] == ["WO-1", "WO-2"]
    queue[0]["qty"] = 99  # the view holds its own copy
    assert state.queue[0]["qty"] == 5
    with pytest.raises(TypeError):
        state.queue[0]["qty"] = 1  # type: ignore[index]
