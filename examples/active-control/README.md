# Active Control

The twin as a **decision tool**, not just a viewer. This floor exercises all of Phase A's
active-control features at once, each declared purely in `model.yaml`.

| Feature | Where it lives | What it does |
|---|---|---|
| **Dispatch (A1)** | `press.dispatch: edd` | The press runs its queue by earliest due date, not arrival order, so the job most at risk of being late goes first. Swap in `fifo` / `spt` / `critical_ratio` and watch on-time % move. It is a lever (`locations[press].dispatch`), so a sweep can compare policies. |
| **Order release (A2)** | `release: {policy: wip_cap, wip_cap: 5}` | Work is held off the floor until fewer than 5 orders are in the system (CONWIP-style), instead of releasing the whole plan on its start dates. |
| **Breakdown (A4)** | `press.breakdown` | The press fails on a seeded schedule (mean 20 min between failures, ~5 min repair, drawn from the dedicated `SOURCE_BREAKDOWN` stream), so the promised dates survive a bad day. Breakdowns are non-preemptive in this alpha: a failure seizes the machine at the next job boundary and delays what follows. |
| **Rush order (A4)** | `priority` column on `wo-12` | A hot order (`priority: 5`) jumps every work center's queue ahead of normal work. |

## Run it

```bash
twinflow validate examples/active-control/model.yaml
twinflow run examples/active-control/model.yaml --plan examples/active-control/plan.csv --reps 20
```

Because `--reps > 1`, every KPI comes back as a mean with a low-to-high confidence band.
The dispatch rule and WIP cap are ordinary config, so an optimizer can search them:

```bash
twinflow optimize examples/active-control/model.yaml --plan examples/active-control/plan.csv \
  --lever labor.pools[0].headcount:1:4 --objective on_time_pct
```

## What this shows

Everything here is config the Layer-2 compiler turns into pure engine mechanism. The same
four features work on any floor, and each disruption draws from its own seeded stream, so
a run reproduces to the number (common random numbers, D-033).

## Sweep the levers

`examples/active-control/sweep.json` grids the press's machine count against
floor headcount:

```json
{
  "locations[press].capacity": [1, 2],
  "labor.pools[0].headcount": [2, 3]
}
```

```bash
twinflow balance examples/active-control/model.yaml --plan examples/active-control/plan.csv \
  --sweep examples/active-control/sweep.json --reps 10
```

This runs all four combinations (2 capacity values x 2 headcount values), each
at 10 replications, so you can see whether a second press or more hands on
the floor moves `on_time_pct` more — before buying either.

## Import into the workspace UI

`active-control.twin.yaml` in this folder is the same floor and plan packaged as a
scenario capsule. Choose it in the workspace's **Import scenario** dialog, or
run it directly:

```bash
twinflow scenario validate examples/active-control/active-control.twin.yaml
twinflow scenario run examples/active-control/active-control.twin.yaml --seed 42
```

`model.yaml` on its own is not a capsule; the importer says so if you pick it.
