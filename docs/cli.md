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
`report.html`, `kpis.json`, `intervals.json`, and `run_meta.json`.

```bash
twinflow run examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv --reps 30
```

Use `--reps` greater than 1 to get confidence ranges — the report's headline and its
range charts then show each KPI's mean and its low-to-high band.

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
