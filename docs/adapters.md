# Adapter surfaces

`twinflow.adapters` holds the **unified seams external systems plug into**. Each is a
protocol + a registry + reference implementations. A real ERP, RL agent, or forecaster
is then a single class and one `register(...)` call — never a change to the core. The
concrete SAP / OPC-UA / Prophet / stable-baselines integrations are deliberately *not*
here; this is what they attach to so they work the moment they land.

Discover what is registered at runtime: `GET /api/adapters`, or `.names()` on any
registry below.

## Connectors — data in and out of a system of record (ERP/MES)

Three capabilities; a connector implements whichever it can.

| Protocol | Method | Reference |
|---|---|---|
| `PlanSource` | `read_plan(registry) -> [WorkOrder]` | `CsvPlanSource` |
| `ResultSink` | `write_results(payload)` | `JsonResultSink` |
| `ActualSource` | `read_actuals() -> DataFrame` | `ParquetActualSource` |

The README's promise — "a connector becomes a column mapping onto the plan contract, not
a rewrite" — is `FieldMapping`: map the ERP's own column names onto twinflow's canonical
plan columns, and the shipped `load_plan` does the rest.

```python
from twinflow.adapters import CsvPlanSource, FieldMapping

source = CsvPlanSource(
    "erp_export.csv",
    FieldMapping(
        {
            "work_order_id": "WO",
            "part": "Item",
            "qty": "Quantity",
            "start_date": "Release",
            "due_date": "Due",
        }
    ),
)
orders = source.read_plan(registry)
```

**To add a real ERP:** implement `PlanSource`/`ResultSink`/`ActualSource` and
`PLAN_SOURCES.register("sap", SapPlanSource)`.

## Demand — generate a plan instead of importing one

`DemandGenerator.generate(seed) -> [WorkOrder]`. Built in: `FixedDemand` (evenly spaced,
deterministic) and `PoissonDemand` (seeded Poisson arrivals, random part + size). A
generated plan is reproducible, like everything else in the twin.

```python
from twinflow.adapters import PoissonDemand

plan = PoissonDemand(parts=("shaft", "bracket"), rate=0.01, horizon=3600, lead_time=1800).generate(
    seed=1
)
```

## Forecast — feed a demand/lead-time forecast in

`Forecaster.fit(history)` then `predict(periods) -> [float]`. Built in: `naive` and
`moving_average`. A forecast can supply the arrival rate or order size a demand
generator uses. **To add Prophet/ARIMA:** implement `Forecaster` and register it.

## Dispatch — which queued job a center runs next

`DispatchPolicy.select(queue, now)` / `.order(queue, now)` over `DispatchJob`s. Built in:
`fifo`, `edd` (earliest due date), `spt` (shortest processing time), `critical_ratio`
(slack per unit work). These are the scheduling seam; wiring one into the engine's queues
is the active-scheduling roadmap item.

## RL — the twin as a Gymnasium environment

`TwinEnv` wraps the `ScoringSurface` with the modern Gymnasium API:

```python
from twinflow.adapters import TwinEnv
from twinflow.modules import ScoringSurface, LeverSpace, IntRange
from twinflow.modules.objectives import OBJECTIVES

env = TwinEnv(
    ScoringSurface("examples/cnc-shop/model.yaml", plan, reps=5),
    LeverSpace({"labor.pools[0].headcount": IntRange(1, 8)}),
    OBJECTIVES.create("on_time_pct"),
    max_steps=10,
)
obs, info = env.reset()
obs, reward, terminated, truncated, info = env.step((2,))  # nudge the lever up
```

An action nudges each lever up/down/hold; the reward is the objective's score (negated
for a `min` objective, so an agent always maximises). No gymnasium dependency is
required to *use* the env; `env.to_gymnasium()` adapts it to a real `gymnasium.Env` when
gymnasium is installed (`pip install gymnasium`), so **stable-baselines3 and friends plug
straight in.**

## Reconcile — plan vs actual

`reconcile(planned_kpis, actual_kpis) -> Variance`. The actual `KpiSet` comes from
running the shipped `compute_kpis` over a real event log (read via an `ActualSource`),
since it shares the twin's event schema. The `Variance` reports on-time, run-hours, and
per-center utilization gaps — the numbers that tell you whether to trust or correct the
model.

## Fulfillment — orders & deliveries

`compute_fulfillment(kpis) -> FulfillmentKpis`: fill rate, on-time delivery %,
backorders, and average delivery lateness, derived from the order outcomes the twin
already produces.
