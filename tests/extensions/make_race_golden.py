"""Write `race_golden.json`: the RL repo's race and analysis numbers that twinflow must equal.

T-266 (race half) and T-267 (TF-006, REQ-226, REQ-227). Run this ONCE, inside the RL repo's
own environment, from the RL repo folder (it needs `factory_mdp`, numpy and pandas, which
twinflow does not install):

    cd ~/Projects/rl-mfg-supplychain && uv run python \
        ~/Projects/manufacturing-twin-tf006/tests/extensions/make_race_golden.py

The pytest tests read only the JSON file; they never import the RL repo. Re-run only when
the RL repo's model or analysis changes on purpose, and review the diff.

What it records (12 SKUs from SKU_TABLE, FactoryProblemDefinition defaults):

  tuning        `tune_policies(params, 30, seed=777)` for (s,S), family campaign and lowest
                cover: every trial and the best settings (notebook 2's TUNED for these three)
  observation   notebook 2's profit-by-view table: `observation_table(params, TUNED, 150,
                seed=11)`, every rule under every observation level
  notebook 1    on seed 2024 (the golden seed) with 30 episodes and five tuned rules: mean
                cumulative profit per day, its standard deviation (the bands), rank by day,
                first day passed for a few pairs, the reward parts per episode
  horizon       `horizon_table(params, TUNED, [10, 30], 20, seed=7)` (random left out: its
                dice depend on call order)
  markov        `estimate_markov` of 30 tuned family-campaign episodes on seed 5, family and
                full views, labels written the twinflow way
  aliasing      `find_aliased_pair` on 30 tuned (s,S) episodes on seed 5 (bucketed view, a
                small budget so the twinflow test is quick)
  entropy       `entropy_comparison` on the same episodes (family view, small budget)
  inventory     days of cover and low/medium/high of episode 0 of the markov episodes

Labels are written as twinflow writes them: family view `grey:low black:ok natural:high |
grey`, bucketed view `GRY-S:low ... | GRY-S`, full view `GRY-S:480 ... | GRY-S`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from factory_mdp.analysis import (
    entropy_comparison,
    estimate_markov,
    find_aliased_pair,
    first_day_passed,
    horizon_table,
    mean_cumulative_profit,
    observation_table,
    rank_over_time,
    tune_policies,
)
from factory_mdp.observations import (
    HIGH_COVER_DAYS,
    LOW_COVER_DAYS,
    N_LEVELS,
    bucket_levels,
    cover_levels,
)
from factory_mdp.params import SKU_TABLE, FactoryProblemDefinition, make_params
from factory_mdp.policies import build_policies
from factory_mdp.simulate import simulate

OUT = Path(__file__).with_name("race_golden.json")

NAMES = {
    "Idle always": "idle",
    "Random": "random",
    "Myopic (1-day)": "myopic",
    "Lowest cover": "lowest_cover",
    "Fixed wheel": "fixed_wheel",
    "ABC wheel": "abc_wheel",
    "Family campaign": "family_campaign",
    "(s,S) with EOQ lot": "s_big_s",
    "Kanban": "kanban",
    "Biggest $ shortfall": "dollar_shortfall",
    "Changeover-aware run-out": "setup_aware_runout",
}
LEVEL_MASKS = {
    "Full state": "full",
    "No setup/family info": "no_setup",
    "Bucketed stock (L/M/H)": "bucketed",
    "Own SKU stock only": "own_product",
    "No day (end of horizon hidden)": "no_days_left",
    "Bucketed + no day": "bucketed+no_days_left",
}
RACED = ["(s,S) with EOQ lot", "Family campaign", "Lowest cover"]
NOTEBOOK1 = [
    "Idle always",
    "Myopic (1-day)",
    "Lowest cover",
    "Family campaign",
    "(s,S) with EOQ lot",
]
FAMILY_LEVELS = ("low", "ok", "high")
BUCKET_LEVELS = ("low", "medium", "high")


def family_label(index: int, params: Any) -> str:
    """The RL family id as the twinflow `family` view label."""
    n = len(params.family_names)
    on_press, rest = divmod(int(index), N_LEVELS**n)
    parts = [
        f"{params.family_names[f]}:{FAMILY_LEVELS[(rest // N_LEVELS**f) % N_LEVELS]}"
        for f in range(n)
    ]
    return " ".join(parts) + " | " + params.family_names[on_press]


def bucket_label(index: int, params: Any) -> str:
    """The RL bucket id as the twinflow `bucketed` view label."""
    setup, levels = bucket_levels(int(index), params)
    parts = [f"{sku}:{BUCKET_LEVELS[lvl]}" for sku, lvl in zip(params.skus, levels, strict=True)]
    return " ".join(parts) + " | " + params.skus[setup]


def full_label(state_id: str, params: Any) -> str:
    setup, stock = state_id.split("|")
    units = stock.split(",")
    parts = [f"{sku}:{u}" for sku, u in zip(params.skus, units, strict=True)]
    return " ".join(parts) + " | " + params.skus[int(setup)]


def settings_of(raw: dict[str, Any]) -> dict[str, float]:
    return {k: float(v) for k, v in raw.items()}


def chain(df: Any, params: Any, view: str) -> dict[str, Any]:
    result = estimate_markov(df, params, top_k=15, view=view)
    convert = family_label if view == "family" else full_label
    return {
        "labels": [convert(s, params) for s in result.state_ids],
        "matrix": result.matrix.tolist(),
        "coverage": result.coverage,
        "distinct": len(result.frequencies),
    }


def notebook1(params: Any, tuned: dict[str, Any]) -> dict[str, Any]:
    policies = build_policies(params, tuned)
    runs = {name: simulate(policies[name], params, 30, seed=2024) for name in NOTEBOOK1}
    mean_cum = mean_cumulative_profit(runs)
    ranks = rank_over_time(mean_cum)
    std = {
        NAMES[n]: df.pivot(index="day", columns="episode", values="cum_profit").std(axis=1).tolist()
        for n, df in runs.items()
    }
    parts = ["revenue", "production", "changeover", "holding", "lost_sales", "total"]
    pairs = [
        ("Myopic (1-day)", "Family campaign"),
        ("Idle always", "Lowest cover"),
        ("Myopic (1-day)", "(s,S) with EOQ lot"),
        ("Family campaign", "Lowest cover"),
    ]
    return {
        "episodes": 30,
        "seed": 2024,
        "policies": [NAMES[n] for n in NOTEBOOK1],
        "mean_cumulative": {NAMES[n]: mean_cum[n].tolist() for n in NOTEBOOK1},
        "std_cumulative": std,
        "rank_by_day": {NAMES[n]: ranks[n].astype(int).tolist() for n in NOTEBOOK1},
        "first_day_passed": [
            {
                "leader": NAMES[a],
                "challenger": NAMES[b],
                "day": first_day_passed(mean_cum, a, b),
            }
            for a, b in pairs
        ],
        "parts_per_episode": {
            NAMES[n]: {p: float(df[p].sum() / 30) for p in parts} for n, df in runs.items()
        },
    }


def state_json(state: Any, params: Any) -> dict[str, Any]:
    return {
        "day": int(state.day),
        "stock": {sku: int(u) for sku, u in zip(params.skus, state.inv, strict=True)},
        "setup": params.skus[int(state.setup)],
    }


def aliasing(df: Any, params: Any, rule: Any) -> dict[str, Any]:
    budget = {"n_buckets": 3, "per_bucket": 4, "lookahead": 10, "n_draws": 10, "seed": 0}
    pair = find_aliased_pair(df, params, rule, view="bucketed", **budget)
    return {
        "budget": budget,
        "bucket": bucket_label(pair["bucket"], params),
        "states": [state_json(s, params) for s in pair["states"]],
        "best": [params.action_name(int(a)) for a in pair["best"]],
        "values": [
            {params.action_name(a): float(v[a]) for a in range(params.n_actions)}
            for v in pair["values"]
        ],
        "regret": float(pair["regret"]),
    }


def entropy(df: Any, params: Any) -> dict[str, Any]:
    budget = {"min_rows": 30, "max_groups": 10, "states_per_group": 5, "n_draws": 30, "seed": 0}
    table = entropy_comparison(df, params, view="family", **budget)
    rows = [
        {
            "label": family_label(int(r.bucket), params),
            "action": r.action,
            "days": int(r.days),
            "bucketed": float(r["bucketed view"]),
            "full": float(r["full view"]),
        }
        for _, r in table.iterrows()
    ]
    return {"budget": budget, "rows": rows}


def inventory(df: Any, params: Any) -> dict[str, Any]:
    episode = df[df.episode == 0].set_index("day")
    stock = episode[[f"inv_{n}" for n in params.skus]].to_numpy().T
    cover = stock / np.asarray(params.demand_rate)[:, None]
    return {
        "cover": np.minimum(cover, 30).tolist(),
        "levels": cover_levels(cover).tolist(),
        "setups": [params.skus[int(s)] for s in episode["setup_before"]],
    }


def main() -> None:
    assert (LOW_COVER_DAYS, HIGH_COVER_DAYS) == (3.0, 10.0)
    problem = FactoryProblemDefinition()
    params = make_params(problem, SKU_TABLE)
    tuned_all, trials = tune_policies(params, n_episodes=30, seed=777)
    tuned = {name: tuned_all[name] for name in tuned_all}
    obs = observation_table(params, tuned, n_episodes=150, seed=11)
    horizons = horizon_table(params, tuned, [10, 30], n_episodes=20, seed=7)
    policies = build_policies(params, tuned)
    map_df = simulate(policies["Family campaign"], params, 30, seed=5)
    ss_df = simulate(policies["(s,S) with EOQ lot"], params, 30, seed=5)
    golden = {
        "about": "Written by make_race_golden.py from the RL repo (factory_mdp). Do not edit.",
        "tuning": {
            "episodes": 30,
            "seed": 777,
            "trials": [
                {"policy": NAMES[r.policy], "settings": settings_of(r.settings), "profit": r.profit}
                for r in trials.itertuples()
                if r.policy in RACED
            ],
            "best": {NAMES[n]: settings_of(tuned_all[n]) for n in RACED},
            "all_best": {NAMES[n]: settings_of(s) for n, s in tuned_all.items()},
        },
        "observation_table": {
            "episodes": 150,
            "seed": 11,
            "profit": {
                NAMES[rule]: {
                    LEVEL_MASKS[level]: float(obs.loc[level, rule]) for level in obs.index
                }
                for rule in RACED
            },
        },
        "notebook1": notebook1(params, tuned),
        "horizon_table": {
            "episodes": 20,
            "seed": 7,
            "horizons": [10, 30],
            "profit": {
                NAMES[n]: {str(h): float(horizons.loc[n, h]) for h in (10, 30)}
                for n in horizons.index
                if n != "Random"
            },
        },
        "markov": {
            "episodes": 30,
            "seed": 5,
            "policy": "family_campaign",
            "family": chain(map_df, params, "family"),
            "full": chain(map_df, params, "full"),
        },
        "aliasing": {
            "episodes": 30,
            "seed": 5,
            "policy": "s_big_s",
            **aliasing(ss_df, params, policies["(s,S) with EOQ lot"]),
        },
        "entropy": {"episodes": 30, "seed": 5, "policy": "s_big_s", **entropy(ss_df, params)},
        "inventory": inventory(map_df, params),
    }
    text = json.dumps(golden, indent=1, allow_nan=False)
    OUT.write_text(text + "\n", encoding="utf-8")
    for rule, row in golden["observation_table"]["profit"].items():
        print(rule, {k: round(v, 2) for k, v in row.items()})
    print(
        "regret",
        golden["aliasing"]["regret"],
        "nan-free:",
        not math.isnan(golden["aliasing"]["regret"]),
    )


if __name__ == "__main__":
    main()
