# Supply chain & inventory

Most floors run on material that is not infinite: a resin, a sand, a bar stock that gets
used up and reordered. twinflow models that material as **stock** you can make finite,
self-refilling, and even fed by an upstream warehouse with a real delivery lead time. Then
it reports how much you held, how long you ran out, and lets you *optimize* your reorder
points.

Worked example: [`examples/supply-chain/`](../examples/supply-chain/).

> New to twinflow? Read [concepts](concepts.md) and [modeling](modeling.md) first.

---

## 1. Finite, self-refilling stock

A stock with a reorder point tops itself back up when it runs low, so a finite feedstock
never blocks the floor forever.

```yaml
stocks:
  - {name: sand, uom: kg, initial: 200, reorder_point: 50, refill_to: 400}
```

| Field | Meaning |
|---|---|
| `initial` | on-hand quantity at the start of the run (default 0) |
| `reorder_point` | when on-hand drops below this, place an order |
| `refill_to` | order enough to bring on-hand back up to this level (an order-up-to, or (s, S), policy) |

Only **one order per stock** is ever in transit at a time (the (s, S) guard).

---

## 2. Supplier lead time

Real orders do not arrive instantly. `lead_time` (seconds) delays a placed order's arrival.

```yaml
stocks:
  - {name: resin, uom: kg, initial: 400, reorder_point: 100, refill_to: 800, lead_time: 1800}
```

- `lead_time: 0` (the default) is instantaneous refill.
- If a stock empties **before** its order lands, that is a real **stockout**: the work
  center that needs the material waits until delivery. It blocks, but it does not deadlock
  (the order is already on the way).

---

## 3. Multi-echelon (a stock fed by another stock)

Add `supplier: <stock_name>` and a stock draws its refill from an **upstream** stock
instead of an infinite external source. Chain them for a warehouse → working-stock model.

```yaml
stocks:
  # Upstream warehouse, replenished from an infinite external supplier after ~1 hour.
  - {name: bulk_resin, uom: kg, initial: 500, reorder_point: 180, refill_to: 800, lead_time: 4000}
  # Working stock, reorders FROM bulk_resin, delivered after 20 minutes.
  - {name: resin, uom: kg, initial: 300, reorder_point: 130, refill_to: 450, lead_time: 1200, supplier: bulk_resin}
```

When `resin` drops below 130 kg it orders up to 450 kg from `bulk_resin`; that draw can pull
`bulk_resin` below 180 kg, so `bulk_resin` in turn orders from its external supplier. Cycles
are rejected at validation.

---

## 4. Pulling material at a work center

A location pulls a secondary material from a finite stock each time it runs, with
`material:`.

```yaml
locations:
  - name: mold
    consumes: [{thing: raw_blank, qty: 1, uom: piece}]
    emits: [{thing: part, qty: 1, uom: piece}]
    material: {stock: resin, qty: 15, uom: kg}   # pull 15 kg of resin per firing
    ...
```

Each `mold` firing draws 15 kg from `resin`, which is what eventually triggers `resin`'s
reorder.

---

## 5. Inventory KPIs

Every run writes an `inventory.parquet` next to its `events.parquet`. From it,
`twinflow.instrumentation.inventory.compute_inventory_kpis(path, horizon)` derives one set
of numbers **per stock**:

| KPI | Meaning |
|---|---|
| `average_level` | time-weighted average on-hand over the run |
| `ending_level` | on-hand at the end of the run |
| `stockout_seconds` | total time the stock sat empty (a real shortage) |
| `orders_placed` | how many replenishment orders were placed |
| `total_ordered` | total quantity delivered |

The [REST API](api.md)'s run result and the [front end](frontend.md)'s Inventory table
surface these directly.

---

## 6. Inventory economics and optimization

Because a stock's `reorder_point` and `refill_to` are ordinary config levers,
**inventory optimization is just [optimization](optimizing.md)** — no special machinery.
The [module surface](modules.md) adds the money and service pieces:

| Piece | Kind | Meaning |
|---|---|---|
| `inventory_holding_cost` | cost | `sum(average level) * rate/unit/hr * hours` — the capital tied up in stock |
| `stockout_penalty` | cost | `sum(stockout hours) * penalty/hr` — the cost of running out |
| `service_level` | objective (max) | 100 minus the average share of the run each stock sat empty |

The classic trade-off — hold less inventory (cheaper) vs. stock out more (costly) — falls
straight out: search the reorder points against a `total_cost` of holding + stockout, or
maximize `service_level`.

```python
from twinflow.modules import (
    LeverSpace,
    IntRange,
    OPTIMIZERS,
    CostObjective,
    TotalCost,
    InventoryHoldingCost,
    StockoutPenalty,
    optimize,
)

space = LeverSpace({"stocks[resin].reorder_point": IntRange(50, 300, step=25)})
objective = CostObjective(
    "inventory_cost",
    TotalCost(
        (InventoryHoldingCost(rate_per_unit_hour=0.01), StockoutPenalty(penalty_per_hour=500.0))
    ),
)
result = optimize(
    "examples/supply-chain/model.yaml",
    plan,
    space,
    objective,
    OPTIMIZERS.create("hill_climb"),
    budget=12,
    reps=12,
)
print(result.best.scenario.levers, result.best_score)
```

---

## Worked example

```bash
twinflow validate examples/supply-chain/model.yaml
twinflow run examples/supply-chain/model.yaml --plan examples/supply-chain/plan.csv --reps 20
```

See also: [modeling](modeling.md) · [modules](modules.md) · [optimizing](optimizing.md) · [troubleshooting](troubleshooting.md).
