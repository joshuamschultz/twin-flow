<div align="center">

<img src="docs/header.png" alt="twinflow" width="100%">

### A working software copy of your operation that you can run experiments on.

**Describe your factory floor in a spreadsheet and a config file. Get back honest answers about dates, bottlenecks, and staffing - each with a confidence range, not a guess.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-0b2340)](#status--quality)
[![tests](https://img.shields.io/badge/tests-507%20passing-1aa179)](#status--quality)
[![mypy](https://img.shields.io/badge/mypy-strict-2bb5b5)](#status--quality)
[![lint](https://img.shields.io/badge/lint-ruff-46a2f1)](#status--quality)
[![status](https://img.shields.io/badge/status-alpha-f5a623)](#roadmap)
[![license](https://img.shields.io/badge/license-proprietary-6e7681)](#license)

**[Quick start](#-quick-start)  ·  [How it works](#-how-it-works)  ·  [For AI agents](#-for-ai-agents-and-multi-agent-systems)  ·  [Roadmap](#roadmap)**

</div>

---

## The problem

Most plants promise dates and set staffing from a spreadsheet and years of gut feel. That works until it doesn't: a rush order, a new product mix, a machine down, a temp short. A spreadsheet cannot tell you what happens next, and it never tells you how sure it is.

`twinflow` builds a **digital twin** of your operation - a working software copy you can run experiments on. Because real floors are random (cycle times vary, scrap happens), it simulates each scenario many times and reports a **confidence interval**, a low-to-high range you can trust, instead of a single number pretending to be certain.

> You stop quoting dates you can't hit, and stop adding capacity you don't need.

No custom code per plant. **One `model.yaml` plus a production plan (`.xlsx` or `.csv`) is a complete client.**

---

## What you're stuck on → what twinflow gives you

| The question you're stuck on | What `twinflow` hands back |
|---|---|
| Will these orders ship on time? | Completion dates and on-time %, each with a confidence range |
| Where does the line actually choke? | Utilization per cell and per machine, and a clear wait breakdown: starved vs blocked vs waiting-on-material |
| Is one more operator worth it? | Run each staffing level many times and compare them with a paired confidence interval on the difference |
| How big should the batch or buffer be? | Every option on a lever grid, side by side, fairly compared |
| Can I trust last quarter's number? | A reproducibility stamp on every run: same inputs, same answer, forever |

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

## 🚀 Quick start

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

model = load_model("model.yaml")               # parse + validate + compile, once
plan  = load_plan("plan.csv", model.registry)
results = ReplicationRunner("model.yaml").run(plan, reps=30, base_seed=42)
```

---

## ✅ What you get today

Everything here is **built and tested** (507 passing tests). The library API is stable; the `twinflow` command surfaces the same operations.

| Capability | What it means for you |
|---|---|
| **Model any discrete floor from config** | Stocks, work centers, machines, labor pools, routings, scrap, rework, batching, and setup/changeover - all declared, never coded. One contract covers cutting, pouring, assembly, sorting, and yield loss. |
| **Plan from Excel or CSV** | Your production plan drives order releases and any work already on the floor. Hand over the spreadsheet you already keep. |
| **Reproducible, seeded runs** | Every run is repeatable to the number. Same inputs, same answer - which is what makes fair comparison possible. |
| **KPIs from one event log** | Completion dates and on-time %, per-cell and per-machine utilization, a clear wait breakdown (starved / blocked / waiting-on-material), labor and machine hours, setup hours separate from run hours, and work-in-progress over time. |
| **What-if lever sweeps** | Try staffing, buffers, and batch sizes across a grid and see every option side by side, fairly paired with common random numbers. The twin shows the trade space and deliberately picks **no winner** - the call stays yours. |
| **Replications + uncertainty** | Run many replications in a bounded process pool; outcome quantiles, confidence intervals on estimated means, censoring counts, and paired differences have distinct contracts. |
| **One emailable report** | A single self-contained HTML file that opens offline with no internet, plus a versioned JSON file for machines. Every report opens with the assumptions the engine had to make, stated plainly. |
| **Full reproducibility stamp** | Each run records hashes of the model and plan, the seed, the engine version, the Python version, and the exact dependency set. A result can always be traced back and re-created. |

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

The twin ships with a local **REST API** and a **React front end** to map the floor, run
it, sweep a lever, and optimize - all in the browser, with the confidence band as the
central visual.

```bash
pip install -e ".[api]"
twinflow serve --port 8000          # the API
cd web && npm install && npm run dev # the UI (proxies /api to :8000)
```

The API exposes model discovery, a floor graph, tunable levers, module discovery, and
run / sweep / optimize as background jobs the UI polls. See [`docs/api.md`](docs/api.md)
and [`docs/frontend.md`](docs/frontend.md).

---

## 🧭 Beyond the single floor (shipped in alpha)

> These extend the same one-contract engine rather than replacing it. All are built and tested today; the [Roadmap](#roadmap) covers what is still direction.

- **Supply-chain twin** - orders, finite stocks, reorder points, supplier lead time, and multi-echelon inventory, with inventory KPIs and economics you can optimize. See [`docs/supply-chain.md`](docs/supply-chain.md).
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

Deep docs: **operating guides for every feature and build live in [`docs/`](docs/)** (install, CLI, modeling, modules, API, front end, quickstart). The product/design/plan specs live in [`.claude/specs/factory-twin-scaffold/`](.claude/specs/factory-twin-scaffold/) and the stable project context in [`.claude/steering/`](.claude/steering/).

---

## Status & quality

- **Python** ≥ 3.11.
- **507 tests** across unit, analytical, and integration layers.
- **Blocking CI gates:** `ruff`, `mypy --strict`, `pytest`, `pip-audit` - the build fails on any finding.
- **Config is the only trust boundary:** YAML is loaded through one safe door, expressions run in one sandbox, and validation reports every problem before a run starts.
- **Reproducible by construction:** every run carries a stamp that lets you re-create it exactly.

> Status: **alpha.** The engine, active control, supply chain, and the optimization, calibration, and integration surfaces are complete and tested. The **beta** items in the roadmap below are designed and next up.

---

## Roadmap

**v1 - available now (built and tested)**
- Single-floor digital twin from pure config
- Excel/CSV plans, initial WIP, terminating stochastic runs
- KPI suite, what-if lever sweeps, parallel replications with confidence intervals
- Self-contained HTML report + versioned JSON sidecar
- `twinflow` CLI: `validate`, `run`, `balance`, `report`

**v2 - alpha (built and tested)**
- Tier 0 floor physics: reorder-point stock, probabilistic quality gate, batch/hold, changeover time
- Active control: dispatch policies (FIFO/EDD/SPT/critical-ratio), order release (CONWIP/WIP-cap), and seeded disruptions (machine breakdown, operator absence, rush priority)
- Trust loop: calibration that tunes a model until its KPIs match a real run
- Supply chain: supplier lead time, multi-echelon inventory, inventory KPIs, and inventory economics (holding/stockout costs, `service_level`) optimizable through the same surface
- Unified module surface: objectives, cost functions, optimizers, and surrogate ML models over one scoring seam
- Unified adapter surfaces: ERP/MES connectors, demand generation, forecasters, dispatch policies, a Gymnasium-style RL environment, plan-vs-actual reconciliation, orders/deliveries KPIs
- `twinflow optimize` CLI and a local REST API (`twinflow serve`)
- React front end (`web/`) to map, run, sweep, and optimize the twin in the browser

**Beta - next up (designed, not yet built)**
- Mid-run decide-act loop: a control seam so a policy or agent observes floor state at each decision point and acts within a seeded run (the seam the RL environment and an agent interface plug into)
- Resource / labor assignment policy: choose *which* machine and *which* operator by skill or load, not just first-free
- ERP / MES export → draft `model.yaml`: point a connector at an export and get a starting model, so a planner does not author YAML from scratch
- Plain-language tuning: "Steady / Normal / Jumpy" instead of a raw `cv`
- Sanity guardrails: validation warnings that catch foot-guns before a run
- Industry default profiles: sensible starting configs per shop type
- Paired-difference confidence interval surfaced in the report

**Near-term**
- Supply-chain at network scale: multi-plant, supplier tiers beyond one echelon
- Richer demand / order generation from customer and feature models (the generation surface ships today)
- Concrete forecasters (Prophet etc.) behind the shipped forecaster surface

**Later**
- Live ERP / MES connectors (the connector *surfaces* ship today; the concrete integrations come next)
- RL training against the shipped Gymnasium environment
- Closed-loop work instruction: push a chosen schedule back to the floor

> Everything under **v1** and **v2 - alpha** is real and running today. Everything below it is direction.

---

## License

Proprietary. © Joshua Schultz. All rights reserved.
