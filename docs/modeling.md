# Modeling a floor

A floor is one `model.yaml` plus a production plan (`.xlsx`/`.csv`). The engine has no
per-client code — every capability below is declarative config the Layer-2 compiler
turns into pure primitives. This page is the config reference; the README's "building
blocks" section is the plain-language tour.

## Skeleton

```yaml
defaults:
  cycle_time_cv: 0.2          # model-wide cycle-time spread (a variable floor by default)

stocks:
  - {name: round_bar, uom: ft}

part_types:
  - {name: round_bar, uom: ft, attributes: {}}
  - {name: blank, uom: piece, attributes: {}}

labor:
  pools:
    - {name: floor_pool, headcount: 6, skills: [machinist]}

locations:
  - name: saw
    consumes: [{thing: round_bar, qty: 1, uom: ft}]
    emits:    [{thing: blank, qty: 4, uom: piece}]
    setup_key: grp_saw
    time_model: {kind: rate_based, rate: 0.5}
    machine: saw_m
    labor_skill: machinist

routing:
  - {part: blank, steps: [saw]}
```

## Time models

| Kind | Shape | Time charged |
|---|---|---|
| `rate_based` | `{kind: rate_based, rate: R}` | `qty / R` |
| `distribution` | `{kind: distribution, dist: lognormal, mean: M, cv: C}` | a seeded draw (`lognormal`, `normal`, `triangular`, `uniform`, `exponential`) |
| `attribute_scaled` | scales by a bundle attribute | per the attribute |
| `batch_hold` | `{kind: batch_hold, seconds: N}` (or a `dist`) | **one** hold for the whole accumulated batch (Tier 0) |

Add `cv: C` to any time model (or set `defaults.cycle_time_cv`) for a lognormal spread;
`0` keeps it deterministic. A `load`/`unload` split is optional.

## Core knobs

- **`scrap: {rate, thing, uom}`** — a fixed fraction becomes a scrap output bundle.
- **`batch_size: "200 piece"`** — accumulate to a threshold in the thing's own unit
  before the operation fires (otherwise arrival order).
- **`capacity: N`** — N identical machines in parallel at this center.
- **`setup_key: grp_x`** — parts in the same group run back to back; see changeover.
- **`output_stocks: {thing: stock_name}`** — route an output back into a named stock
  (recycle / remelt).

## Tier 0 capabilities

### Changeover time

```yaml
locations:
  - name: melt
    changeover_seconds: 120     # charged when the machine's setup group changes
    setup_key: grp_melt
    ...
```

The setup time elapses in simulation and appears in the `setup_seconds` event-log column
and the `setup_hours` KPI.

### Reorder-point / self-refilling stock + secondary material

```yaml
stocks:
  - {name: sand, uom: kg, initial: 0, reorder_point: 20, refill_to: 200}

locations:
  - name: mold
    material: {stock: sand, qty: 3, uom: kg}   # pull 3 kg of sand per firing
    ...
```

`sand` starts empty and tops itself back up to `refill_to` the instant a pull would drop
it below `reorder_point`, so a finite feedstock never deadlocks.

### Supplier lead time + multi-echelon inventory

```yaml
stocks:
  # An upstream echelon replenished from an infinite external supplier after 1 hr.
  - {name: bulk_resin, uom: kg, initial: 2000, reorder_point: 500, refill_to: 3000, lead_time: 3600}
  # A downstream stock that reorders FROM bulk_resin (an echelon), delivered after 10 min.
  - {name: resin, uom: kg, initial: 400, reorder_point: 100, refill_to: 800, supplier: bulk_resin, lead_time: 600}
```

- `lead_time` (seconds) delays a placed order's arrival; the policy is order-up-to
  `refill_to` with a single-order-in-transit guard. Default 0 = instantaneous.
- `supplier: <stock>` draws the refill from an upstream stock instead of an infinite
  source, forming a cascading chain (validated against cycles).
- A stock that empties before its order lands is a real stockout that blocks consumers
  without deadlocking. Every run writes an `inventory.parquet` next to `events.parquet`;
  `twinflow.instrumentation.inventory.compute_inventory_kpis` turns it into average level,
  stockout time, and orders placed per stock. See [`examples/supply-chain/`](../examples/supply-chain/).

### Probabilistic quality gate

```yaml
locations:
  - name: inspect
    quality_gate:
      thing: part
      branches:
        - {prob: 0.9, to: null}         # pass -> finished (terminal)
        - {prob: 0.1, to: scrap_bin}    # fail -> another location, or a stock name
    ...
```

Whole units are routed down a pass/fail branch by chance. Branch probabilities must sum
to 1.0 (validated). The draw comes from a dedicated seeded `SOURCE_ROUTING` stream, so
the split is reproducible and pairs across replications (common random numbers). This is
distinct from `scrap`, which splits a fraction of every firing rather than routing whole
units.

### Batch / hold

```yaml
locations:
  - name: anneal
    batch_size: "5 piece"
    time_model: {kind: batch_hold, seconds: 400}   # ONE 400 s hold for the group
    ...
```

## The plan

A table (`.xlsx` first sheet, or `.csv`) with these columns:

| work_order_id | part | qty | start_date | due_date |
|---|---|---|---|---|
| WO-1001 | carton | 500 | 0 | 3600 |

Optional initial-WIP columns: `initial_wip_location`, `initial_wip_qty`,
`initial_wip_remaining_time` (all three together or none). Formula cells are rejected,
never evaluated.

## Worked examples

`examples/` holds `cnc-shop`, `cnc-shop-3mill` (capacity investment), `foundry`,
`spring`, `led-manufacturer`, and `tier0-foundry` (composes all four Tier 0
capabilities). Each is a `model.yaml` + `plan.csv` + `README.md`.
