"""T-231: the eleven RL-repo rules as pure functions (COMP-151, REQ-204)."""

# ruff: noqa: E501  (the grids below are copied verbatim from the RL repo)

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from twinflow.engines.contract import run_episode
from twinflow.engines.step import rules
from twinflow.engines.step.engine import StepEngine, demand_draws
from twinflow.engines.step.model import StepModel, parse_step_model
from twinflow.state.records import Snapshot

GOLDEN: dict[str, Any] = json.loads(Path(__file__).with_name("golden.json").read_text())
ALL = {**GOLDEN["policies"], **GOLDEN["extra_policies"]}
NEVER = 1e9


def _golden_model() -> StepModel:
    problem = GOLDEN["problem"]
    cost, lost = "changeover_cost_", "output_lost_"
    raw = {
        "domain": "make_to_stock",
        "horizon": problem["horizon"],
        "start_cover_days": problem["start_cover_days"],
        "line": {
            "id": "press",
            "changeover": {
                kind: {"cost": problem[cost + kind[:-7]], "output_lost": problem[lost + kind[:-7]]}
                for kind in ("within_family", "across_family")
            },
        },
        "products": [
            {
                "id": r["sku"],
                "family": r["family"],
                "abc": r["abc"],
                "demand": r["demand"],
                "run_rate": r["run_rate"],
                "price": r["price"],
                "unit_cost": r["unit_cost"],
                "max_storage": r["max_storage"],
            }
            for r in GOLDEN["sku_table"]
        ],  # fmt: skip
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


MODEL = _golden_model()
IDS = MODEL.product_ids
NAMES = (*IDS, "idle")


def _state(period: int = 0, setup: str = "GRY-S", base: float = 100.0, **stock: float) -> Snapshot:
    full = {p: base for p in IDS}
    full.update({k.replace("_", "-"): v for k, v in stock.items()})
    return Snapshot(t=float(period), period=period, stock=full, setup={"press": setup})


def test_rule_names_and_defaults_match_the_rl_repo() -> None:
    assert set(rules.RULE_NAMES) == {
        "idle", "random", "myopic", "lowest_cover", "fixed_wheel", "abc_wheel",
        "family_campaign", "s_big_s", "kanban", "dollar_shortfall", "setup_aware_runout",
    }  # fmt: skip
    assert rules.DEFAULT_SETTINGS["lowest_cover"] == {"stop_days": NEVER}
    assert rules.DEFAULT_SETTINGS["fixed_wheel"] == {"days_per_slot": 1}
    assert rules.DEFAULT_SETTINGS["abc_wheel"] == {"days_per_slot": 1}
    assert rules.DEFAULT_SETTINGS["family_campaign"] == {"target_days": 8.0}
    assert rules.DEFAULT_SETTINGS["s_big_s"] == {"safety_days": 2.0, "eoq_scale": 1.0}
    assert rules.DEFAULT_SETTINGS["kanban"] == {"target_days": 10.0, "trigger_share": 0.5}
    assert rules.DEFAULT_SETTINGS["dollar_shortfall"] == {"target_days": 8.0}
    assert rules.DEFAULT_SETTINGS["setup_aware_runout"] == {
        "cross_penalty_days": 6.0, "within_penalty_days": 1.0, "stop_days": 15.0,
    }  # fmt: skip
    assert rules.DEFAULT_SETTINGS["myopic"] == {"salvage": True}
    assert rules.DEFAULT_SETTINGS["idle"] == {}


def test_grids_match_the_rl_repo() -> None:
    assert rules.GRIDS == {
        "lowest_cover": {"stop_days": [4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0, 40.0, 60.0, NEVER]},
        "fixed_wheel": {"days_per_slot": [1, 2, 3, 4, 5, 6]},
        "abc_wheel": {"days_per_slot": [1, 2, 3, 4, 5, 6]},
        "family_campaign": {"target_days": [6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 25.0, 28.0, 30.0]},
        "s_big_s": {"safety_days": [3.0, 5.0, 7.0, 10.0], "eoq_scale": [0.25, 0.5, 1.0]},
        "kanban": {"target_days": [6.0, 10.0, 14.0, 20.0], "trigger_share": [0.3, 0.5, 0.7]},
        "dollar_shortfall": {"target_days": [3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 24.0, 28.0]},
        "setup_aware_runout": {"cross_penalty_days": [0.5, 1.5, 3.0, 6.0], "within_penalty_days": [0.0, 1.0, 2.0]},
    }  # fmt: skip
    for name, grid in rules.GRIDS.items():
        assert set(grid) <= set(rules.DEFAULT_SETTINGS[name])


@pytest.mark.parametrize("policy", sorted(ALL))
def test_rule_picks_the_rl_repo_actions_and_profit(policy: str) -> None:
    """Run the rule itself on the golden dice: same actions, same profit, to 1e-6."""
    record = ALL[policy]
    engine = StepEngine(MODEL, demand_draws(MODEL, GOLDEN["episodes"], GOLDEN["seed"]))
    decide = rules.make_decider(policy, MODEL)  # one decider for all episodes, in order
    totals, taken = [], []
    for episode in range(GOLDEN["episodes"]):
        moves = run_episode(engine, decide, episode)
        totals.append(sum(m.ledger.total for m in moves))
        taken.append([NAMES.index(m.action) for m in moves if m.action is not None])
    assert taken == record["actions"]
    assert sum(totals) / len(totals) == pytest.approx(record["mean_profit"], abs=1e-6)


def test_unknown_rule_lists_the_valid_names() -> None:
    with pytest.raises(ValueError, match=r"'nope'.*lowest_cover"):
        rules.make_decider("nope", MODEL)
    with pytest.raises(ValueError, match="nope"):
        rules.choose("nope", _state(), MODEL)


def test_unknown_setting_is_a_plain_error() -> None:
    with pytest.raises(ValueError, match=r"stop_dayz.*stop_days"):
        rules.choose("lowest_cover", _state(), MODEL, stop_dayz=3)


def test_rules_are_pure_functions_of_state_and_model() -> None:
    state = _state(period=4, **{"BLK_M": 3.0})
    for name in rules.RULES:
        assert rules.choose(name, state, MODEL) == rules.choose(name, state, MODEL)


def test_lowest_cover_by_hand() -> None:
    state = _state(**{"NAT_XL": 4.0})  # 4 / 4 = 1 day, the lowest
    assert rules.choose("lowest_cover", state, MODEL) == "NAT-XL"
    assert rules.choose("lowest_cover", state, MODEL, stop_days=0.5) == "idle"
    ties = Snapshot(t=0, period=0, stock={p: 0.0 for p in IDS}, setup={"press": "GRY-S"})
    assert rules.choose("lowest_cover", ties, MODEL) == "GRY-S"  # ties go to the first


def test_fixed_wheel_walks_the_families_in_order() -> None:
    order = [rules.choose("fixed_wheel", _state(period=d), MODEL) for d in range(12)]
    assert order == ["GRY-S", "GRY-M", "GRY-L", "GRY-XL", "BLK-S", "BLK-M", "BLK-L", "BLK-XL",
                     "NAT-S", "NAT-M", "NAT-L", "NAT-XL"]  # fmt: skip
    assert rules.choose("fixed_wheel", _state(period=5), MODEL, days_per_slot=5) == "GRY-M"
    assert rules.choose("fixed_wheel", _state(period=12), MODEL) == "GRY-S"


def test_abc_wheel_runs_a_items_every_turn_and_c_items_every_fourth() -> None:
    cycle = [rules.choose("abc_wheel", _state(period=d), MODEL) for d in range(25)]
    assert cycle.count("GRY-S") == 4  # an A item, every turn
    assert cycle.count("GRY-L") == 2  # a B item, every second turn
    assert cycle.count("GRY-XL") == 1  # a C item, every fourth turn
    again = [rules.choose("abc_wheel", _state(period=d), MODEL) for d in range(25, 50)]
    assert again == cycle  # 3 A x 4 + 4 B x 2 + 5 C x 1 = 25 slots, then it repeats


def test_idle_always_idles() -> None:
    assert rules.choose("idle", _state(), MODEL) == "idle"


def test_family_campaign_stays_in_the_family_then_moves() -> None:
    full = {p: 10_000.0 for p in IDS}
    assert (
        rules.choose("family_campaign", Snapshot(0.0, 0, full, {"press": "GRY-S"}), MODEL) == "idle"
    )
    short = dict(full, **{"GRY-M": 5.0, "BLK-S": 1.0})
    state = Snapshot(0.0, 0, short, {"press": "GRY-S"})
    assert (
        rules.choose("family_campaign", state, MODEL) == "GRY-M"
    )  # same family wins over a lower BLK-S


def test_dollar_shortfall_picks_the_biggest_dollar_gap() -> None:
    state = _state(base=5000.0, **{"BLK_XL": 0.0})  # 8 days x 5 = 40 units short at $39.90
    assert rules.choose("dollar_shortfall", state, MODEL, target_days=8.0) == "BLK-XL"


def test_decider_returns_a_valid_action_and_random_is_seeded() -> None:
    a = rules.make_decider("random", MODEL, seed=3)
    b = rules.make_decider("random", MODEL, seed=3)
    state = _state()
    picks = [a(state) for _ in range(30)]
    assert picks == [b(state) for _ in range(30)]
    assert set(picks) <= set(NAMES) and len(set(picks)) > 3
