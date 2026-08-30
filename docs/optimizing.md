# Optimizing — making decisions with the twin

Running the twin tells you what happens. Optimizing tells you **what to do**. This guide
goes from "try a few options side by side" to "let the computer search for the best
setting", with worked examples. It assumes you can already run a twin
([running.md](running.md)).

The stance throughout: **the twin scores the options; you decide.** An optimizer proposes
a setting with a confidence range attached; it never commits a change to your real floor.

## Levers: the things you can change

A lever is one dot-path into your `model.yaml`, the same path the sweep and optimize tools
use:

| Lever | Example path |
|---|---|
| Operators in a pool | `labor.pools[0].headcount` |
| Machines at a center | `locations[mill].capacity` |
| A reorder point | `stocks[resin].reorder_point` |
| A dispatch rule | `locations[press].dispatch` |
| A WIP cap | `release.wip_cap` |

`[0]` selects a list item by index; `[mill]` selects the list item whose `name` is `mill`.

## Level 1 — what-if sweeps (try every option, side by side)

A sweep runs a grid of options and reports them all, fairly paired with common random
numbers. It picks **no winner** — you read the trade-off.

Write a small JSON grid (`sweep.json`):

```json
{ "labor.pools[0].headcount": [2, 3, 4] }
```

Run it:

```bash
twinflow balance model.yaml --plan plan.csv --sweep sweep.json --reps 30
```

You get each option's KPIs (with bands) next to each other. Sweep more than one lever by
adding keys — the tool runs the full grid (every combination). Sweeps are also the only
way today to compare a **categorical** lever like a dispatch rule:

```json
{ "locations[press].dispatch": ["fifo", "edd", "spt", "critical_ratio"] }
```

Use a sweep when the grid is small and you want to *see* the whole trade space.

## Level 2 — optimize (let the twin search)

When the grid is large, search it instead of enumerating it. Every optimization has three
parts: a **space** (which levers, over what range), an **objective** (what "better"
means), and an **optimizer** (how to search).

### Objectives — what "better" means

Pick one with `--objective` (CLI) or `OBJECTIVES.create(name)` (Python):

| Objective | Direction | Rewards |
|---|---|---|
| `on_time_pct` | maximize | average on-time delivery |
| `robust_on_time` | maximize | the **low edge** of the on-time band — reliably good, not luckily good |
| `makespan` | minimize | finishing everything sooner |
| `mean_lateness` | minimize | less average tardiness |
| `utilization` | maximize | busier machines |
| `service_level` | maximize | inventory in stock (less time a stock sits empty) |

Prefer `robust_on_time` when you must *promise* a date: it optimizes the number you can
defend, not the average you might miss.

### Cost functions — putting a price on it

Cost functions turn a run into money (every rate is an assumption you supply):

| Cost | Formula |
|---|---|
| `labor_cost` | headcount x wage/hr x makespan hours |
| `capacity_cost` | installed machines x cost per machine |
| `lateness_penalty` | tardy hours x penalty/hr |
| `inventory_holding_cost` | average inventory x rate/unit/hr x hours |
| `stockout_penalty` | stockout hours x penalty/hr |
| `total_cost` | the sum of any set of the above |

Turn a cost into a minimize-objective with `CostObjective`, or blend cost with service
using `WeightedObjective`.

### Optimizers — how to search

| Optimizer | Use when |
|---|---|
| `grid` | the space is small; you want every point evaluated |
| `random` | the space is large; a quick, unbiased scan |
| `hill_climb` | smooth surfaces (staffing, capacity) — usually the best default |
| `genetic` | levers interact and a hill climb gets stuck |

`budget` is the number of **real (simulated) evaluations** the search may spend. Repeated
points are cached and do not cost budget. Every optimizer is guaranteed to stop.

### The CLI

```bash
twinflow optimize model.yaml --plan plan.csv \
  --lever labor.pools[0].headcount:2:6 \
  --objective robust_on_time --optimizer hill_climb --budget 12 --reps 12
```

- `--lever PATH:MIN:MAX[:STEP]` — repeat for more than one lever. Ranges are integers.
- Prints the winning levers, the best score, its on-time band, and how many evaluations
  it used.

### The Python facade

```python
from twinflow.model import load_model
from twinflow.plan.loader import load_plan
from twinflow.modules import LeverSpace, IntRange, OBJECTIVES, OPTIMIZERS, optimize

model = "model.yaml"
plan = load_plan("plan.csv", load_model(model).registry)

result = optimize(
    model, plan,
    LeverSpace({"labor.pools[0].headcount": IntRange(2, 6)}),
    OBJECTIVES.create("robust_on_time"),
    OPTIMIZERS.create("hill_climb"),
    budget=12, reps=12,
)
print(result.best.scenario.levers, result.best_score)
for scenario, score in result.history:      # the search trail, for a convergence chart
    print(scenario.levers, score)
```

## Worked example A — staffing for on-time delivery

Question: how many operators to hit on-time without over-hiring?

```bash
twinflow optimize model.yaml --plan plan.csv \
  --lever labor.pools[0].headcount:2:8 \
  --objective robust_on_time --optimizer hill_climb --budget 15 --reps 20
```

Read the result: the headcount that maximizes the *reliable* on-time %, with its band. If
the band barely moves past a certain headcount, that is your point of diminishing returns.

## Worked example B — capacity vs cost

Question: is a second machine at the bottleneck worth it? Optimize on-time, then price each
option with a cost function and compare in Python:

```python
from twinflow.modules import CostObjective, TotalCost, LaborCost, CapacityCost

# search capacity + staffing for on-time
result = optimize(
    model, plan,
    LeverSpace({
        "locations[mill].capacity": IntRange(1, 3),
        "labor.pools[0].headcount": IntRange(2, 6),
    }),
    OBJECTIVES.create("robust_on_time"),
    OPTIMIZERS.create("genetic"),
    budget=25, reps=20,
)

# price the winner
cost = TotalCost((LaborCost(wage_per_hour=40.0), CapacityCost(cost_per_machine=1500.0)))
print("best:", result.best.scenario.levers, "on-time:", result.best_score)
print("cost of best: $", round(cost.cost(result.best)))
```

## Worked example C — inventory optimization

Because a stock's `reorder_point` and `refill_to` are ordinary levers, tuning inventory is
just optimization. Minimize holding + stockout cost by searching the reorder point:

```python
from twinflow.modules import CostObjective, TotalCost, InventoryHoldingCost, StockoutPenalty

spend = TotalCost((
    InventoryHoldingCost(rate_per_unit_hour=0.02),   # cost to hold a unit for an hour
    StockoutPenalty(penalty_per_hour=200.0),         # cost of an empty stock per hour
))

result = optimize(
    "examples/supply-chain/model.yaml", plan,
    LeverSpace({"stocks[resin].reorder_point": IntRange(50, 400, 25)}),
    CostObjective("inventory_spend", spend),          # a minimize objective
    OPTIMIZERS.create("hill_climb"),
    budget=15, reps=20,
)
print("best reorder point:", result.best.scenario.levers, "spend: $", round(result.best_score))
```

Set the reorder point too low and you pay stockout penalties; too high and you pay to hold
inventory. The optimizer finds the balance — and the twin still reports the service-level
band so you can see the risk you are buying.

## Level 3 — surrogate models (screen before you simulate)

When even one evaluation is expensive, fit a cheap **surrogate** on the runs you already
did, then let it rank the whole grid to pick the few points worth really simulating:

```python
from twinflow.modules import MODELS, predicted_ranking, OBJECTIVES

surrogate = MODELS.create("linear")                   # or "nearest_neighbor"
surrogate.fit(result.evaluations, OBJECTIVES.create("on_time_pct"))
ranked = predicted_ranking(surrogate, space)          # [(levers, predicted_score), ...]
```

A surrogate is an approximation, never the truth. Its ranking chooses *what to simulate*;
the twin still produces the honest, banded answer.

## Adding your own objective, cost, or optimizer

All four kinds live in registries you can extend with one call — no core change. See
[modules.md](modules.md) for the pattern and how to write a new one.

## Next

- Make the twin match your real numbers first, so the optimum is trustworthy:
  [tuning.md](tuning.md).
- The module surface in depth: [modules.md](modules.md).
- Active-control levers (dispatch, release): [modeling.md](modeling.md#active-control-turning-the-twin-into-a-decision-tool).
