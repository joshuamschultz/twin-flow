<div align="center">

<img src="docs/header.png" alt="twinflow" width="100%">

### A working software copy of your operation that you can run experiments on.

**An agent-accessible operational twin: import a scenario capsule, test bounded alternatives, and retrieve evidence about work, dates, resources, and constraints across manufacturing, office workflows, and supply networks.**

Release `0.4.0-alpha.1` · Python `0.4.0a1` · web `0.4.0-alpha.1`

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-0b2340)](#status--quality)
[![tests](https://img.shields.io/badge/tests-689%20passed-1aa179)](#status--quality)
[![mypy](https://img.shields.io/badge/mypy-strict-2bb5b5)](#status--quality)
[![lint](https://img.shields.io/badge/lint-ruff-46a2f1)](#status--quality)
[![status](https://img.shields.io/badge/status-alpha-f5a623)](#release-scope-and-roadmap)
[![license](https://img.shields.io/badge/license-proprietary-6e7681)](#license)

**[Quick start](#-quick-start)  ·  [Enterprise workspace](#enterprise-build)  ·  [For AI agents](#-for-ai-agents-and-multi-agent-systems)  ·  [Status](#status--quality)**

</div>

---

## Enterprise build

Start with the [workspace user guide](docs/enterprise/README.md) and [agent integration guide](docs/enterprise/AGENT-GUIDE.md). The [roadmap](docs/enterprise-roadmap.md), [build playbook](docs/enterprise-build-playbook.md), and [implementation status](docs/enterprise/BUILD-STATUS.md) distinguish delivered capabilities from the remaining enterprise acceptance gates.

The integrated build adds portable scenario capsules, a shared API/SDK/MCP surface, a responsive operator workspace, manufacturing/office/supply-network adapters, data reconciliation, verified scheduling, and evidence-bound dry-run action review. It is a dedicated-workspace alpha; customer calibration, enterprise identity, distributed execution, and production connector rollout remain explicit next steps.

## The problem

Most plants promise dates and set staffing from a spreadsheet and years of gut feel. That works until it doesn't: a rush order, a new product mix, a machine down, a temp short. A spreadsheet cannot tell you what happens next, and it never tells you how sure it is.

`twinflow` builds a **digital twin** of your operation - a working software copy you can run experiments on. Repeated simulations separate outcome quantiles from confidence intervals on estimated means. Incomplete runs remain visible; a simulated forecast is conditional on its model and data.

> You stop quoting dates you can't hit, and stop adding capacity you don't need.

The legacy manufacturing engine remains configuration-driven. The integrated workspace packages model, snapshot, experiment, assumptions, and provenance in a versioned `.twin.yaml` capsule; a new operation still requires mapping and validation.

---

## What you're stuck on → what twinflow gives you

| The question you're stuck on | What `twinflow` hands back |
|---|---|
| Will these orders complete on time? | Accepted completion dates and on-time %, each with a confidence range |
| Where does the line actually choke? | Utilization per cell and per machine, and a clear wait breakdown: starved vs blocked vs waiting-on-material |
| Is one more operator worth it? | Compare staffing scenarios across replications; workspace results report mean differences, while legacy sweep analysis supports paired intervals |
| How big should the batch or buffer be? | Every option on a lever grid, side by side, fairly compared |
| Can I trust last quarter's number? | Retained inputs, seed, outcomes, and runtime evidence for reproducibility checks |

---

## 🔧 How it works

Four steps, start to finish.

**1. Define the floor** in `model.yaml` - in plain floor language, never code:

```yaml
part_types:
  - { name: wire,  uom: ft }
  - { name: blank, uom: piece }
  - { name: carton, uom: piece }

stocks:
  - { name: wire_stock, thing: wire, uom: ft }

locations:
  - name: cut
    consumes: [{ thing: wire, qty: 10, uom: ft }]
    emits:    [{ thing: blank, uom: piece }]                  # count derived from the recipe
    scrap:    { rate: 0.05, thing: scrap_blank, uom: piece }  # 5% scrap, split automatically
    batch_size: "200 piece"
    time_model: { kind: rate_based, rate: 2.0 }
    machine: cut_01
    labor_skill: operator

  - name: pack
    consumes: [{ thing: blank, qty: 1, uom: piece }]
    emits:    [{ thing: carton, qty: 1, uom: piece }]
    time_model: { kind: rate_based, rate: 5.0 }
    machine: pack_01
    labor_skill: operator

labor:
  pools:
    - { name: line_labor, skills: [operator], headcount: 2 }

routing:
  - { part: carton, steps: [cut, pack] }
```

**2. Provide the plan** as a spreadsheet or CSV (`plan.csv`):

| work_order_id | part   | qty | start_date | due_date |
|---------------|--------|-----|------------|----------|
| WO-1001       | carton | 500 | 0          | 3600     |
| WO-1002       | carton | 250 | 600        | 5400     |

**3. Validate, then simulate.** **4. Read the report.**

You declare *what the floor does* - "5% scrap", "batches of 200", "cut then pack". The engine turns that into the mechanism. You never write a simulation by hand.

---

## 🧱 The building blocks

You model a floor by naming a few kinds of thing in config. Here is the whole vocabulary in plain language. **Legend: ✅ available now.** Every capability below works on any floor, not just the example it appears in.

**Resources - what work competes for**

| Piece | What it is | |
|---|---|---|
| **Machine** | A server inside a work center that does the work and remembers its own setup/changeover state. | ✅ |
| **Labor pool** | A named, skilled group of operators, separate from machines, limited by shift calendars (available-to-work hours). Unload work outranks fresh loads, so machines get freed first. | ✅ |
| **Stock** | A material level you pull from by name (raw metal, wire, fasteners). Blocks work when it is empty and reports the time lost waiting on material. | ✅ |

**What flows, and the operation it flows through**

| Piece | What it is | |
|---|---|---|
| **Bundle** | The unit of flow: a quantity of one part type, in one unit of measure, with attributes. It never changes in place. | ✅ |
| **Part type** | A declared thing with its single unit of measure and its typed attributes (`part_types: [{name, uom}]`). | ✅ |
| **Transform** | The one contract every operation uses: a list of input bundles becomes a list of output bundles. Cutting, pouring, assembly, sorting, yield loss - all the same mechanism, no special cases. | ✅ |
| **Location (work center)** | The floor node that runs an operation. It pulls a job, grabs a machine then an operator, consumes material, charges setup then run time, applies scrap, emits outputs, then releases the operator and the machine - a fixed, auditable order every time. | ✅ |

**Modeling knobs - plain-language config the engine turns into mechanism**

| Knob | What it does | |
|---|---|---|
| **Time model** | How long an operation takes: a named distribution (`lognormal`, `normal`, `triangular`, `uniform`, `exponential`) by `mean` + `cv`, a rate (`qty / rate`), or scaled by an attribute; with an optional load/run/unload split. | ✅ |
| **Variation** | Real floors are variable. Add `cv: 0.2` to any time model, or set `defaults: {cycle_time_cv: 0.2}` once, and every cycle time gets a lognormal spread - so replicated runs report a confidence range instead of a fake-certain number. `0` keeps it deterministic. | ✅ |
| **Scrap** | Declare `scrap: {rate: 0.05, ...}` and the engine makes the good/scrap split for you. Scrap is just an ordinary output bundle. | ✅ |
| **Batching / pull rule** | Take the queue in arrival order by default, or accumulate to a batch threshold in the thing's own unit (`batch_size: "200 piece"`, `"500 lb"`). | ✅ |
| **Setup / changeover** | A changeover matrix: parts in the same setup group run back to back for free; others pay the declared changeover time. | ✅ |
| **Routing** | The ordered list of work centers a part visits (`routing: [{part, steps}]`). | ✅ |
| **Bill of materials** | Rolled up automatically from what each operation consumes. You never hand-write it. | ✅ |
| **Capacity-N work center** | `capacity: N` on a work center gives it N identical machines in parallel; jobs pull whichever is free, so up to N run at once. The report's flow diagram draws it as a stack of N machines. (v1 shares one setup state across the N machines.) See [`examples/cnc-shop-3mill`](examples/cnc-shop-3mill/). | ✅ |

**Four general capabilities** - each is framework-wide, usable by any floor; the [`tier0-foundry`](examples/tier0-foundry/) example composes all four at once.

| Capability | What it lets you model | |
|---|---|---|
| **Reorder-point stock** | A stock that refills itself when it drops below a set level - any consumable or feedstock. Declare `{reorder_point, refill_to}`; a location pulls it with `material: {stock, qty, uom}`. | ✅ |
| **Probabilistic routing / quality gate** | Send whole units down a pass or fail path by chance (inspection, rework), which is different from a fixed scrap rate. Declare `quality_gate: {thing, branches: [{prob, to}]}`; draws from a dedicated seeded stream. | ✅ |
| **Batch / hold operation** | One timed hold over a whole group at once - an oven, a cure, a dry, a cool. Declare `time_model: {kind: batch_hold, seconds: N}` with `batch_size`. | ✅ |
| **Changeover time** | A real setup charged when the next job's setup group differs from the machine's current one. Declare `changeover_seconds: N`; it elapses in simulation and shows in the `setup_hours` KPI. | ✅ |
| **Output-to-stock (recycle / remelt)** | Route scrap or rework back into a named stock so the material re-enters the flow. | ✅ |

Everything above is declared in config. The engine has no per-client code, so the same vocabulary models any discrete floor. See [`docs/modeling.md`](docs/modeling.md) for the full config reference.

---

## Production capability alpha

The `twinflow.production` namespace contains the typed, deterministic capability
contracts for the Western Spring assessment. The [production guide](docs/production/README.md)
covers the public state ledgers and runtime composition; its [validation matrix](docs/production/VALIDATION.md)
maps the ten capability specifications to source and evidence. The rich scheduler is an
earliest-feasible baseline: `FEASIBLE` means the declared constraints were satisfied, and
does not claim certified optimality or empirical plant validation.

The production surface can be consumed after installing the package from source:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
python - <<'PY'
from twinflow.production import contracts, scheduling

print(contracts.Resource, scheduling.solve)
PY
```

The state ledgers preserve order and lot identity across WIP, material reservations,
qualification gates, and outside-processing receipts. Calendars, attendance phases,
thermal recipe batching, and per-order commitments are represented in the typed rich
contract; unsupported or missing plant facts remain explicit in the result. The legacy
`model.yaml` + plan DES CLI remains a separate compatible surface. Repeated legacy
locations that name the same machine now share its capacity and setup state, while rich
schedule verification remains specific to `twinflow.production`.

## 🚀 Quick start

For the integrated workspace, install the service and start the local API:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[api,agent,scheduling,dev]"
python -m twinflow.service.serve --host 127.0.0.1 --port 8000 \
  --models-root examples --workspace-root .twinflow-workspace
```

In another terminal, start the operator UI with `cd web && npm ci && npm run dev`.
Use the [workspace guide](docs/enterprise/README.md) and [agent guide](docs/enterprise/AGENT-GUIDE.md)
for capsule import/export, SDK, MCP, evidence, and operator flows. The commands
below are the supported legacy manufacturing CLI.

Install from source:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Run it from the command line:

```bash
twinflow validate model.yaml                                     # check the floor before running
twinflow run model.yaml --plan plan.csv --reps 30                # simulate 30 replications
twinflow balance model.yaml --plan plan.csv --sweep sweep.json --reps 30   # sweep the levers
twinflow report <run-id> --out html                              # render the emailable report
```

> `--sweep` takes a small **JSON** file: `{"labor.pools[0].headcount": [2, 3, 4]}`.
>
> Run with `--reps` greater than 1 to get the confidence ranges: the report's headline card and its **Confidence ranges** charts then show each KPI's mean and its low-to-high band across the replications.

Or drive it from Python - the same operations, callable in your own code:

```python
from twinflow.model import load_model
from twinflow.plan import load_plan
from twinflow.plan.replication import ReplicationRunner

model = load_model("model.yaml")  # parse + validate + compile, once
plan = load_plan("plan.csv", model.registry)
results = ReplicationRunner("model.yaml").run(plan, reps=30, base_seed=42)
```

---

## ✅ What you get today

The integrated alpha includes the delivered slices listed in the [build status](docs/enterprise/BUILD-STATUS.md). The validation page records integration evidence from the release build; it is not a production-readiness claim. The versioned workspace API is the shared agent/operator boundary; APIs remain pre-1.0.

| Capability | What it means for you |
|---|---|
| **Model supported discrete processes from config** | Stocks, work centers, machines, labor pools, routings, scrap, rework, batching, and setup/changeover are declared for the manufacturing engine. |
| **Plan from Excel or CSV** | The legacy manufacturing engine reads production plans and releases work orders; capsule imports preserve the resulting model and provenance. |
| **Seeded experiments** | Runs record their seed and inputs so comparisons can be repeated under the same engine and dependency conditions. |
| **KPIs from one event log** | Completion dates and on-time %, per-cell and per-machine utilization, a clear wait breakdown (starved / blocked / waiting-on-material), labor and machine hours, setup hours separate from run hours, and work-in-progress over time. |
| **What-if comparisons** | Legacy engine sweeps can use common random numbers for paired analysis; workspace compare reports bounded baseline/candidate differences with recorded assumptions. The twin shows the trade space and deliberately picks **no winner** - the call stays yours. |
| **Replications + uncertainty** | Run many replications in a bounded process pool; outcome quantiles, confidence intervals on estimated means, censoring counts, and paired differences have distinct contracts. |
| **One emailable report** | A single self-contained HTML file that opens offline with no internet, plus a versioned JSON file for machines. Every report opens with the assumptions the engine had to make, stated plainly. |
| **Reproducibility evidence** | Runs record hashes, seeds, engine/runtime metadata, and dependency evidence so a result can be audited and rerun under the recorded conditions. |

---

## 🤖 For AI agents and multi-agent systems

`twinflow` is built to be driven by software, not just by a person clicking. It is a **safe sandbox** where an agentic system can try a decision a thousand times before it ever touches a real worker, planner, or schedule.

- **Deterministic, seeded runs** - every candidate is scored on the same random draws, so comparisons are fair, not noise.
- **In-process run entry point** - callable thousands of times against an already-loaded model, no subprocess or re-parse required.
- **Structured JSON output** - a versioned KPI sidecar an agent can read back without re-simulating.
- **No network, no secrets** - a pure, self-contained sandbox.

> The pattern: **agents propose changes, the twin scores them, and your planners, schedulers, and operators get the recommendation** - with a confidence range attached.

Available today: programmatic runs, sweeps, replications, the JSON KPI contract, a **unified module surface** (propose a scenario → the twin scores it → an objective ranks it) with pluggable optimizers/objectives/cost-functions/models, and a local **REST API** that drives run / sweep / optimize as jobs. See [Modules](#-modules---one-surface-for-optimizers-objectives-costs-and-models) and [`docs/modules.md`](docs/modules.md).

---

## 🧩 Modules - one surface for optimizers, objectives, costs, and models

Real floors need many kinds of optimization, many objective and cost functions, and
many models. `twinflow.modules` gives them **one unified surface** to attach to. The
twin already turns a **scenario** (a set of config lever overrides) into **KPIs with a
confidence band**; every module is a small function over that.

```
ScoringSurface(model, plan)  ──►  Scenario ──► Evaluation (KPIs + band)
       │                                   │                    │
   Optimizer  ──── ranks by ─────────► Objective  ◄── money ── CostFunction
       │                                                        │
   SurrogateModel (learns the surface to screen scenarios cheaply)
```

Each kind lives in its own registry, so adding one is a single `register(name, factory)`
call - never a core edit.

| Kind | Built in today |
|---|---|
| **Objectives** | `on_time_pct`, `robust_on_time` (a confidence-band edge, not the mean), `makespan`, `mean_lateness`, `utilization`, plus weighted blends and cost objectives |
| **Cost functions** | `labor_cost`, `capacity_cost`, `lateness_penalty`, `total_cost` |
| **Optimizers** | `grid`, `random`, `hill_climb`, `genetic` - they *propose*, they never commit |
| **Surrogate models** | `linear`, `nearest_neighbor` - learn the surface, screen scenarios cheaply |

```bash
twinflow optimize examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv \
  --lever labor.pools[0].headcount:2:6 --objective robust_on_time --optimizer hill_climb
```

Full guide, including how to add your own: [`docs/modules.md`](docs/modules.md).

---

## 🖥️ Front end & API

The twin ships with a local **REST API**, Python SDK, MCP adapter, and React
operator workspace. The UI imports and exports capsules, runs bounded jobs,
shows evidence, compares baseline/candidate scenarios, and exposes legacy floor
maps and optimization views.

```bash
pip install -e ".[api]"
twinflow serve --port 8000          # the API
cd web && npm install && npm run dev # the UI (proxies /api to :8000)
```

The API exposes model discovery, a floor graph, tunable levers, module discovery, and
run / sweep / optimize as background jobs the UI polls. See [`docs/api.md`](docs/api.md)
and [`docs/frontend.md`](docs/frontend.md).

---

## 🧭 Three domains in the integrated alpha

> These adapters share the workspace contract and return bounded, JSON-safe evidence. See the [build status](docs/enterprise/BUILD-STATUS.md) for remaining readiness gates.

- **Manufacturing** - the deterministic discrete-event engine, plans, schedules, resources, and seeded experiments. See [`docs/modeling.md`](docs/modeling.md).
- **Office workflows** - cases, documents, approvals, calendars, roles, and bounded rework through the `office` adapter.
- **Supply networks** - stock, receipts, BOMs, qualifications, gates, and bounded lead-time decisions through the `supply_chain` adapter.
- **Active control** - dispatch policies, order release (CONWIP / WIP-cap), and seeded disruptions turn the twin from a viewer into a decision tool. See [`docs/active-control.md`](docs/active-control.md).
- **Optimizers** - *search* the scoring surface for the best staffing / capacity / reorder settings, not just enumerate a grid. See [`docs/optimizing.md`](docs/optimizing.md).
- **Reinforcement-learning surface** - the twin as a Gymnasium-style environment (`TwinEnv`) an RL agent plugs straight into.
- **Integration surfaces** - ERP/MES connectors (a column mapping onto the plan contract), demand generation, and forecaster seams, so concrete SAP / OPC-UA / Prophet integrations plug in. See [`docs/adapters.md`](docs/adapters.md).
- **Calibration** - tune a model until its KPIs match a real run. See [`docs/tuning.md`](docs/tuning.md).

---

## 🏗️ Architecture

A five-layer design. Lower layers never import higher ones. Layers 0 through 2 never change per client - only config does.

| Layer | Package | Owns |
|---|---|---|
| 0 · Engine | `twinflow.engine` | SimPy clock, deadlock-safe acquisition, per-source random streams |
| 1 · Primitives | `twinflow.primitives` | Bundle, Stock, Location, Machine, Transform, TimeModel, LaborPool |
| 2 · Model | `twinflow.model` | `model.yaml` → validated routing graph + BOM; the only YAML and expression trust boundary |
| 3 · Plan | `twinflow.plan` | Work orders, release timing, initial WIP, the run driver, replications |
| 4 · Instrumentation / Report | `twinflow.instrumentation`, `twinflow.report` | Event log → KPIs → sweeps; self-contained HTML + JSON report |
| 5 · Modules / Service | `twinflow.modules`, `twinflow.service` | Unified scoring surface (objectives, costs, optimizers, models); optional REST API |

**The core rule:** a new client is data, never code. A capability a floor needs is added to the engine for everyone, never subclassed per client.

Deep docs: **operating guides for every feature and build live in [`docs/`](docs/)** (install, CLI, modeling, modules, API, front end, quickstart). Release history and migration notes are in [`CHANGELOG.md`](CHANGELOG.md).

---

## Status & quality

- **Python** ≥ 3.11.
- **689 tests passed** across unit, analytical, integration, enterprise, boundary, and
  production capability layers in the release validation run.
- **Blocking CI gates:** `ruff`, `mypy --strict`, `pytest`, `pip-audit` - the build fails on any finding.
- **Layered validation:** YAML is loaded through one safe door, expressions run in one sandbox, and workspace/API boundaries enforce request, capability, evidence, and action limits before work starts.
- **Reproducibility evidence:** every run carries a stamp describing its inputs, seed, runtime, and dependencies for audit and rerun under recorded conditions.

> Status: **alpha.** Supported engine, policy, domain, scheduling, and integration slices are tested. Consult the enterprise build status for specific limitations and production acceptance gates.

---

## Release scope and roadmap

Release `0.4.0-alpha.1` is an integrated local workspace plus the typed production
capability alpha. Delivered slices and
their remaining acceptance gates are maintained in [enterprise build status](docs/enterprise/BUILD-STATUS.md)
and the [validation record](docs/enterprise/VALIDATION.md). The [enterprise
roadmap](docs/enterprise-roadmap.md) describes proposed product direction; it
is not a promise that every roadmap item is in this release.

The deterministic simulation core is separate from host-agent reasoning. Twinflow
has no built-in LLM: an agent may retrieve source facts, construct a capsule,
branch it, and ask Twinflow to validate and evaluate bounded experiments. The
service records assumptions and evidence, while operators decide what to act on.

---

## License

Proprietary. © Joshua Schultz. All rights reserved.
