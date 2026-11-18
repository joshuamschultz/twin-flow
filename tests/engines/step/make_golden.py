"""Write `golden.json`: the RL repo's numbers that the twinflow step engine must reproduce.

T-229 (TF-006, REQ-196). Run this ONCE, inside the RL repo's own environment, from the
RL repo folder (it needs `factory_mdp`, numpy and pandas, which twinflow does not install):

    cd ~/Projects/rl-mfg-supplychain && uv run python \
        ~/Projects/manufacturing-twin-tf006-step/tests/engines/step/make_golden.py

The pytest tests read only the JSON file; they never import the RL repo. Re-run only when
the RL repo's model changes on purpose, and review the diff of `golden.json`.

What it records (12 SKUs from SKU_TABLE, FactoryProblemDefinition defaults, seed 2024,
50 episodes):
  policies        idle, lowest_cover (default stop_days), fixed_wheel(days_per_slot=1),
                  family_campaign(target_days=8.0), myopic (default)
  extra_policies  the other six rules with their default settings (T-231 checks them too)
For each policy: mean total profit, every episode's total, every action taken (a number
per day: the product's position in SKU_TABLE, 12 for idle), and the first 5 days of
episode 0 (action, demand, stock after, the reward parts). The actions let the engine
tests replay whole episodes without any rule code (T-230); the rule tests then check the
rules choose the same actions (T-231).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from factory_mdp.params import SKU_TABLE, FactoryProblemDefinition, make_params
from factory_mdp.policies import (
    abc_wheel_policy,
    dollar_shortfall_policy,
    family_campaign_policy,
    fixed_wheel_policy,
    idle_policy,
    kanban_policy,
    lowest_cover_policy,
    myopic_policy,
    random_policy,
    s_big_s_policy,
    setup_aware_runout_policy,
)
from factory_mdp.simulate import demand_draws, run_episode
from factory_mdp.state import initial_state

SEED = 2024
EPISODES = 50
TRACE_DAYS = 5
OUT = Path(__file__).with_name("golden.json")


def _record(policy: Any, params: Any, demands: Any) -> dict[str, Any]:
    totals = []
    trace = []
    actions = []
    for episode, episode_demand in enumerate(demands):
        total = 0.0
        taken = []
        for day, (_, action, demand, after, reward) in enumerate(
            run_episode(policy, params, episode_demand)
        ):
            total += reward.total
            taken.append(int(action))
            if episode == 0 and day < TRACE_DAYS:
                trace.append(
                    {
                        "day": day,
                        "action": params.action_name(action),
                        "demand": list(demand),
                        "stock_after": list(after.inv),
                        "setup_after": params.skus[after.setup],
                        "revenue": reward.revenue,
                        "production": reward.production,
                        "changeover": reward.changeover,
                        "holding": reward.holding,
                        "lost_sales": reward.lost_sales,
                        "total": reward.total,
                    }
                )
        totals.append(total)
        actions.append(taken)
    return {
        "mean_profit": sum(totals) / len(totals),
        "episode_totals": totals,
        "actions": actions,
        "first_days": trace,
    }


def main() -> None:
    problem = FactoryProblemDefinition()
    params = make_params(problem, SKU_TABLE)
    demands = demand_draws(params, EPISODES, SEED)
    main_policies = {
        "idle": idle_policy(params),
        "lowest_cover": lowest_cover_policy(params),
        "fixed_wheel": fixed_wheel_policy(params, days_per_slot=1),
        "family_campaign": family_campaign_policy(params, target_days=8.0),
        "myopic": myopic_policy(params),
    }
    extra_policies = {
        "random": random_policy(params),
        "abc_wheel": abc_wheel_policy(params),
        "s_big_s": s_big_s_policy(params),
        "kanban": kanban_policy(params),
        "dollar_shortfall": dollar_shortfall_policy(params),
        "setup_aware_runout": setup_aware_runout_policy(params),
    }
    golden = {
        "about": "Written by make_golden.py from the RL repo (factory_mdp). Do not edit by hand.",
        "seed": SEED,
        "episodes": EPISODES,
        "problem": asdict(problem),
        "sku_table": SKU_TABLE,
        "initial_stock": list(initial_state(params).inv),
        "policies": {n: _record(p, params, demands) for n, p in main_policies.items()},
        "extra_policies": {n: _record(p, params, demands) for n, p in extra_policies.items()},
    }
    OUT.write_text(json.dumps(golden, separators=(",", ":")) + "\n", encoding="utf-8")
    for name, rec in {**golden["policies"], **golden["extra_policies"]}.items():
        print(f"{name:20s} {rec['mean_profit']:14.4f}")


if __name__ == "__main__":
    main()
