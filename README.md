![factory-twin](docs/header.png)

# factory-twin

**Describe your factory floor in a spreadsheet and a config file. Get back honest answers about dates, bottlenecks, and staffing — each with a confidence range, not a guess.**

`factory-twin` is a config-driven digital twin for discrete manufacturing floors. A digital twin is a working software copy of your real operation. You describe the floor once, in plain config, and the engine runs a fast, realistic simulation of a shift, a week, or a whole order book. Then it answers the questions planners actually ask.

No custom code per plant. One `model.yaml` plus a production plan (`.xlsx` or `.csv`) is a complete client.

---

## What it is, and who it's for

Two audiences, one engine.

- **Operations and planning people** — production planners, schedulers, plant and ops managers, industrial engineers. You want to know when orders will finish, whether you will be on time, where the line chokes, and whether one more operator or a bigger buffer is worth it. You get those answers without a consultant or a six-month software project.
- **AI and agent teams** — the twin is built to be driven by software, not just by a person clicking. It is a safe sandbox where an agent can try a decision a thousand times before it ever touches a real worker, planner, or schedule.

You bring the shape of your floor. The twin brings the math.

## Why it exists

Most plants promise dates and set staffing from a spreadsheet and years of gut feel. That works until it doesn't: a rush order, a new mix, a machine down, a temp short. The spreadsheet cannot tell you what happens next, and it never tells you how sure it is.

A simulation can. Because real floors are random (cycle times vary, scrap happens), `factory-twin` runs each scenario many times and reports a **confidence interval** — a low-to-high range you can trust — instead of a single number that pretends to be certain. You stop quoting dates you cannot hit and stop adding capacity you do not need.

## Key capabilities (available today)

Everything in this section is built and tested. The library API is stable; the `ftwin` command line surfaces the same operations.

| Capability | What it means for you |
|---|---|
| **Model any discrete floor from plain config** | Stocks, work centers, machines, labor pools, routings, scrap, rework, batching, and setup/changeover — all declared, never coded. One modeling contract covers cutting, pouring, assembly, sorting, and yield loss. |
| **Plan from Excel or CSV** | Your production plan drives order releases and any work already on the floor (work-in-progress). Hand over the spreadsheet you already keep. |
| **Reproducible, seeded runs** | Every run is repeatable to the number. Same inputs, same answer — which is what makes fair comparisons possible. |
| **KPIs from one event log** | Completion dates and on-time %, per-cell and per-machine utilization, a clear wait breakdown (starved vs blocked vs waiting-on-material), labor and machine hours, setup hours separate from run hours, and work-in-progress over time. |
| **"What-if" lever sweeps** | Try staffing, buffer sizes, and batch sizes across a grid and see every option side by side, fairly paired. The twin shows the trade space and deliberately picks no winner — the call stays yours. |
| **Parallel replications + confidence intervals** | Run many replications at once. Compare two setups and get a confidence interval on the *difference*, not two overlapping ranges that hide the real signal. |
| **One emailable report** | A single self-contained HTML file that opens offline with no internet, plus a versioned JSON file for machines. Every report opens with the assumptions the engine had to make, stated plainly. |
| **Full reproducibility stamp** | Each run records hashes of the model and plan, the seed, the engine version, the Python version, and the exact dependency set. A result can always be traced back and re-created. |

## How it works

Four steps, start to finish.

1. **Define the floor** in `model.yaml`.
2. **Provide the plan** as a spreadsheet or CSV.
3. **Validate**, then **simulate**.
4. **Read the report.**

A small, illustrative `model.yaml` (shortened for clarity):

```yaml
part_types:
  - name: wire
    uom: ft
  - name: blank
    uom: piece

stocks:
  - name: wire_stock
    thing: wire
    uom: ft

locations:
  - name: cut
    consumes: [{ thing: wire, qty: 10, uom: ft }]
    emits:    [{ thing: blank, uom: piece }]     # count derived from the recipe
    scrap:    { rate: 0.05, thing: scrap_blank, uom: piece }   # 5% scrap, auto-split
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

A tiny production plan (`plan.csv`):

| work_order_id | part   | qty  | start_date | due_date |
|---------------|--------|------|------------|----------|
| WO-1001       | carton | 500  | 0          | 3600     |
| WO-1002       | carton | 250  | 600        | 5400     |

You declare *what the floor does* in floor language — "5% scrap", "batches of 200", "cut then pack". The engine turns that into the mechanism. You never write a simulation by hand.

## Quick start

Install from source:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Run it from the command line:

```bash
ftwin validate model.yaml                                  # check the floor before running
ftwin run model.yaml --plan plan.csv --reps 30             # simulate 30 replications
ftwin balance model.yaml --plan plan.csv --sweep sweep.json --reps 30   # sweep the levers
ftwin report <run-id> --out html                           # render the emailable report
```

Or drive it from Python (the same operations, callable in your own code):

```python
from factory_twin.model import load_model
from factory_twin.plan import load_plan
from factory_twin.plan.replication import ReplicationRunner

model = load_model("model.yaml")          # parse + validate + compile, once
plan = load_plan("plan.csv", model.registry)
results = ReplicationRunner("model.yaml").run(plan, reps=30, base_seed=42)
```

## For AI agents and multi-agent systems

This is a first-class use case, not an afterthought.

The twin is a **decision sandbox**. An agent can propose a change — add an operator to a cell, resize a buffer, resequence a batch — and the twin scores it against the same reproducible clock every other option is scored on. Nothing real is touched. The humans who run the floor (planners, schedulers, operators) receive a recommendation with evidence behind it.

What is here today:

- **Structured JSON output** of every KPI, versioned, ready to parse.
- **Deterministic seeded runs**, so two agent proposals are compared on equal footing.
- **An in-process run entry point** callable thousands of times against an already-loaded model — no re-parsing, no subprocess churn.
- **No network, no secrets, no side effects.** Safe to run in a tight loop.

Pattern: *agents propose, the twin scores, humans decide.* The richer agent-interaction surface (standard tool schemas, streaming scenario feedback, direct policy hooks) is evolving — see the roadmap.

## Planned

The sections below are direction, not shipped features. They build on the same one-contract engine.

### Planned: supply-chain twin

Extend past the four walls of one plant to **orders, sub-vendors, and multi-echelon inventory** (stock held at several tiers — plant, regional, customer). Same engine, new node types. A live feed from another system becomes a column mapping onto the existing plan contract rather than a new integration each time.

### Planned: reinforcement learning via Gymnasium

Expose the twin as a **Gymnasium environment** (the standard interface RL agents train against) so reinforcement-learning methods can learn staffing, dispatch, and inventory policies by playing against a realistic floor instead of a toy model.

### Planned: forecaster integrations

Plug in demand and lead-time **forecasters** (for example, Prophet) to feed plans and generate forward-looking scenarios, so the twin runs against what is likely to come, not only what already happened.

### Planned: optimizers

Today the sweep enumerates a grid and reports it. Next, pair that scoring surface with **optimizers** that actively search for the best way forward — the good configuration found faster, without hand-listing every option.

### Planned: live ERP/MES connectors

Let plans arrive straight from the systems of record (ERP for orders, MES for shop-floor status) instead of exported spreadsheets, so the twin stays in step with the real operation.

## Architecture

A five-layer design. Lower layers never depend on higher ones, and layers 0 through 2 never change per client — only the config does.

| Layer | Role |
|---|---|
| **engine** | Simulation substrate: clock, resource acquisition, random-number streams |
| **primitives** | The modeling vocabulary: bundles, stocks, locations, machines, labor, transforms |
| **model** | Config in: safe YAML load, expression sandbox, compiler, validator |
| **plan** | Work orders, release timing, initial WIP, the run driver, replications |
| **instrumentation / report** | One event log to every KPI, the lever sweep, and the report |

The governing rule: **no per-client code, config only.** If a new floor needs an engine change, the abstraction was wrong.

Deeper docs live in the repo:

- Requirements, design, and task plan: [`.claude/specs/factory-twin-scaffold/`](.claude/specs/factory-twin-scaffold/)
- Product, tech, structure, and roadmap context: [`.claude/steering/`](.claude/steering/)

## Status and quality

- **300+ automated tests**, run on every change.
- **Blocking quality gates**: `ruff` (style), `mypy --strict` (types), `pytest` (tests), `pip-audit` (dependency security). A build fails on any finding.
- **Python 3.11+**. Installed from source; designed to be embedded inside larger builds that need a digital twin.

---

## Roadmap

**v1 — available now (built and tested)**
- Single-floor digital twin from pure config
- Excel/CSV plans, initial WIP, terminating stochastic runs
- Full KPI set from one event log
- Lever sweeps with fair, paired comparisons
- Parallel replications with confidence intervals
- Self-contained offline HTML report + versioned JSON sidecar
- Reproducibility stamp on every run
- `ftwin` command line: `validate`, `run`, `balance`, `report`

**Near-term — direction**
- Supply-chain nodes: orders, sub-vendors, multi-echelon inventory
- Gymnasium environment for reinforcement-learning policies
- Forecaster hooks (Prophet and friends) feeding plans and scenarios
- Optimizer loop over the sweep/scoring surface

**Later — direction**
- Live ERP/MES connectors for plans from systems of record
- Richer multi-agent orchestration surface
- Calibration against real historical production runs, so a twin can be trusted enough to quote dates from

*v1 is real and running. Everything under "direction" is where this is headed, not what it does today.*
