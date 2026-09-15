# Supply-chain example

A floor whose feedstock is **finite and self-refilling**, fed by a **two-echelon**
inventory chain with real supplier **lead times** — all declared purely in config
(D-044).

```
 external supplier          bulk_resin (warehouse)        resin (working stock)
   lead 4000 s      ─────►    initial 500 kg      ─────►    initial 300 kg
                             reorder < 180                 reorder < 130
                             order up to 800               order up to 450
                                                           lead 1200 s, supplier: bulk_resin
                                                                │
                                                          mold pulls 15 kg / firing
                                                                │
                                                             part
```

The single work center `mold` consumes an (infinite, demand-released) `raw_blank` and
pulls 15 kg of `resin` per firing from the finite working stock. When `resin` drops
below 130 kg it orders up to 450 kg from `bulk_resin`, arriving 1200 s later; that draw
pulls `bulk_resin` below 180 kg, so it in turn orders up to 800 kg from an external
supplier, arriving 4000 s later. An (s, S) guard means only one order per stock is ever
in transit at a time; a stock that empties before its order lands is a real stockout
that blocks the mold without deadlocking.

## Config surface

| Field | On | Meaning |
|---|---|---|
| `reorder_point` / `refill_to` | a stock | order up to `refill_to` when on-hand drops below `reorder_point` |
| `lead_time` | a stock | seconds until a placed order is delivered (`0` = instantaneous) |
| `supplier` | a stock | draw the order quantity from this upstream stock (a multi-echelon chain) instead of an infinite external source |
| `material: {stock, qty, uom}` | a location | pull a secondary material from a finite stock each firing |

## Run it

```bash
twinflow validate examples/supply-chain/model.yaml
twinflow run examples/supply-chain/model.yaml --plan examples/supply-chain/plan.csv --reps 20
```

Each run writes an `inventory.parquet` next to `events.parquet`. From it,
`twinflow.instrumentation.inventory.compute_inventory_kpis` derives per-stock KPIs:
time-weighted **average level**, **ending level**, **stockout seconds**, **orders
placed**, and **total ordered**.

## What this shows

- Inventory is a first-class, config-declared part of the twin, not a bolt-on.
- The two echelons and the supplier lead times are ordinary config the Layer-2 compiler
  turns into pure primitives — the same rule as every other capability.
- Because the reorder points and refill levels are ordinary config levers
  (`stocks[resin].reorder_point`, `refill_to`), the [module surface](../../docs/modules.md)
  can **optimize inventory policy**: search reorder points against a total-cost objective
  (holding + stockout), the same way it tunes staffing.

## Sweep the levers

`examples/supply-chain/sweep.json` grids the single work center's machine
count:

```json
{ "locations[mold].capacity": [1, 2, 3] }
```

```bash
twinflow balance examples/supply-chain/model.yaml --plan examples/supply-chain/plan.csv \
  --sweep examples/supply-chain/sweep.json --reps 10
```

This runs the floor at 1, 2, and 3 molds, 10 replications each. A second
mold pulls `resin` twice as fast, which drains the two-echelon stock chain
faster too — so this is also a check on whether the reorder points and lead
times declared on `resin` / `bulk_resin` can keep up with faster demand
before you add a real machine.

## Import into the workspace UI

`supply-chain.twin.yaml` in this folder is the same floor and plan packaged as a
scenario capsule. Choose it in the workspace's **Import scenario** dialog, or
run it directly:

```bash
twinflow scenario validate examples/supply-chain/supply-chain.twin.yaml
twinflow scenario run examples/supply-chain/supply-chain.twin.yaml --seed 42
```

`model.yaml` on its own is not a capsule; the importer says so if you pick it.
