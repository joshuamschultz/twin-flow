# Spring worked example

A spring-making floor expressed purely as config (no Python): wire stock
runs through cut &rarr; form &rarr; stress-relief &rarr; grind &rarr; coat
to a finished coated spring.

- **`cut`** is the continuous-to-discrete step: it consumes wire by the foot
  and emits two ordinary outputs from the same recipe basis &mdash; `blank`
  (5 pieces per foot) and `wire_remainder` (the leftover ~2 in of wire per
  foot), both declared as plain `emits` ratios, no scrap block or
  hand-written emit expression.
- **`form`**, **`grind`**, and **`coat`** are ordinary one-in-one-out steps.
- **`stress_relief`** runs as a batch: it accumulates up to 50 pieces before
  firing, instead of processing one piece at a time like every other step.

One shared labor pool, `floor_pool` (headcount 5, skills
`cut_op`/`form_op`/`relief_op`/`grind_op`/`coat_op`), staffs every station.
Each of the five stations has its own dedicated machine
(`cut_m`, `form_m`, `relief_m`, `grind_m`, `coat_m`).

## Run it

```bash
twinflow validate examples/spring/model.yaml
twinflow run examples/spring/model.yaml --plan examples/spring/plan.csv --reps 1
twinflow report <run-id> --out html
```

`plan.csv` is a 9-order book, all for the same `coated` spring, released
over the first ~4,000 simulated seconds with a due date far out at 100,000
seconds &mdash; a loose, realistic order book rather than a stress test.

## What to look for

This floor is intentionally not overloaded. Open `intervals.json` (or
`report.html`) after a multi-rep run and you'll see:

- **`on_time_pct`** comes back at 100% &mdash; every order finishes well
  before its due date.
- **`utilization_by_cell`** stays low everywhere: `stress_relief` is the
  busiest station at roughly 26% busy, with `cut`, `form`, `grind`, and
  `coat` all under 20%. No station is close to saturated.
- **`stress_relief`** is the busiest of the five even though its own
  per-piece rate (0.2) isn't the slowest in the chain (`cut` is the
  slowest at 0.1) &mdash; because it only fires once it has accumulated a
  50-piece batch, its utilization reflects batch wait time, not raw
  processing speed. This is a useful contrast with `examples/cnc-shop/`,
  where the bottleneck station is visibly saturated; here, nothing is.

## What this shows

A floor with enough machine and labor capacity for its order book doesn't
need a capacity decision &mdash; it needs confirmation that it has slack.
Running `examples/spring/model.yaml` at multiple replications (`--reps 20`
or more) is how you'd check that 100% on-time and ~25% utilization hold up
across random variation, not just in one lucky run, before concluding there
is no bottleneck to fix here.

## Sweep the levers

`examples/spring/sweep.json` grids the busiest station's machine count:

```json
{ "locations[stress_relief].capacity": [1, 2, 3] }
```

```bash
twinflow balance examples/spring/model.yaml --plan examples/spring/plan.csv \
  --sweep examples/spring/sweep.json --reps 10
```

This runs the floor at 1, 2, and 3 stress-relief ovens, 10 replications
each. Given the floor is already running at 100% on-time with spare
capacity everywhere, this is a useful null result to confirm: adding
machines to a station that isn't a constraint shouldn't move `on_time_pct`
at all.

## Import into the workspace UI

`spring.twin.yaml` in this folder is the same floor and plan packaged as a
scenario capsule. Choose it in the workspace's **Import scenario** dialog, or
run it directly:

```bash
twinflow scenario validate examples/spring/spring.twin.yaml
twinflow scenario run examples/spring/spring.twin.yaml --seed 42
```

`model.yaml` on its own is not a capsule; the importer says so if you pick it.
