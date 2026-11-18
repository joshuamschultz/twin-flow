"""COMP-151: the eleven RL-repo decision rules as pure functions (REQ-204, D-142).

Each rule is `rule(state, model, **settings) -> action id`: it reads a `Snapshot` and the
`StepModel` and returns a product id or `"idle"`. They port `factory_mdp.policies` with the
same default settings (the keyword defaults below) and the same tuning grids (`GRIDS`).
This module is plain Python: the domain adapter uses it and it never loads user code (D-139).

`random` is the one rule that is not a pure function: like the RL repo it draws from its own
seeded stream, so `make_decider("random", model, seed=...)` builds a decider that owns it.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

import numpy as np

from twinflow.engines.contract import Decide
from twinflow.engines.step.engine import IDLE, changeover_between, units_made
from twinflow.engines.step.model import StepModel
from twinflow.state.records import Snapshot

NEVER = 1e9  # "no limit" for stop rules
Rule = Callable[..., str]


# --- reading the state -------------------------------------------------------------------------


def _inventory(state: Snapshot, model: StepModel) -> list[int]:
    return [round(state.stock[p]) for p in model.product_ids]


def _setup_index(state: Snapshot, model: StepModel) -> int:
    return model.product_ids.index(str(state.setup[model.line_id]))


def _action(model: StepModel, index: int) -> str:
    return IDLE if index == len(model.products) else model.product_ids[index]


def _cover_days(inv: list[int], model: StepModel) -> list[float]:
    """Days until each product runs out at average demand."""
    return [i / p.demand_rate for i, p in zip(inv, model.products, strict=True)]


def _lowest_cover_index(cover: list[float]) -> int:
    return min(range(len(cover)), key=lambda k: (cover[k], k))


def _wheel_order(model: StepModel) -> list[int]:
    """Products grouped by family (in first-seen family order), so wheel steps are cheap."""
    first = list(dict.fromkeys(model.families))
    return sorted(range(len(model.products)), key=lambda k: (first.index(model.families[k]), k))


def _stock_targets(model: StepModel, days: float) -> list[int]:
    return [min(p.max_storage, math.ceil(days * p.demand_rate)) for p in model.products]


def _capped_levels(levels: list[int], state: Snapshot, model: StepModel) -> list[int]:
    """Wind down near the end: never stock more than the days left will sell."""
    days_left = max(0, model.horizon - state.period)
    return [
        min(lvl, math.ceil(days_left * p.demand_rate))
        for lvl, p in zip(levels, model.products, strict=True)
    ]


# --- the rules ---------------------------------------------------------------------------------


def idle(state: Snapshot, model: StepModel) -> str:
    """Never run the line."""
    return IDLE


def lowest_cover(state: Snapshot, model: StepModel, *, stop_days: float = NEVER) -> str:
    """Run the product that runs out first. Idle if all have at least `stop_days` of stock."""
    cover = _cover_days(_inventory(state, model), model)
    pick = _lowest_cover_index(cover)
    return _action(model, pick if cover[pick] < stop_days else len(cover))


def fixed_wheel(state: Snapshot, model: StepModel, *, days_per_slot: int = 1) -> str:
    """Run every product in the same order, a set number of days each."""
    order = _wheel_order(model)
    return model.product_ids[order[(state.period // days_per_slot) % len(order)]]


def _abc_sequence(model: StepModel) -> list[int]:
    """Four wheel turns: A items every turn, B items every 2nd turn, C items every 4th."""
    every = {"A": 1, "B": 2, "C": 4}
    return [
        k
        for turn in range(4)
        for k in _wheel_order(model)
        if turn % every[model.products[k].abc] == 0
    ]


def abc_wheel(state: Snapshot, model: StepModel, *, days_per_slot: int = 1) -> str:
    """Fast sellers come round often, slow sellers rarely."""
    sequence = _abc_sequence(model)
    return model.product_ids[sequence[(state.period // days_per_slot) % len(sequence)]]


def family_campaign(state: Snapshot, model: StepModel, *, target_days: float = 8.0) -> str:
    """Stay in the current family until all its products are covered, then move to the
    family of the product that runs out first."""
    inv = _inventory(state, model)
    levels = _capped_levels(_stock_targets(model, target_days), state, model)
    short = [k for k in range(len(inv)) if inv[k] < levels[k]]
    if not short:
        return IDLE
    cover = _cover_days(inv, model)
    family = model.families[_setup_index(state, model)]
    pool = [k for k in short if model.families[k] == family] or short
    return _action(model, min(pool, key=lambda k: (cover[k], k)))


def _eoq_units(model: StepModel, k: int) -> float:
    """The lot that balances changeover cost against holding cost."""
    changeover = (model.within.cost + model.across.cost) / 2
    holding = model.holding_cost(model.product_ids[k])
    return math.sqrt(2 * model.products[k].demand_rate * changeover / holding)


def s_big_s(
    state: Snapshot, model: StepModel, *, safety_days: float = 2.0, eoq_scale: float = 1.0
) -> str:
    """(s, S) with an EOQ lot: reorder at `safety_days` of demand, order up to s plus one
    EOQ run, and keep running a product until it is back near S."""
    n = len(model.products)
    rate = [p.demand_rate for p in model.products]
    s_level = [math.ceil(safety_days * d) for d in rate]
    lot = [math.ceil(eoq_scale * _eoq_units(model, k)) for k in range(n)]
    big_s = [min(model.products[k].max_storage, s_level[k] + lot[k]) for k in range(n)]
    inv = _inventory(state, model)
    s_now = _capped_levels(s_level, state, model)
    big_s_now = _capped_levels(big_s, state, model)
    current = _setup_index(state, model)
    noise = math.ceil(2 * math.sqrt(rate[current]))
    near_full = big_s_now[current] - math.ceil(rate[current]) - noise
    if inv[current] < near_full and inv[current] < big_s_now[current]:
        return _action(model, current)
    cover = _cover_days(inv, model)
    needy = [k for k in range(n) if inv[k] < s_now[k]]
    return _action(model, min(needy, key=lambda k: (cover[k], k)) if needy else n)


def kanban(
    state: Snapshot, model: StepModel, *, target_days: float = 10.0, trigger_share: float = 0.5
) -> str:
    """Each product has a target stock; under a share of it, its card is released. Run the
    released product that is closest to running out."""
    inv = _inventory(state, model)
    levels = _capped_levels(_stock_targets(model, target_days), state, model)
    cover = _cover_days(inv, model)
    released = [k for k in range(len(inv)) if inv[k] < trigger_share * levels[k]]
    pick = min(released, key=lambda k: (cover[k], k)) if released else len(inv)
    return _action(model, pick)


def dollar_shortfall(state: Snapshot, model: StepModel, *, target_days: float = 8.0) -> str:
    """Run the product whose gap to target is worth the most at sale price."""
    inv = _inventory(state, model)
    levels = _capped_levels(_stock_targets(model, target_days), state, model)
    gap = [max(0, lvl - i) * p.price for lvl, i, p in zip(levels, inv, model.products, strict=True)]
    worst = max(range(len(inv)), key=lambda k: (gap[k], -k))
    return _action(model, worst if gap[worst] > 0 else len(inv))


def setup_aware_runout(
    state: Snapshot,
    model: StepModel,
    *,
    cross_penalty_days: float = 6.0,
    within_penalty_days: float = 1.0,
    stop_days: float = 15.0,
) -> str:
    """Run-out rule that dislikes changeovers: each product's days of cover gets a penalty
    when it needs a within-family or a cross-family changeover."""
    cover = _cover_days(_inventory(state, model), model)
    if min(cover) >= stop_days:
        return IDLE
    setup = _setup_index(state, model)

    def penalty(k: int) -> float:
        if k == setup:
            return 0.0
        same = model.families[k] == model.families[setup]
        return within_penalty_days if same else cross_penalty_days

    return _action(model, min(range(len(cover)), key=lambda k: (cover[k] + penalty(k), k)))


@lru_cache(maxsize=16)
def _expected_sold(
    rates: tuple[float, ...], caps: tuple[int, ...]
) -> tuple[tuple[float, ...], ...]:
    """table[i][s] = expected units of product i sold today when s are on hand."""
    table = []
    for rate, cap in zip(rates, caps, strict=True):
        ks = np.arange(0, int(rate + 10 * math.sqrt(rate) + 30))
        log_pmf = ks * math.log(rate) - rate - np.array([math.lgamma(k + 1) for k in ks])
        pmf = np.exp(log_pmf)
        table.append(tuple(float(np.minimum(s, ks) @ pmf) for s in range(cap + 1)))
    return tuple(table)


def _one_day_value(
    model: StepModel, inv: list[int], setup: int, action: int, salvage: bool
) -> float:
    """Expected money from one action today (average demand over the dice)."""
    table = _expected_sold(model.demand_rates, tuple(p.max_storage for p in model.products))
    made = units_made(model, inv, setup, action)
    stock = [i + m for i, m in zip(inv, made, strict=True)]
    sold = [table[k][min(s, len(table[k]) - 1)] for k, s in enumerate(stock)]
    products = model.products
    revenue = sum(x * p.price for x, p in zip(sold, products, strict=True))
    production = sum(m * p.unit_cost for m, p in zip(made, products, strict=True))
    switch = action != len(products) and action != setup
    changeover = changeover_between(model, setup, action).cost if switch else 0.0
    left_over = [s - x for s, x in zip(stock, sold, strict=True)]
    holding = sum(model.holding_cost(p.id) * x for p, x in zip(products, left_over, strict=True))
    lost = sum(
        model.lost_sale_cost(p.id) * (p.demand_rate - x)
        for p, x in zip(products, sold, strict=True)
    )
    stock_value = (
        sum(x * p.unit_cost for x, p in zip(left_over, products, strict=True)) if salvage else 0.0
    )
    return revenue - production - changeover - holding - lost + stock_value


def myopic(state: Snapshot, model: StepModel, *, salvage: bool = True) -> str:
    """Pick the action with the best expected money for today only."""
    inv = _inventory(state, model)
    setup = _setup_index(state, model)
    n = len(model.products)
    order = [n, *range(n)]
    best = max(order, key=lambda a: _one_day_value(model, inv, setup, a, salvage))
    return _action(model, best)


# --- the table of rules ------------------------------------------------------------------------

RULES: Mapping[str, Rule] = {
    "idle": idle,
    "myopic": myopic,
    "lowest_cover": lowest_cover,
    "fixed_wheel": fixed_wheel,
    "abc_wheel": abc_wheel,
    "family_campaign": family_campaign,
    "s_big_s": s_big_s,
    "kanban": kanban,
    "dollar_shortfall": dollar_shortfall,
    "setup_aware_runout": setup_aware_runout,
}
RULE_NAMES: tuple[str, ...] = ("idle", "random", *list(RULES)[1:])


def _defaults(rule: Rule) -> dict[str, Any]:
    params = inspect.signature(rule).parameters.values()
    return {p.name: p.default for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY}


DEFAULT_SETTINGS: Mapping[str, Mapping[str, Any]] = {
    **{name: _defaults(rule) for name, rule in RULES.items()},
    "random": {"seed": 0},
}

# Tuning budget: at most 12 trial settings per rule (RL repo `POLICY_GRIDS`).
GRIDS: Mapping[str, Mapping[str, list[float]]] = {
    "lowest_cover": {
        "stop_days": [4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0, 40.0, 60.0, NEVER]
    },
    "fixed_wheel": {"days_per_slot": [1, 2, 3, 4, 5, 6]},
    "abc_wheel": {"days_per_slot": [1, 2, 3, 4, 5, 6]},
    "family_campaign": {
        "target_days": [6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 25.0, 28.0, 30.0]
    },
    "s_big_s": {"safety_days": [3.0, 5.0, 7.0, 10.0], "eoq_scale": [0.25, 0.5, 1.0]},
    "kanban": {"target_days": [6.0, 10.0, 14.0, 20.0], "trigger_share": [0.3, 0.5, 0.7]},
    "dollar_shortfall": {
        "target_days": [3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 24.0, 28.0]
    },
    "setup_aware_runout": {
        "cross_penalty_days": [0.5, 1.5, 3.0, 6.0],
        "within_penalty_days": [0.0, 1.0, 2.0],
    },
}


def _check_settings(name: str, settings: Mapping[str, Any]) -> None:
    allowed = DEFAULT_SETTINGS[name]
    for key in settings:
        if key not in allowed:
            valid = ", ".join(allowed) or "no settings"
            raise ValueError(f"rule {name!r} has no setting {key!r}; it has: {valid}")


def _known(name: str) -> None:
    if name not in RULE_NAMES:
        raise ValueError(f"unknown rule {name!r}; choose one of {', '.join(RULE_NAMES)}")


def choose(name: str, state: Snapshot, model: StepModel, **settings: Any) -> str:
    """Apply one pure rule by name to a state."""
    _known(name)
    if name == "random":
        raise ValueError("random is not a pure rule; use make_decider('random', model, seed=...)")
    _check_settings(name, settings)
    return RULES[name](state, model, **settings)


def make_decider(name: str, model: StepModel, **settings: Any) -> Decide:
    """A `Decide` (Snapshot -> action id) for the engine. `random` owns its seeded stream."""
    _known(name)
    _check_settings(name, settings)
    if name == "random":
        rng = np.random.default_rng(int(settings.get("seed", 0)))
        actions = (*model.product_ids, IDLE)
        return lambda state: actions[int(rng.integers(len(actions)))]
    rule = RULES[name]
    return lambda state: rule(state, model, **settings)
