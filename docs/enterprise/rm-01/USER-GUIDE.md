# RM-01 user guide

RM-01 adds bounded, evidence-retaining execution while preserving the original
three positional arguments to `RunDriver.run` and `ReplicationRunner.run`.

## One bounded run

```python
from pathlib import Path

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

model_path = "examples/cnc-shop/model.yaml"
compiled = load_model(model_path)
plan = load_plan("examples/cnc-shop/plan.csv", compiled.registry)

result = RunDriver(compiled).run(
    plan,
    seed=42,
    replication_index=0,
    artifact_dir=Path("artifacts/baseline"),
    max_sim_time=2_592_000,
    max_events=1_000_000,
    max_wall_seconds=120.0,
)

print(result.outcome, result.termination_reason)
print(result.order_outcomes)
print(result.artifact_dir)
```

The first reached bound stops the simulation. `termination_reason` is one of
`demand_completed`, `natural_exhaustion`, `sim_time_limit`, `event_limit`, or
`wall_time_limit`. Natural exhaustion only describes the event queue; inspect
`result.outcome` and every order ledger to determine fulfillment.

Each positive-quantity order ledger reports required, accepted, scrapped, shipped,
and remaining quantities. `completion_time` is present only when accepted output at
the order's terminal routing operation meets required quantity. A terminal scrap or
an intermediate operation cannot create a completion.

The run directory contains `events.parquet`, `inventory.parquet`, `plan.json`, and
`run_meta.json`. The latter records seeds, bounds, runtime identity, termination, and
the complete order ledger.

## Bounded replications

```python
from twinflow.plan.replication import ReplicationRunner

results = ReplicationRunner(model_path).run(
    plan,
    reps=20,
    base_seed=42,
    artifact_dir="artifacts/study-001",
    max_workers=4,
    max_sim_time=2_592_000,
    max_events=1_000_000,
    max_wall_seconds=120.0,
)
```

`max_workers` bounds the process pool even on large machines. Each replication writes
beneath `replication-NNNN`; `resolved-model.yaml` is retained at the study root.

## Statistical fields

`aggregate_kpis(...).completion_distributions[order_id]` provides:

- `quantiles`: observed P10/P50/P90 outcome quantiles when every replication completes;
- `mean_ci`: Student-t confidence interval for the estimated mean;
- `observed_count` and `censored_count`;
- `estimability`: `estimated` or `not_estimable`.

If any completion is censored, completion quantiles are `None`. The compatibility
`Interval.lo`/`hi` fields now mean a confidence interval on the mean, not percentile
bounds.

`ScoringSurface` accepts the same artifact and execution bounds. Its `Evaluation`
retains `per_replication_kpis` and `per_replication_inventory`; objective functions
use those replications rather than silently scoring the first trace.

## Limits

The terminal ledger currently treats accepted terminal output as shipped immediately.
There is no separate shipping event yet. `infeasible` and `failed` are reserved outcome
states; this foundation reports unfinished bounded or exhausted runs as
`incomplete_at_horizon`. Model validation against real operations, calibrated promise
dates, keyed event randomness across topology changes, durable cancellation, and
cross-host environment reconstruction remain outside this change.

