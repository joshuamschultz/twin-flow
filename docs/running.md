# Running a twin and reading the results

This guide takes you from a model file to answers you can act on. It assumes you have a
`model.yaml` and a plan (see [modeling.md](modeling.md)). No prior twinflow knowledge is
needed.

New here? Start with [quickstart.md](quickstart.md) and [concepts.md](concepts.md), then
come back.

## The one idea to hold onto

A real floor is random: cycle times vary, scrap happens, machines break. So **one run is
one lucky (or unlucky) day**, not the truth. twinflow runs your scenario many times
(replications) and reports a **confidence range** (a low-to-high band) for every number.
A single run is a lie; a band is honest. Always run more than one replication.

## Step 1 — validate the model

Check the floor before you simulate it.

```bash
twinflow validate model.yaml
```

- Prints nothing and exits 0 if the model is runnable.
- Prints one line per problem (`path: message`) and exits 1 if not. Fix those first.

## Step 2 — run it

```bash
twinflow run model.yaml --plan plan.csv --reps 30
```

- `--plan` is your production plan (`.csv` or `.xlsx`).
- `--reps` is how many times to simulate. Use **at least 20-30** so the bands are
  meaningful. `--reps 1` runs once and every band collapses to a point (only use it for a
  quick smoke test).

It prints where the run landed:

```
run 2026-08-29T17-03-11Z-9f3a2b1c
  dir:    runs/2026-08-29T17-03-11Z-9f3a2b1c
  report: runs/2026-08-29T17-03-11Z-9f3a2b1c/report.html
  kpis:   runs/2026-08-29T17-03-11Z-9f3a2b1c/kpis.json
  view:   twinflow report 2026-08-29T17-03-11Z-9f3a2b1c --out html
```

## Step 3 — what a run folder contains

Everything about the run lives in `runs/<run-id>/`:

| File | What it is |
|---|---|
| `report.html` | A single self-contained page. Open it in any browser, offline. This is the human view. |
| `kpis.json` | Machine-readable KPIs for the representative run (schema-versioned). |
| `intervals.json` | The confidence bands across all replications (mean + lo/hi per metric). |
| `run_meta.json` | The reproducibility stamp: model/plan hashes, seed, engine + Python versions, dependencies. |
| `events.parquet` | The raw event log the KPIs are computed from. |

`twinflow report <run-id> --out html` re-confirms and locates the report for a finished run.

## Step 4 — read the report

The report opens with **the assumptions the engine had to make**, stated plainly (for
example: no shift calendar, so every pool is always on shift). Read these first — they
tell you what the numbers do and do not account for.

Then the headline card shows **on-time %** as a mean with its low-to-high band, a
value-stream diagram of your floor, and range charts for the key KPIs.

## The KPIs, in plain language

Every number below comes from the one event log. Where a run has more than one
replication, it comes back as a band.

| KPI | What it tells you |
|---|---|
| **On-time %** | Share of orders that finished on or before their due date. The headline. |
| **Completion by order** | When each order actually finished (simulated seconds). `None` means it never finished within the run. |
| **Lateness by order** | Finish minus due date, per order. Negative = early, positive = late. |
| **Utilization by cell / machine** | Share of the run each work center was busy. High = a likely bottleneck. |
| **Wait breakdown** (per center) | Why a center was idle: **starved** (nothing to work on), **blocked** (finished a job but can't hand it off), **material-starved** (waiting on a finite stock). This is how you find the *real* choke point: the bottleneck runs busy while the centers it feeds show high **starved**. |
| **WIP over time** | Work-in-progress at each center across the run — where jobs pile up. |
| **Machine hours / labor hours** | Total busy hours by machine and by pool/skill. |
| **Setup hours** | Changeover time, reported separately from run time. |

## How to read a confidence band

`intervals.json` and the report give each metric five numbers:

```json
"on_time_pct": { "mean": 82.4, "lo": 74.0, "hi": 90.0, "p50": 83.0, "n": 30 }
```

- **mean** — the average across replications. The single number, if you must pick one.
- **lo / hi** — the low and high edge of the band (a percentile range, so a skewed spread
  is shown as it really falls, not assumed symmetric).
- **p50** — the median.
- **n** — how many replications fed the band.

Read it as: *"on-time is about 82%, and on a normal run it lands between 74% and 90%."*
If the band is wide, your floor is volatile and a single promised date is risky. If you
need a number you can defend, quote the **low edge**, not the mean.

## Reading results programmatically

`kpis.json` (representative run) and `intervals.json` (bands) are plain JSON:

```python
import json
kpis = json.load(open("runs/<run-id>/kpis.json"))
bands = json.load(open("runs/<run-id>/intervals.json"))
print(bands["on_time_pct"])          # {"mean":..., "lo":..., "hi":..., "p50":..., "n":...}
print(kpis["machine_hours_by_machine"])
```

## Driving it from Python (the in-process API)

The same operations are callable in your own code — no subprocess, no re-parsing.

```python
from twinflow.model import load_model
from twinflow.plan.loader import load_plan
from twinflow.plan.replication import ReplicationRunner
from twinflow.instrumentation import compute_kpis
from twinflow.instrumentation.aggregate import aggregate_kpis
from twinflow.instrumentation.sweep import orders_frame

model = load_model("model.yaml")                       # parse + validate + compile, once
plan  = load_plan("plan.csv", model.registry)
results = ReplicationRunner("model.yaml").run(plan, reps=30, base_seed=0)

per_rep = [
    compute_kpis(r.event_log_path, orders_frame(plan, model, r.event_log_path), r.horizon)
    for r in results
]
bands = aggregate_kpis(per_rep)                         # mean + lo/hi per KPI
print(bands.on_time_pct)                                # Interval(mean, lo, hi, p50, n)
```

### Comparing two setups fairly

To compare "2 operators vs 3", do not eyeball two overlapping bands. Run both on the same
seeds and get **one** confidence interval on the *difference* (common random numbers make
the comparison fair, not noise):

```python
from twinflow.plan.replication import compare

diff = compare(
    "model_2ops.yaml", "model_3ops.yaml", plan,
    reps=30, base_seed=0,
    metric=lambda result: result.horizon,   # any number pulled from a RunResult
)
print(diff.mean_difference, diff.low, diff.high)   # if the band excludes 0, the difference is real
```

## Reproducibility

Every run carries a stamp (`run_meta.json`): the same model, plan, and seed always produce
the same answer, forever. That is what makes a comparison fair and last quarter's number
trustworthy. Never change the seed to "get a better result" — change the floor.

## What to do next

- Try levers side by side, then let an optimizer search them: [optimizing.md](optimizing.md).
- Make the twin match your real floor: [tuning.md](tuning.md).
- Every command and flag: [cli.md](cli.md). Every KPI's definition also lives in
  [concepts.md](concepts.md).
