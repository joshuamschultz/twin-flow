# REST API

An optional local service (`twinflow.service`) the front end drives. It never lets a
request read a file outside the models root: `model` in every request names a
discovered example, never a free path.

## Start it

```bash
pip install -e ".[api]"
twinflow serve --host 127.0.0.1 --port 8000 --models-root examples
# or: python -m twinflow.service.serve --port 8000
```

## Endpoints

### Discovery

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{status:"ok"}` |
| GET | `/api/modules` | `{objectives, costs, optimizers, models}` — the registered plugin names |
| GET | `/api/models` | `[{name, has_plan}]` — the example floors |

### The visual model

| Method | Path | Returns |
|---|---|---|
| GET | `/api/models/{name}/floor` | `{nodes, edges, parts}` — the floor graph |
| GET | `/api/models/{name}/levers` | `[{path, label, current, min, max}]` — tunable knobs |

A floor node is `{id, kind:"location"|"stock", label, ...}`; a location node carries
`capacity`, `labor_skill`, `machine`, `time_model`; a stock node carries `uom`. An edge
is `{source, target, label}` (the label is the part or the stock feeding a center).

### Work (background jobs)

Each returns a job immediately: `{id, kind, status:"running", result:null, error:null}`.

| Method | Path | Body |
|---|---|---|
| POST | `/api/run` | `{model, reps}` |
| POST | `/api/sweep` | `{model, sweep:{path:[values]}, reps}` |
| POST | `/api/optimize` | `{model, space:{path:{min,max,step?}|{choices}}, objective, optimizer, budget, reps, seed?}` |

### Polling

| Method | Path | Returns |
|---|---|---|
| GET | `/api/jobs` | every job |
| GET | `/api/jobs/{id}` | `{id, kind, status, result, error}` |

Poll `/api/jobs/{id}` (about every 1.5 s) until `status != "running"`. Then:

- **run** result: `{kpis:{...}, intervals:{...}}`
- **sweep** result: `{points:[{levers, kpis, intervals}]}`
- **optimize** result: `{objective, direction, best:{levers, kpis, intervals}, best_score, evaluations_used, history:[{levers, score}]}`

An `intervals` block carries a confidence band per metric, e.g.
`on_time_pct: {mean, lo, hi, p50, n}`. `on_time_pct` is a 0–100 percentage;
`utilization_by_cell` values are 0–1 fractions.

## Example

```bash
curl -s localhost:8000/api/modules
curl -s localhost:8000/api/models/cnc-shop/floor
JOB=$(curl -s -X POST localhost:8000/api/run \
  -H 'content-type: application/json' \
  -d '{"model":"cnc-shop","reps":20}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s localhost:8000/api/jobs/$JOB      # repeat until status == "done"
```

## Notes

- The job store is in-memory and single-process — a local alpha driver for one
  operator's twin, not a multi-tenant service. Restarting the API forgets finished
  jobs; the `runs/` artifacts on disk are the durable record.
- Interactive API docs are available at `/docs` (FastAPI's built-in Swagger UI) while
  the server runs.
