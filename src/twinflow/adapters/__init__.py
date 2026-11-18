"""twinflow.adapters, the unified surfaces external systems plug into.

Each is a stable seam (a protocol + a registry + reference implementations), so a real
integration is a single class and one `register(...)` call, never a core change:

- **connectors**: data in/out of a system of record (ERP/MES): `PlanSource`,
  `ResultSink`, `ActualSource`, with a `FieldMapping` onto the canonical plan contract.
- **demand**: generate a plan from a product mix + arrival process (`DemandGenerator`).
- **forecast**: feed a demand/lead-time forecast in (`Forecaster`).
- **reconcile**: plan-vs-actual variance from a real event log.
- **fulfillment**: orders & deliveries KPIs (fill rate, on-time delivery, backorders).

The concrete SAP/OPC-UA/Prophet integrations are deliberately out of
scope here; this package is what they attach to so they work the moment they land.
"""

from __future__ import annotations

from twinflow.adapters.connectors import (
    ACTUAL_SOURCES,
    PLAN_SOURCES,
    RESULT_SINKS,
    ActualSource,
    CsvPlanSource,
    FieldMapping,
    JsonResultSink,
    ParquetActualSource,
    PlanSource,
    ResultSink,
)
from twinflow.adapters.demand import (
    DEMAND_GENERATORS,
    DemandGenerator,
    FixedDemand,
    PoissonDemand,
)
from twinflow.adapters.forecast import (
    FORECASTERS,
    Forecaster,
    MovingAverageForecaster,
    NaiveForecaster,
)
from twinflow.adapters.fulfillment import FulfillmentKpis, compute_fulfillment
from twinflow.adapters.reconcile import Variance, reconcile

__all__ = [
    "ACTUAL_SOURCES",
    "DEMAND_GENERATORS",
    "FORECASTERS",
    "PLAN_SOURCES",
    "RESULT_SINKS",
    "ActualSource",
    "CsvPlanSource",
    "DemandGenerator",
    "FieldMapping",
    "FixedDemand",
    "Forecaster",
    "FulfillmentKpis",
    "JsonResultSink",
    "MovingAverageForecaster",
    "NaiveForecaster",
    "ParquetActualSource",
    "PlanSource",
    "PoissonDemand",
    "ResultSink",
    "Variance",
    "compute_fulfillment",
    "reconcile",
]
