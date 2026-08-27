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
