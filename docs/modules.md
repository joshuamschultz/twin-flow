# The module surface

`twinflow.modules` is one unified seam that many optimizers, objective functions, cost
functions, and machine-learning models all attach to the same way. The idea: the twin
already turns a **scenario** (a set of config lever overrides) into **KPIs with a
confidence band**. Everything else is a small function over that.

```
ScoringSurface(model, plan)  ──►  Scenario ──► Evaluation (KPIs + band)
       │                                   │                    │
   Optimizer  ──── ranks by ─────────► Objective  ◄── money ── CostFunction
       │                                                        │
   SurrogateModel (learns the surface to screen scenarios cheaply)
```

Each kind lives in its own registry. Adding one is a single `register(name, factory)`
call — never a core edit. This is the module-system analogue of the compiler's single
growth point: capability is added at one place, not branched through the core.

## The scoring surface

```python
from twinflow.model import load_model
from twinflow.plan.loader import load_plan
from twinflow.modules import ScoringSurface, Scenario

compiled = load_model("examples/cnc-shop/model.yaml")
plan = load_plan("examples/cnc-shop/plan.csv", compiled.registry)

surface = ScoringSurface("examples/cnc-shop/model.yaml", plan, reps=20, base_seed=0)
evaluation = surface.evaluate(Scenario(levers={"labor.pools[0].headcount": 3}))

print(evaluation.intervals.on_time_pct)   # Interval(mean, lo, hi, p50, n)
print(evaluation.kpis.run_hours)
```

Every scenario runs at the same `base_seed`, so replication *i* of one scenario shares
its random draws with replication *i* of another — common random numbers, so
comparisons are fair rather than noisy. A lever path is the same dot-path scheme sweeps
use: `labor.pools[0].headcount`, `locations[mill_shaft].capacity`.

## Objectives — KPIs → one score

An objective declares a `direction` (`"max"` or `"min"`) and a `score(evaluation)`. The
optimizer only needs to compare two scores; it never learns which metric it is.

| Name | Direction | Reads |
|---|---|---|
| `on_time_pct` | max | mean on-time % |
| `robust_on_time` | max | the **lower** edge of the on-time band (reliable, not lucky) |
| `makespan` | min | last completion time |
| `mean_lateness` | min | average signed lateness |
| `utilization` | max | mean utilization across cells |
| `service_level` | max | supply-chain service level (100 minus the average share of the run each stock sat empty) |

```python
from twinflow.modules import OBJECTIVES
objective = OBJECTIVES.create("robust_on_time")
```

Combine metrics with `WeightedObjective` (a linear blend, each term oriented so higher
is always better), or turn a cost into an objective with `CostObjective`.

## Cost functions — config + KPIs → money

A cost function reads the scenario's configuration (headcount, capacity) and its result
(lateness) and returns money. It never runs the simulator. Every rate is an assumption
you supply — the module invents no numbers.

| Name | Cost |
|---|---|
| `labor_cost` | `sum(headcount) * wage/hr * makespan hours` |
| `capacity_cost` | `sum(location capacity) * cost per machine` |
| `lateness_penalty` | `sum(positive lateness) hours * penalty/hr` |
| `inventory_holding_cost` | `sum(time-weighted avg level) * rate/unit/hr * horizon hours` |
| `stockout_penalty` | `sum(stockout hours) * penalty/hr` |
| `total_cost` | the sum of any set of the above |

Because a stock's `reorder_point` and `refill_to` are ordinary config levers
(`stocks[resin].reorder_point`), **inventory optimization is just optimization**: search
those levers with any optimizer against a `total_cost` of holding + stockout, or maximize
`service_level`. No special machinery — the unified surface already covers it.

```python
from twinflow.modules import LaborCost, CapacityCost, TotalCost, CostObjective
cost = TotalCost((LaborCost(wage_per_hour=40.0), CapacityCost(cost_per_machine=1000.0)))
minimise_cost = CostObjective("spend", cost)   # direction "min"
```

## Optimizers — search the lever space

Every optimizer takes the same four things (a surface, a `LeverSpace`, an objective, an
evaluation `budget`) and returns the same `OptimizationResult`. They **propose**; they
never commit a change to a real floor.

| Name | Strategy |
|---|---|
| `grid` | enumerate the whole space (bounded by budget) |
| `random` | sample the space uniformly (seeded) |
| `hill_climb` | greedy local search with random restarts |
| `genetic` | tournament selection, uniform crossover, mutation, elitism |

```python
from twinflow.modules import LeverSpace, IntRange, OBJECTIVES, OPTIMIZERS, optimize

space = LeverSpace({"labor.pools[0].headcount": IntRange(2, 6)})
result = optimize(
    "examples/cnc-shop/model.yaml", plan, space,
    OBJECTIVES.create("on_time_pct"), OPTIMIZERS.create("hill_climb"),
    budget=12, reps=12,
)
print(result.best.scenario.levers, result.best_score)
for scenario, score in result.history:   # the convergence trail a UI plots
    print(scenario.levers, score)
```

`budget` bounds the number of real (simulated) evaluations; repeat points are cached and
do not spend budget. Every optimizer is guaranteed to terminate even when the space is
smaller than the budget.

A `LeverSpace` domain is either an `IntRange(low, high, step)` (staffing, capacity,
buffers) or a `Choice((...))` (a categorical knob).

## Surrogate models — learn the surface

A surrogate is a cheap stand-in that, once fit on a handful of real evaluations,
predicts an objective's score for any unseen lever point instantly. Use it to rank a
whole grid and pick the few scenarios worth really simulating.

| Name | Model |
|---|---|
| `linear` | ordinary least squares over the featurised levers |
| `nearest_neighbor` | the score of the closest fit point |

```python
from twinflow.modules import MODELS, predicted_ranking
surrogate = MODELS.create("linear")
surrogate.fit(result.evaluations, OBJECTIVES.create("on_time_pct"))
ranking = predicted_ranking(surrogate, space)   # [(levers, predicted_score), ...]
```

A surrogate is an approximation, never the truth — its ranking chooses *what to
simulate*, and the twin still produces the honest, uncertainty-carrying score.

## Adding your own module

Any of the four kinds is added by registering a factory. No core file changes.

```python
from twinflow.modules import OBJECTIVES
from twinflow.modules.objectives import MetricObjective

OBJECTIVES.register(
    "wip_ceiling",
    lambda: MetricObjective(
        "wip_ceiling", "min",
        lambda ev: float(ev.kpis.wip_by_location["wip"].max() or 0),
    ),
)
```

The same pattern works for `COSTS`, `OPTIMIZERS`, and `MODELS`. A duplicate name is
rejected (fail closed), and every registry exposes `.names()` for discovery — which is
exactly what the REST API's `/api/modules` endpoint returns.

To write a brand-new optimizer or surrogate, implement the corresponding `Protocol`
(`Optimizer`, `SurrogateModel`) — a class with the required methods and a `name` — and
register a factory for it.
