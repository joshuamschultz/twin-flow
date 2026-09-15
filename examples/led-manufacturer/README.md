# LED manufacturer worked example

A made-to-order LED light maker, expressed as pure `twinflow` config (no
Python): `model.yaml` + `plan.csv`.

## What this shows: a LABOR SHORTAGE, not a machine bottleneck

This example is tuned so its report reads as a **hire decision**. Two
labor pools are declared (`model.yaml`'s `labor.pools`):

- `assembler_pool` — headcount **1**, skill `asm_op`. This is the ONLY pool
  that can staff any `assemble_*` station, and all three size variants
  (small/medium/large) share it — one human hand-builds every light in the
  plant, one at a time.
- `support_pool` — headcount 6, skills `cut_op`/`qa_op`/`pack_op`. Deliberately
  ample: cut/burn-in/pack are fast and never queue on an operator, so
  nothing but the assembler is ever the constraint.

The MACHINES are not the problem. Every `cut_*`, `burnin_*`, and `pack_*`
station is fast and dedicated per size — plenty of spare capacity. The
`assemble_*` stations themselves are not machine-starved either; they sit
idle waiting for the ONE assembler to become free, not waiting for
material or a slow machine.

## What to look for in the KPIs

Since the engine now has a work center pull ONE job at a time by default
and the wait breakdown is per-CENTER idle time (a starved center is one
sitting idle waiting for work, not "the bottleneck"), this shows up
cleanly:

- **`labor_pool_utilization`** (the KEY labor metric — busy operator-seconds
  / (horizon × headcount)) is essentially saturated: the single assembler is
  busy almost the entire horizon, because all three assemble stations
  compete for that one person.
- **`utilization_by_cell`** for every machine — `assemble_*` included — sits
  well below the labor number. The machines have headroom; the person does
  not.
- **`on_time_pct`** is low: most of the ten released work orders miss their
  due date, because the plan releases orders on a tighter cadence than one
  assembler can clear.

Verified from a real in-process run (`RunDriver` + `KpiEngine`, see
`tests/integration/test_led_manufacturer_example.py::test_labor_pool_is_the_constraint_the_machines_have_headroom`
for the exact numbers this asserts):

```
labor_pool_utilization["default"]  ~1.01   (>= 0.8 -- saturated)
max(utilization_by_cell.values())  ~0.38   (assemble_large -- clearly lower)
on_time_pct                         0.0    (all 10 orders late)
```

## The fix: hire another assembler — demonstrable today

This is the satisfying part: the fix is a one-line config change, not a
model rewrite. Raise `assembler_pool`'s `headcount` from 1 to 2 in
`model.yaml` and re-run:

```bash
twinflow run examples/led-manufacturer/model.yaml \
  --plan examples/led-manufacturer/plan.csv --reps 1
```

With `headcount: 2`, the same plan finishes in roughly half the simulated
time, `on_time_pct` jumps from 0% to ~60%, and `labor_pool_utilization`
drops from saturated (~1.01, with `labor_pool_capacity=1`) to ~0.91 (with
`labor_pool_capacity=2`) — still tight, but no longer hopeless. One hire,
measured, before you spend the money.

## The chain

Per size variant (small / medium / large):

```
diffuser_sheet (ft, continuous roll)
  -> CUT            [continuous-to-discrete: sheet ft -> panel pieces + leftover-sheet remainder]
  -> ASSEMBLE        [panel + led_strip + driver + housing -> one light: several
                       component bundles feed ONE emit]
  -> BURN-IN          [test/burn-in]
  -> PACK             [-> finished packed light]
```

- **CUT** is the continuous-to-discrete signature step, mirroring
  `examples/spring/`'s wire cut: 1 ft of diffuser sheet (the ratio basis)
  yields a size-specific panel COUNT plus a leftover-sheet remainder, both
  plain `emits` ratios against the same `consumes[0]` basis — no scrap
  block, no hand-written emit expression (D-043/D-044).
- **ASSEMBLE** is the assembly signature step, mirroring
  `tests/unit/test_compile.py`'s `assembler` case: FOUR `consumes` entries
  (panel, led_strip, driver, housing) feed ONE `emits` entry (the light).
  `consumes[0]` (the panel) is the declared ratio basis: 1 panel -> 1 light.

## Made-to-order sizing — a real schema gap (STOP-AND-REPORT)

The brief asked for each work order to carry its own custom light
**dimensions**, which would set exactly how much panel and LED strip that
order's light needs. **`plan.csv`'s committed schema today is
`work_order_id, part, qty, start_date, due_date` — there is no per-order
size/dimension column.** A truly continuous "8.2in x 3.4in" custom
dimension per order cannot be expressed with the current plan loader
(`src/twinflow/plan/loader.py`'s `REQUIRED_COLUMNS`).

This example works around that by modeling made-to-order sizing as **three
discrete SIZE VARIANTS as distinct part types** — `packed_light_small`,
`packed_light_medium`, `packed_light_large` — each with its own panel/strip
consumption ratio compiled into its own `cut_*`/`assemble_*` location pair.
`plan.csv` mixes all three variants across ten work orders to stand in for
"made to order." This is a legitimate modeling technique (a client with a
handful of standard sizes could ship exactly this), but it is **not** the
same as true continuous per-order dimensions.

**What a real fix would need** (not implemented here — a candidate future
capability, flagged for the orchestrator/product owner):

- A plan-schema extension carrying per-work-order attributes, e.g. optional
  `panel_length_ft` / `panel_width_ft` (or a generic `attributes` JSON/dict
  column) alongside the existing five required columns.
- `plan/loader.py` would need to parse and validate those columns per row
  (today `WorkOrder` has no such field).
- The compiled `Transform`'s output-qty callables (`model/compile.py`)
  would need a variant that scales `emits[i].qty` off a **per-order runtime
  attribute** rather than purely off `consumes[0]`'s actual qty — a
  different sugar than any of `rate_based` / `distribution` /
  `attribute_scaled` handles today, since the scaling factor would need to
  flow from the *work order*, not from the part's own declared attributes
  or the basis bundle's qty.
- This is a `model/compile.py` + `plan/loader.py` change, not something
  expressible in `model.yaml`/`plan.csv` alone — out of scope for this
  example, which is config-only.

## A second, smaller finding: the live PullRule engine does not gate a
## multi-consumes firing on "wait for one of everything"

Verified by running the model for real (`twinflow run`, inspected
`events.parquet`): at each `assemble_*` location, the three directly-BOM-
released raw materials (`led_strip`, `driver`, `housing_*`) arrive
instantly at the order's `start_date`, while the panel arrives later (after
`cut_*` actually processes it). `PullRule.select()` (`primitives/location.py`)
fires on **any** eligible arrival — there is no "wait until one bundle of
every declared `consumes[*].thing` is present" mode, and a declared
`batch_size` only **caps** a firing's size, it never blocks below its
threshold. The observed, real result: **one wasted zero-output firing**
per order (consumes led_strip+driver+housing while the panel is
away/still cutting, emits a `qty=0` light that cascades a `qty=0` row
through burn-in and pack too) plus **one correct firing** (panel alone,
correct light qty). Confirmed the *demanded totals still come out right*
(`pack_small`/`pack_medium`/`pack_large` total qty in the event log exactly
matches `plan.csv`'s summed demand per size), so this does not corrupt the
example's KPIs, but it does add event-log noise and burns a sliver of
otherwise-idle machine/labor time on a zero-value phantom lot every order.
Fixing this for real would need an engine-level PullRule mode (or a
`material_requirement`-style Stock-gated multi-pull) that can wait for a
complete component set before firing — a `primitives/location.py` change,
out of scope here (config-only example, no src edits).

## Running it

```bash
twinflow validate examples/led-manufacturer/model.yaml
twinflow run examples/led-manufacturer/model.yaml --plan examples/led-manufacturer/plan.csv --reps 1
twinflow report <run-id> --out html
```

## Sweep the levers

The "labor shortage, not a machine bottleneck" thesis above rests on the
single assembler in `assembler_pool` (headcount 1).
`examples/led-manufacturer/sweep.json` grids that headcount directly:

```json
{ "labor.pools[0].headcount": [1, 2, 3] }
```

```bash
twinflow balance examples/led-manufacturer/model.yaml --plan examples/led-manufacturer/plan.csv \
  --sweep examples/led-manufacturer/sweep.json --reps 10
```

This runs the floor with 1, 2, and 3 assemblers, 10 replications each, so you
can check the hire decision this example points to: does adding a second
assembler actually move `on_time_pct`, or does the constraint move somewhere
else first?

## Import into the workspace UI

`led-manufacturer.twin.yaml` in this folder is the same floor and plan packaged as a
scenario capsule. Choose it in the workspace's **Import scenario** dialog, or
run it directly:

```bash
twinflow scenario validate examples/led-manufacturer/led-manufacturer.twin.yaml
twinflow scenario run examples/led-manufacturer/led-manufacturer.twin.yaml --seed 42
```

`model.yaml` on its own is not a capsule; the importer says so if you pick it.
