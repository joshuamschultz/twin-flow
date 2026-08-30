# Data contracts

Every piece of data that goes into twinflow or comes out of it, with a real example of
each. If you are wiring twinflow to another system, this is the page you need.

## What goes in

Two files make a complete client: a **model** ([modeling.md](modeling.md)) and a
**plan** (below). Nothing else.

### The production plan

A table — the first worksheet of an `.xlsx`, or a `.csv` with the same columns. It drives
which work orders are released, when, and any work already on the floor at the start.

**Required columns**

| Column | Type | Meaning |
|---|---|---|
| `work_order_id` | text | A unique id for the order |
| `part` | text | The finished part (must exist in the model's `part_types`) |
| `qty` | integer | How many to make |
| `start_date` | number | Release time, in seconds from run start |
| `due_date` | number | When it is due, in seconds from run start |

**Optional columns**

| Column | Type | Meaning |
|---|---|---|
| `priority` | integer | Rush priority. A higher number jumps the dispatch queue at every center. Blank or absent = 0 (normal). See [active-control.md](active-control.md). |
| `initial_wip_location` | text | Seed work already on the floor at this work center (t=0) |
| `initial_wip_qty` | integer | How much initial WIP |
| `initial_wip_remaining_time` | number | Remaining process time on that WIP |

The three `initial_wip_*` columns are all-or-nothing: populate all three or none.

```csv
work_order_id,part,qty,start_date,due_date,priority
WO-1001,carton,500,0,3600,0
WO-1002,carton,250,600,5400,0
RUSH-9,carton,50,600,99999,9
```

**Fail-closed rules** (the loader rejects, naming the 1-indexed row): an unknown part, a
missing required column, a non-numeric quantity, a partially populated initial-WIP row,
or a formula cell (a cell starting with `=` is never evaluated).

## What comes out

Every run writes a folder `runs/<run-id>/` with four artifacts, plus two event tables.

### `events.parquet` — the event log

One row per firing (one operation at one work center). Every KPI is derived from this one
table; nothing is computed anywhere else.

| Column | Meaning |
|---|---|
| `location_id` | The work center that fired |
| `part_id` | The part produced |
| `lot_id` | The lot (links a firing back to its order) |
| `process_name` | `transform`, `rework`, or `scrap` |
| `qty` | Quantity in this firing |
| `queue_arrival_time` | When the job joined this center's queue |
| `material_ready_time` | When required material became available (nullable) |
| `actual_start` | When processing began |
| `actual_end` | When processing finished |
| `release_time` | When the finished bundle left (held until downstream took it) |
| `outcome` | The firing outcome |
| `setup_seconds` | Changeover time charged before this firing |

The wait breakdown reads straight off these: `busy = actual_end - actual_start`,
`blocked = release_time - actual_end`, and the idle remainder is `starved`.

### `inventory.parquet` — inventory over time

One row per stock-level change (written next to `events.parquet`). Present even when a
floor has no stocks (then it is empty).

| Column | Meaning |
|---|---|
| `stock` | The stock name |
| `t` | Time of the change |
| `level` | On-hand level **after** the change (so the log is a step function) |
| `delta` | Signed change (negative consume, positive replenish, ordered qty on an order row) |
| `kind` | `seed`, `consume`, `replenish`, or `order` |

`twinflow.instrumentation.inventory.compute_inventory_kpis` turns this into average level,
stockout seconds, orders placed, and total ordered per stock. See [supply-chain.md](supply-chain.md).

### `kpis.json` — machine-readable KPIs

A versioned sidecar of the objective-agnostic KPIs: signed lateness per order, labor hours
by pool and skill, machine hours by machine, run hours, setup hours (separate), and WIP
over time. See [running.md](running.md) for what each KPI means.

### `intervals.json` — the confidence bands

Each headline KPI as a `{mean, lo, hi, p50, n}` interval across replications, plus the
`reps` count and confidence `level`. This is the honest range — read this, not a single
number. See [running.md](running.md).

### `run_meta.json` — the reproducibility stamp

Hashes of the model and plan, the seed, the engine and Python versions, and the exact
dependency set — enough to re-create the run to the number. The in-process `RunResult`
also carries a live `run_meta` dict with `run_id`, `seed`, `replication_index`,
`stock_levels` (ending inventory), and `downtime_seconds_by_machine` (machine downtime
from any [breakdowns](active-control.md)).

## Wiring to another system

You do not have to hand-write any of these. Point a connector at your ERP/MES export and
map its columns onto the plan contract above — see [adapters.md](adapters.md). The event
log's schema is shared by real and simulated runs, which is what makes
[plan-vs-actual reconciliation](adapters.md) possible.
