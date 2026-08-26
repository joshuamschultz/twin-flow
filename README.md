# factory-twin

Config-driven discrete-event **digital twin** for discrete manufacturing floors.

A client is one `model.yaml` plus a spreadsheet/CSV production plan — never new engine
code. Every operation is one contract (bundles in → bundles out); every KPI derives from
a single `ProcessExecution` event table. Runs are terminating and stochastic, so promised
dates carry confidence intervals.

## Install (from source)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"      # editable, with dev tooling
```

For use inside another project:

```bash
pip install "factory-twin @ git+ssh://<private-remote>@<40-char-sha>"
```

## CLI

```bash
ftwin validate <model.yaml>
ftwin run <model.yaml> --plan <plan.xlsx> --reps N
ftwin balance <model.yaml> --plan <plan.xlsx> --sweep <sweep.yaml> --reps N
ftwin report <run-id> --out html
```

## Layout

```
src/factory_twin/
  engine/          L0  SimPy substrate, dual-acquire, per-source RNG
  primitives/      L1  Bundle, Stock, Location, Machine, Transform, TimeModel, LaborPool
  model/           L2  yaml.safe_load + expression sandbox + compiler + validator
  plan/            L3  work orders, release timing, initial WIP, run driver
  instrumentation/ L4  event log + KPIs + sweep harness
  report/          L4  self-contained HTML + JSON sidecar + assumptions
  cli/                 thin argument parsing
```

Quality gates (CI-blocking): `ruff check`, `ruff format --check`, `mypy --strict src`,
`pytest`, `pip-audit`.

See [.claude/specs/factory-twin-scaffold/](.claude/specs/factory-twin-scaffold/) for PRD/SDD/PLAN.
