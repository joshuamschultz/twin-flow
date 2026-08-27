# CNC Shop worked example

A machine shop floor expressed purely as config (no Python): two part
families run raw stock through saw -> CNC mill -> deburr -> inspect (scrap
rate) -> finished.

- **`shaft`** — `round_bar` (ft, continuous stock) -> `saw_shaft` (4 blanks
  per foot) -> `mill_shaft` -> `deburr_shaft` -> `inspect_shaft` (3% scrap)
  -> `shaft`.
- **`bracket`** — `square_billet` (already-discrete pieces) -> `saw_bracket`
  -> `mill_bracket` -> `deburr_bracket` -> `inspect_bracket` (5% scrap) ->
  `bracket`.

Each family gets its own location at every step, each with a distinct
`setup_key` (`grp_saw_shaft` vs `grp_saw_bracket`, `grp_mill_shaft` vs
`grp_mill_bracket`, ...). Labor: `machinist` runs saw + mill, `inspector`
runs deburr + inspect, one shared `floor_pool`.

## Run it

```bash
twinflow validate examples/cnc-shop/model.yaml
twinflow run examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv --reps 1
twinflow report <run-id> --out html
```

`plan.csv` is a 10-order book (5 shaft, 5 bracket) with a tight release
cadence and tight due dates, so both families are in flight and being routed
through their own chains concurrently.

## What this shows

The CNC mill (`mill_shaft` / `mill_bracket`) is tuned far slower than every
other step on the floor — `rate: 0.03` piece/s versus `0.5-2.0` piece/s for
saw, deburr, and inspect. Orders release close together (every 100-300
simulated seconds), faster than the mill can clear its queue, so work piles
up in front of the mill while the rest of the floor sits mostly idle.

No location declares `batch_size`, so every work center pulls the default
way: one job at a time, in arrival order (`PullRule`'s default — batching
is opt-in, never automatic). That means a real queue forms in front of the
mill instead of being silently absorbed into bigger batches — the report's
wait-time breakdown, not its firing counts, is where that backlog shows up
(see below).

## What to look for

Run the example and open `kpis.json` / `report.html` (and recompute
`utilization_by_cell` / the wait-time breakdown via `KpiEngine`, since
today's `kpis.json` sidecar doesn't carry those two fields yet — see
`tests/integration/test_cnc_shop_example.py`):

- **`utilization_by_cell`** — `mill_shaft` runs at ~99% busy and
  `mill_bracket` at ~85%, both far above every other station on the floor
  (all under 2%). The mill is the visibly busiest resource on the floor,
  full stop, and `mill_shaft`'s ~99% is close to a hard ceiling: a
  work center that pulls one job at a time is never more than ~100% busy
  (`utilization_by_cell` for a serialized center is always <= 100%).
- **The wait-time breakdown (`starved` / `blocked`)** — this is a
  per-work-CENTER view, not a per-job queue view: `starved` is time a
  center spent idle waiting for its next job, `blocked` is time it sat
  finished but held. The mill shows almost no `starved` time (it's busy) —
  `mill_shaft` starves for well under 1% of the run — while `deburr_shaft`,
  immediately downstream and fed only by the mill, starves for the vast
  majority of the run (it has nothing to do until the mill's backlog
  reaches it). That contrast — a busy bottleneck feeding starving
  downstream stations — is the report's bottleneck fingerprint. Firing
  counts do **not** show this: every location, including the mill, fires
  exactly once per order (10 releases each), because the default pull rule
  never batches. The backlog lives entirely in queue wait time, not in
  fewer/bigger firings.
- **`on_time_pct` / `lateness_by_order`** — on-time performance lands at
  10%: nine of the ten work orders finish late (`lateness_by_order` > 0).
  Every order in a family completes when the LAST unit of that family
  clears the floor (a documented v1 simplification: completion is tracked
  per terminal lot, not per individual work order — see `orders_frame` in
  `instrumentation/sweep.py`), so the earliest-due order in each family
  (promised against the mill's still-empty backlog) reads as the *most*
  late, while the last, loosest-due-date order can land on time.

## The decision this points to

This is not a scheduling problem — every other station has slack. It is a
**capacity** problem: the CNC mill is the floor's constraint. A planner
looking at this report has one clear lever: add mill capacity (a second
machine, or a shift) rather than push harder on saw, deburr, or inspect,
which are already nowhere near their limit. Today's schema has no
`capacity: N` field to express "a second mill" directly — see "Roadmap
note" below.

## Roadmap note: parallel mill capacity

Today's schema is one work center (one machine) per `locations[*]` entry —
there is no `capacity: N` field to model "two mills running in parallel."
Splitting demand across two hand-written mill locations would silently
change each family's setup/fixture identity (`setup_key`) and isn't a
faithful stand-in for shared-machine pooling. Modeling "add a second mill"
as a first-class capacity bump (rather than a same-part duplicate location)
is a capability in progress on the roadmap, not something this example
fakes today.

## Design note: why two locations per step, not one shared "mill"

`LocationCompiler` (`model/compile.py`) assigns exactly one `setup_key`
string to a *location*, applied uniformly to every `thing` it consumes. A
single location also owns exactly one recipe (`consumes[0]` is the fixed
ratio basis) — so one YAML location cannot represent "the CNC mill, which
sometimes runs shafts and sometimes brackets" while keeping each family's
part identity distinct through to its own finished part (`routing[*].part`'s
last step must emit that specific finished thing). The schema-correct
expression of "two families sharing a work-center shape but never a setup
group" is two locations, one per family, each singly-capacitied — which is
also what the task brief asked for ("keep each step a single work center;
parallel/shared machines are a separate future capability").

## Gap found, not fixed (per task instructions)

`LocationCompiler._compile_location` hardcodes the changeover cost:

```python
setup_policy = SetupPolicy(setup_key_of=setup_key_of, changeover_matrix={}, default_seconds=0.0)
```

There is no `model.yaml` field today that sets a nonzero `default_seconds`
or a `changeover_matrix` entry. `setup_key` is real and load-bearing for
`PullRule` eligibility (which bundles a location may batch together — see
`primitives/location.py::PullRule.select`), but it currently contributes
**zero** simulated changeover time regardless of value. This example uses
`setup_key` exactly as the schema defines it today (grouping/eligibility),
and documents the gap here rather than inventing a new YAML field or
changing `compile.py`/`SetupPolicy` to add one.
