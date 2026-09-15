# Examples index

Every example folder below is self-contained and has its own README with
copy-paste commands. This page is the map.

## Manufacturing floors (legacy `model.yaml` + `plan.csv`)

| Folder | Domain / decision | Run it |
|---|---|---|
| [`active-control/`](active-control/) | Dispatch rules, WIP caps, breakdowns, and rush orders, all as config | `twinflow run examples/active-control/model.yaml --plan examples/active-control/plan.csv --reps 20` |
| [`cnc-shop/`](cnc-shop/) | A machine shop with one slow CNC mill as the floor's bottleneck | `twinflow run examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv --reps 1` |
| [`cnc-shop-3mill/`](cnc-shop-3mill/) | Same shop, 3 mills instead of 1 &mdash; the "buy more machines?" decision | `twinflow run examples/cnc-shop-3mill/model.yaml --plan examples/cnc-shop-3mill/plan.csv --reps 30` |
| [`foundry/`](foundry/) | Two sand-casting lines sharing raw metal feedstock and a scrap-recycle loop | `twinflow run examples/foundry/model.yaml --plan examples/foundry/plan.csv --reps 1` |
| [`hmlv-calendar/`](hmlv-calendar/) | A labor calendar (closed dates, shift-end pauses) and a two-machine center | `twinflow run examples/hmlv-calendar/model.yaml --plan examples/hmlv-calendar/plan.csv --reps 1` |
| [`home-bakery/`](home-bakery/) | A one-person sourdough bakery: tubs of dough that mix and ferment together, then a `divide` step that splits each tub into individual loaves (`split_output`), shared ovens and fridge, two drop dates &mdash; the "can I promise this?" decision | `twinflow run examples/home-bakery/model.yaml --plan examples/home-bakery/plan.csv --reps 1` |
| [`led-manufacturer/`](led-manufacturer/) | A labor shortage (one assembler), not a machine bottleneck &mdash; the "hire?" decision | `twinflow run examples/led-manufacturer/model.yaml --plan examples/led-manufacturer/plan.csv --reps 1` |
| [`spring/`](spring/) | A well-balanced floor with spare capacity everywhere and 100% on-time | `twinflow run examples/spring/model.yaml --plan examples/spring/plan.csv --reps 1` |
| [`supply-chain/`](supply-chain/) | A floor fed by a two-echelon inventory chain with real supplier lead times | `twinflow run examples/supply-chain/model.yaml --plan examples/supply-chain/plan.csv --reps 20` |
| [`tier0-foundry/`](tier0-foundry/) | Changeover time, reorder-point material, batch hold, and a quality gate, all at once | `twinflow run examples/tier0-foundry/model.yaml --plan examples/tier0-foundry/plan.csv --reps 20` |

Every floor above also ships a ready-made capsule, `<folder>/<folder>.twin.yaml`
(for example `spring/spring.twin.yaml`). That is the file to pick in the
workspace UI's **Import scenario** dialog. A bare `model.yaml` is a floor
model, not a capsule, and the importer rejects it with a message that says
so. To build a capsule from your own `model.yaml` + `plan.csv`:

```bash
twinflow scenario import model.yaml plan.csv --out model.twin.yaml
```

Every floor above also ships a `sweep.json` so you can run
`twinflow balance <model> --plan <plan> --sweep <name>/sweep.json --reps 10`
and see its key capacity or staffing lever compared side by side &mdash; see
each folder's own README for the exact lever and what it's testing.

## Scenario capsules

| Folder | Domain / decision | Run it |
|---|---|---|
| [`capsules/`](capsules/) | Four `.twin.yaml` capsules (office workflow, two supply-chain networks, one manufacturing floor) bundling model + snapshot + experiment + assumptions + provenance in one portable file | `twinflow scenario run examples/capsules/office.twin.yaml --seed 42` |

## Scheduling

| Folder | Domain / decision | Run it |
|---|---|---|
| [`scheduling/`](scheduling/) | A hard-constraint job-shop problem (precedence, resource windows, qualifications) | `twinflow schedule examples/scheduling/problem.json --solver baseline --time-limit 10` |

## Supply network

| Folder | Domain / decision | Run it |
|---|---|---|
| [`supply-chain-network/`](supply-chain-network/) | Material-feasible delivery dates across suppliers, BOMs, processes, and evidence gates (manufacturing + distribution profiles) | Python: `twinflow.domain.supply_chain.evaluate(model, snapshot, seed=42, ...)`, or import `manufacturing.twin.yaml` / `distribution.twin.yaml` in the UI |

## Operational data

| Folder | Domain / decision | Run it |
|---|---|---|
| [`data/`](data/) | Reconciling raw events into point-in-time snapshots, and scoring dated forecasts against actuals | `twinflow data import --db /tmp/ops.db examples/data/operational-events.jsonl` |

## Two ways to run any of this

**CLI** &mdash; every command above works straight from a terminal, as
documented in each folder's README.

**Workspace UI** &mdash; start the local API, then the web app:

```bash
python -m twinflow.service.serve --host 127.0.0.1 --port 8000 \
  --models-root examples --workspace-root .twinflow-workspace
```

```bash
cd web && npm run dev
```

The manufacturing floors above appear as example cards named after their
folder (e.g. "Cnc Shop"). Three of the four scenario capsules appear as
"Office", "Supply Manufacturing", and "Supply Distribution" &mdash; see
[`capsules/README.md`](capsules/README.md) for why the fourth
(`spring.twin.yaml`) is intentionally left out.

If port 8000 is already in use by something else on your machine, start the
API on a different port and point the web dev server at it:

```bash
python -m twinflow.service.serve --host 127.0.0.1 --port 8010 \
  --models-root examples --workspace-root .twinflow-workspace
```

```bash
TWINFLOW_API_TARGET=http://127.0.0.1:8010 npm run dev
```

`web/vite.config.ts` reads `TWINFLOW_API_TARGET` (defaulting to
`http://127.0.0.1:8000`) and proxies every `/api` call there; pointing it at
the wrong port is why the example cards would otherwise fail to load.
