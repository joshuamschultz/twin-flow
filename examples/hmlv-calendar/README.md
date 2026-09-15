# HMLV calendar and independent machines

This runnable example declares a timezone-aware weekday labor calendar, one closed
date, paused work at shift end, and a two-machine center. Run it with:

```bash
twinflow run examples/hmlv-calendar/model.yaml \
  --plan examples/hmlv-calendar/plan.csv --reps 1
```

The run's `resource_usage.parquet` names `mill-1` and `mill-2`. Both start cold and
therefore pay their own 600-second setup before processing in parallel.

## Sweep the levers

`examples/hmlv-calendar/sweep.json` grids the mill's machine count around
this example's own starting point of 2:

```json
{ "locations[mill].capacity": [1, 2, 3] }
```

```bash
twinflow balance examples/hmlv-calendar/model.yaml --plan examples/hmlv-calendar/plan.csv \
  --sweep examples/hmlv-calendar/sweep.json --reps 10
```

This runs the floor at 1, 2, and 3 mills, 10 replications each, so you can
see how much of the calendar's lost time (closed date, shift-end pauses,
per-machine cold-start setup) a third machine buys back versus just running
two.

## Import into the workspace UI

`hmlv-calendar.twin.yaml` in this folder is the same floor and plan packaged as a
scenario capsule. Choose it in the workspace's **Import scenario** dialog, or
run it directly:

```bash
twinflow scenario validate examples/hmlv-calendar/hmlv-calendar.twin.yaml
twinflow scenario run examples/hmlv-calendar/hmlv-calendar.twin.yaml --seed 42
```

`model.yaml` on its own is not a capsule; the importer says so if you pick it.
