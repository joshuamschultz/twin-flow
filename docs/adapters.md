# Adapter surfaces

![Three plugs from server racks docking into one socket block that feeds a factory](images/headers/adapters.webp)

`twinflow.adapters` holds the **unified seams external systems plug into**. Each is a
protocol + a registry + reference implementations. A real ERP or forecaster
is then a single class and one `register(...)` call — never a change to the core. The
concrete SAP / OPC-UA / Prophet integrations are deliberately *not*
here; this is what they attach to so they work the moment they land.

Discover what is registered at runtime: `GET /api/adapters`, or `.names()` on any
registry below. Dispatch rules (`fifo`, `edd`, `spt`, `critical_ratio`) are not an adapter;
set them per location with `dispatch:` in the model (see [active control](active-control.md))
and read the names from `GET /api/adapters` (`dispatch_policies`).

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
