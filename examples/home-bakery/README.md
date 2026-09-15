# Home bakery worked example

One baker, two ovens, three sourdough breads, two drop dates. This is the twin
used as a **bakery production planner**, and it is a hard test for two reasons.
First, almost all the elapsed time is *waiting*, not working: a 4.5-hour bulk
ferment and a 14-hour cold retard dwarf the ~25 minutes of hands-on work per
loaf. Second, the work has a real **batch-then-split** shape that most floors
do not, and this example is built to model it honestly.

## Loaves move together, then split apart

You do not mix one loaf. You mix a **tub** of dough, six loaves' worth, as a
single lump. That tub moves through mixing and the long bulk ferment as **one
lot**: the loaves travel together because they are literally one mass of dough
in one tub. Only at the **divide** bench does the tub become six separate
loaves, each then shaped, retarded, baked, and cooled on its own.

The model mirrors this exactly:

- **An order is a tub of six loaves.** Every `qty` in `plan.csv` is 6.
- **`scale`, `mix`, and `bulk` each fire once per tub**, on a bundle of six
  loaves moving together. Material and time are charged at tub scale: mixing a
  lump is about five minutes whether it holds four loaves or six, and the flour
  and starter are pulled a tub at a time.
- **`divide` carries `split_output: true`.** It takes the one bulked tub and
  explodes it into six individual loaf bundles. This is the inverse of a batch:
  the tub is processed as one firing, then six loaves leave separately. Every
  loaf keeps its tub's order id, so per-order completion still tracks after the
  split.
- **`shape`, `retard`, `bake`, and `cool` run per loaf**, each competing for
  its own banneton, oven slot, and rack space.

You can see all three behaviors in one run's `events.parquet`: `mix_plain`
fires three times at qty 6 (three tubs), `divide_plain` fires three times at
qty 6, and `bake_plain` fires eighteen times at qty 1 (three tubs, six loaves
each).

## The bakery

- **One baker** (`bakers`, headcount 1, skill `bake`) does every step. A split
  shift runs Mon-Sat **04:00-07:00 and 19:00-20:00**, closed Sunday, timezone
  `America/Chicago`. Simulation second 0 is **Monday 2026-09-14 04:00**. The
  12-hour daytime gap is when dough ferments and shaped loaves cold-retard, all
  unattended while the baker is away. Four operating hours a day is what makes
  the baker the binding constraint.
- **Two ovens**, four loaf slots in total (`oven_m`, capacity 4). Every step's
  machine is shared by all three breads, the same line-A / line-B pattern
  [`examples/foundry`](../foundry/) uses: one scale, one mixer, bulk tubs, one
  bench, the retard fridge, the ovens, the cooling rack.
- **Three breads** (`plain`, `pumpkin`, `blueberry`), each an almost-identical
  chain. Pumpkin and blueberry add one lamination step for inclusions; plain
  skips it. Blueberry, the wettest dough, has a 5% flat-loaf quality gate.

## The flow (per tub, then per loaf)

```
order (tub of 6) -> [scale] -> [mix] -> [bulk ferment 4.5h]          <- loaves together
                 -> [laminate inclusions]*                            <- still one tub
                 -> [DIVIDE: split tub into 6 loaves]                 <- the split
                 -> [shape] -> [cold retard 14h] -> [bake 55m] -> [cool 2h + bag] -> loaf
```

`*` plain skips lamination. Every waiting step is
`shift_crossing: finish_unattended`, so the baker is charged only for loading
and unloading, and the ferment, retard, and bake run overnight while the shift
calendar says the bakery is closed.

## Run it

```bash
twinflow validate examples/home-bakery/model.yaml
twinflow run examples/home-bakery/model.yaml --plan examples/home-bakery/plan.csv --reps 20
twinflow report <run-id> --out html
```

Use `--reps 20` so every KPI comes back as a mean with a low-to-high confidence
band, since every ferment, bake, and hand step varies by about +/-12%.

## Sweep the levers

The binding constraint is the baker, not the ovens. Price a second and third
pair of hands:

```bash
twinflow balance examples/home-bakery/model.yaml \
  --plan examples/home-bakery/plan.csv --sweep examples/home-bakery/sweep.json --reps 10
```

`sweep.json` sweeps `labor.pools[bakers].headcount` across 1, 2, and 3. On-time
delivery climbs steeply with the second baker, which is the whole point: the
ovens and fridge have slack, the person does not.

## Import into the workspace UI

`home-bakery.twin.yaml` in this folder is the same bakery packaged as a
scenario capsule. Choose it in the workspace's **Import scenario** dialog, or
run it directly:

```bash
twinflow scenario validate examples/home-bakery/home-bakery.twin.yaml
twinflow scenario run examples/home-bakery/home-bakery.twin.yaml --seed 42
```

`model.yaml` on its own is not a capsule; the importer says so if you pick it.
