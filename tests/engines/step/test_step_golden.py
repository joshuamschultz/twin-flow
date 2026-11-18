"""T-229 / T-230: the step engine reproduces the RL repo's numbers (REQ-196, D-135).

Reads only `golden.json` (written by `make_golden.py` in the RL repo's environment). Each
recorded episode is replayed action by action through `StepEngine`; profit and the first
five days must match the RL repo to 1e-6 dollars.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from twinflow.engines.contract import run_episode
from twinflow.engines.step.engine import StepEngine, demand_draws
from twinflow.engines.step.model import StepModel, parse_step_model
from twinflow.state.records import Move

GOLDEN: dict[str, Any] = json.loads(Path(__file__).with_name("golden.json").read_text())
ALL = {**GOLDEN["policies"], **GOLDEN["extra_policies"]}
TOLERANCE = 1e-6


def _model() -> StepModel:
    problem = GOLDEN["problem"]
    raw = {
        "domain": "make_to_stock",
        "period": "day",
        "horizon": problem["horizon"],
        "start_cover_days": problem["start_cover_days"],
        "line": {
            "id": "press",
            "changeover": {
                "within_family": {
                    "cost": problem["changeover_cost_within"],
                    "output_lost": problem["output_lost_within"],
                },
                "across_family": {
                    "cost": problem["changeover_cost_across"],
                    "output_lost": problem["output_lost_across"],
                },
            },
        },
        "products": [
            {
                "id": row["sku"],
                "family": row["family"],
                "abc": row["abc"],
                "demand": row["demand"],
                "run_rate": row["run_rate"],
                "price": row["price"],
                "unit_cost": row["unit_cost"],
                "max_storage": row["max_storage"],
            }
            for row in GOLDEN["sku_table"]
        ],
        "economics": {
            "defaults": {
                "holding_rate": problem["holding_rate"],
                "lost_sale_rate": problem["lost_sale_penalty_rate"],
            }
        },
    }
    model, issues = parse_step_model(raw)
    assert model is not None, issues
    return model


MODEL = _model()
NAMES = (*MODEL.product_ids, "idle")


def _replay(engine: StepEngine, episode: int, codes: list[int]) -> list[Move]:
    """Run one episode, taking the recorded action for each day."""
    day = iter(codes)
    return run_episode(engine, lambda _state: NAMES[next(day)], episode)


@pytest.fixture(scope="module")
def engine() -> StepEngine:
    return StepEngine(MODEL, demand_draws(MODEL, GOLDEN["episodes"], GOLDEN["seed"]))


def test_demand_draws_match_the_rl_repo_dice() -> None:
    draws = demand_draws(MODEL, GOLDEN["episodes"], GOLDEN["seed"])
    assert draws.shape == (GOLDEN["episodes"], MODEL.horizon, len(MODEL.products))
    first = ALL["idle"]["first_days"]
    for day, record in enumerate(first):
        assert list(draws[0, day]) == record["demand"]


def test_initial_stock_is_start_cover_days_of_demand(engine: StepEngine) -> None:
    start = engine.start(0)
    assert [start.stock[p] for p in MODEL.product_ids] == GOLDEN["initial_stock"]
    assert start.setup == {"press": MODEL.product_ids[0]}
    assert start.period == 0


@pytest.mark.parametrize("policy", sorted(ALL))
def test_mean_profit_matches_the_rl_repo(engine: StepEngine, policy: str) -> None:
    record = ALL[policy]
    totals = [
        sum(m.ledger.total for m in _replay(engine, episode, codes))
        for episode, codes in enumerate(record["actions"])
    ]
    assert sum(totals) / len(totals) == pytest.approx(record["mean_profit"], abs=TOLERANCE)
    assert totals == pytest.approx(record["episode_totals"], abs=TOLERANCE)


@pytest.mark.parametrize("policy", sorted(ALL))
def test_first_five_days_match(engine: StepEngine, policy: str) -> None:
    record = ALL[policy]
    moves = _replay(engine, 0, record["actions"][0])
    assert len(moves) == MODEL.horizon
    for move, expected in zip(moves, record["first_days"], strict=False):
        assert move.action == expected["action"]
        assert [move.after.stock[p] for p in MODEL.product_ids] == expected["stock_after"]
        assert move.after.setup == {"press": expected["setup_after"]}
        parts = move.ledger
        assert parts.revenue == pytest.approx(expected["revenue"], abs=TOLERANCE)
        assert parts.production == pytest.approx(expected["production"], abs=TOLERANCE)
        assert parts.setup == pytest.approx(expected["changeover"], abs=TOLERANCE)
        assert parts.holding == pytest.approx(expected["holding"], abs=TOLERANCE)
        assert parts.lost_sales == pytest.approx(expected["lost_sales"], abs=TOLERANCE)
        assert parts.late == 0.0
        assert parts.total == pytest.approx(expected["total"], abs=TOLERANCE)
        demand = [move.events[f"demand.{p}"] for p in MODEL.product_ids]
        assert demand == expected["demand"]


def test_events_balance_each_day(engine: StepEngine) -> None:
    moves = _replay(engine, 3, ALL["family_campaign"]["actions"][3])
    for move in moves:
        for p in MODEL.product_ids:
            ev = move.events
            assert ev[f"sold.{p}"] + ev[f"lost.{p}"] == ev[f"demand.{p}"]
            assert move.after.stock[p] == move.before.stock[p] + ev[f"made.{p}"] - ev[f"sold.{p}"]
