# FOUNDRY worked example

A sand-casting floor expressed purely as config (`model.yaml` + `plan.csv`,
no Python). Two parallel casting lines share one raw metal feedstock and one
shared recycle stock:

```
metal_ingot -> melt -> pour (continuous-to-discrete: lb -> piece castings)
             -> clean (splits into good + scrap) -> machine -> finished

Line A: melt_a -> pour_a -> clean_a -> machine_a  =>  bracket
Line B: melt_b -> pour_b -> clean_b -> machine_b  =>  flywheel
```

## The remelt loop

`clean_a` and `clean_b` each scrap a fraction of the castings they clean
(8% and 12% respectively). That scrap is routed back into a shared, named
top-level stock instead of a dead-end sink:

```yaml
scrap:
  rate: 0.08
  thing: remelt
  uom: piece
output_stocks:
  remelt: remelt
```

This is the general "route an output back to a Stock" recycle capability
(`stock_destinations`), not a foundry-only special case — it works
identically for any declared `emits`/`scrap` output, per
`tests/unit/test_stock_destination.py`. Per the stock-identity rule
(`Stock.put()` checks the bundle's `thing` against the stock's own `name`),
the routed thing and the declared stock share the literal name `remelt`.
After a run, `RunResult.run_meta["stock_levels"]["remelt"]` reports the total
scrap recycled across both lines.

## Design notes / assumptions

- **Two lines, not one shared chain.** `RunDriver._wire_routing` rewrites a
  location's `destinations[<thing>]` per routing entry, keyed only by
  `thing` — so a single shared location cannot fan its output to two
  different next-locations depending on which finished part's routing is
  being walked (last-registered routing wins, silently). Two independent
  lines (only the raw `metal_ingot` stock and the `remelt` recycle stock are
  shared) avoid that collision entirely and stay within today's schema; no
  engine change was needed or made.
- **`rate_based` time models only.** The task allowed `rate_based` or
  `distribution`. `distribution`'s `TimeModel` primitive expects
  `params["draw"]` to be a Python **callable**
  (`src/twinflow/primitives/time_model.py`), and `LocationCompiler` passes a
  location's `time_model` block straight through from YAML with no sugar
  that turns a declarative distribution (e.g. `dist: normal, mean: ..., std:
  ...`) into that callable — so `distribution` cannot round-trip through
  pure YAML config today (same gap applies to `attribute_scaled`'s `scale`
  callable). This is a documented compiler gap, not something this example
  works around with Python; `rate_based` is used throughout, matching
  `examples/spring/model.yaml`'s own precedent.
- Kept intentionally boring per scope: one station per step, no parallel
  stations, no quality gates, no batch cooling — later phases of the
  multi-station-foundry feature, not needed here.

## Running it

```bash
twinflow validate examples/foundry/model.yaml
twinflow run examples/foundry/model.yaml --plan examples/foundry/plan.csv --reps 1
twinflow report <run-id> --out html
```

## Sweep the levers

`examples/foundry/sweep.json` grids each casting line's melt-furnace machine
count independently:

```json
{
  "locations[melt_a].capacity": [1, 2],
  "locations[melt_b].capacity": [1, 2]
}
```

```bash
twinflow balance examples/foundry/model.yaml --plan examples/foundry/plan.csv \
  --sweep examples/foundry/sweep.json --reps 10
```

This runs all four combinations (each line at 1 or 2 furnaces), 10
replications each, so you can see whether a second furnace on line A, line
B, or both moves the KPIs more, given the two lines share only the raw
`metal_ingot` feedstock and the `remelt` recycle stock.

## Import into the workspace UI

`foundry.twin.yaml` in this folder is the same floor and plan packaged as a
scenario capsule. Choose it in the workspace's **Import scenario** dialog, or
run it directly:

```bash
twinflow scenario validate examples/foundry/foundry.twin.yaml
twinflow scenario run examples/foundry/foundry.twin.yaml --seed 42
```

`model.yaml` on its own is not a capsule; the importer says so if you pick it.
