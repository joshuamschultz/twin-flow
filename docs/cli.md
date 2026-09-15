# CLI reference

Every command returns a process exit code (0 = success) and never raises past the
boundary. Run inside your virtualenv (`. .venv/bin/activate`).

## `twinflow validate <model>`

Check a `model.yaml` before running. Prints one line per problem found (path +
message); exit 0 means the model may run.

```bash
twinflow validate examples/cnc-shop/model.yaml
```

## `twinflow run <model> --plan <plan> --reps N`

Simulate `N` replications and write a run folder under `runs/<run-id>/` containing
`report.html`, `kpis.json`, `intervals.json`, and `run_meta.json`, plus the event and
resource tables described in [data contracts](data.md).

```bash
twinflow run examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv --reps 30
```

Use `--reps` greater than 1 to estimate confidence intervals — the report shows a
mean and interval for supported aggregate metrics.

The console summary shows completed orders out of the total, on-time count and percentage,
late count, and makespan in hours. With multiple replications these console figures describe
replication 1 (the representative run), marked `rep 1 of N`; the report's aggregate
intervals use all replications. With `--reps 1`, there is no `rep 1 of 1` note and no
multi-replication interval. If orders remain incomplete, the console prints a warning with
possible causes, including reaching the horizon, quality-gate scrap, or a resource deadlock.

## `twinflow balance <model> --plan <plan> --sweep <sweep.json> --reps N`

Run a declared lever grid and report every point side by side, fairly paired with
common random numbers. The sweep file is plain JSON:

```json
{ "labor.pools[0].headcount": [2, 3, 4] }
```

```bash
twinflow balance examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv \
  --sweep sweep.json --reps 30
```

The command prints and writes `summary.json` in the sweep output directory. Each point
includes its lever values, replication count, mean `on_time_pct`, and mean `busy_hours`
(the legacy sweep's total machine `run_hours`).

## `twinflow report <run-id> --out html`

Confirm (and locate) the `report.html` for an already-completed run.

## `twinflow optimize <model> --plan <plan> --lever PATH:MIN:MAX[:STEP] ...`

Search an integer lever space for the scenario a named objective prefers, via a named
optimizer, and print the winner with its confidence band. Repeat `--lever` for more
than one knob. The twin scores the options; it proposes no commit — the decision stays
yours.

```bash
twinflow optimize examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv \
  --lever labor.pools[0].headcount:2:6 \
  --objective on_time_pct --optimizer hill_climb --budget 12 --reps 12
```

| Flag | Default | Meaning |
|---|---|---|
| `--lever` | (required) | `PATH:MIN:MAX[:STEP]`; a dot-path lever and its integer search range |
| `--objective` | `on_time_pct` | one of the registered objectives (see [modules.md](modules.md)) |
| `--optimizer` | `hill_climb` | `grid`, `random`, `hill_climb`, or `genetic` |
| `--budget` | 12 | maximum real (simulated) evaluations |
| `--reps` | 12 | replications per evaluation (the confidence band) |
| `--seed` | 0 | seeds the optimizer's own randomness |

## `twinflow serve [--host --port --models-root]`

Start the local REST API (needs the `api` extra). Defaults to `127.0.0.1:8000` and
serves the example floors under `examples/`.

```bash
twinflow serve --port 8000
```

See [api.md](api.md) for the endpoints and [frontend.md](frontend.md) for the UI.

The workspace CLI also provides scenario capsule, snapshot/data, and verified scheduling
commands, plus administration commands for local workspace operations. Their supported
flows and boundaries are documented in the [workspace guide](enterprise/README.md) and
[agent integration guide](enterprise/AGENT-GUIDE.md); they use workspace contracts,
separate from the legacy `model.yaml` simulation commands above.
