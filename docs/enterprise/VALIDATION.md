# Integrated validation evidence

Validated locally on 2026-09-12 for the integration of RM-01 through RM-10 into `enterprise-build`. The [build status](BUILD-STATUS.md) distinguishes delivered slices from the remaining [roadmap](../enterprise-roadmap.md) goals. Each epic has its own specification, review, and user guide.

## Results

| Check | Observed result |
| --- | --- |
| Full Python suite, including installed MCP and OR-Tools extras | 619 passed in 32.19 seconds |
| Strict mypy | 110 source files passed |
| Ruff lint and formatting | Passed |
| TypeScript and production Vite build | Passed |
| Frontend formatting | Passed |
| Chromium end-to-end tests | Six journeys passed; desktop and mobile screenshots inspected |
| npm dependency audit | Zero known vulnerabilities |
| pip dependency audit | No known vulnerabilities; local unpublished `twinflow` package skipped |
| Agent guide executable example | Ran against the local HTTP service: construct, validate, import, evaluate, branch staffing, compare job results, and export |

The browser journeys cover manufacturing import/load/map/run/export, mobile invalid input, office case results, supply delivery outlooks, operational event snapshots and actual CP-SAT scheduling with CSV export, and proposal approval followed by a dry-run receipt. Tests create their own scenarios; the action journey does not depend on another test's job.

Python 3.13.13, MCP 2.2.0, OR-Tools 9.15.6755, FastAPI 0.141.1, Ruff 0.16.4, and mypy 2.3.1 were installed for local validation. The frontend uses Vite 8 and Node 26 locally. CI targets Python 3.12 and Node 22. Hosted GitHub Actions has not been run as part of this local build.

## Reproduce

From the repository root, after installing `.[dev,api,agent,scheduling]`:

```console
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/twinflow
.venv/bin/pip-audit --skip-editable
```

From `web`, after `npm ci`:

```console
npm run build
npm run format:check
npm audit
npx playwright install chromium
npm run test:browser
```

Browser tests require the API at `127.0.0.1:8000` and Vite at `127.0.0.1:5173`; use a disposable API workspace. `PLAYWRIGHT_CHROMIUM_EXECUTABLE` optionally selects an existing Chromium binary. Screenshots and test artifacts go to `web/test-results`. The checked-in workflow starts both services and retains browser artifacts.

## Review corrections covered by regression tests

- Manufacturing order completion uses terminal accepted quantities; incomplete replications do not produce reassuring completion intervals. Capsule evaluation respects explicit artifact and execution limits.
- YAML parsing rejects duplicate keys, aliases, and excessive nesting through the single model loader boundary.
- Scheduling checks solver output independently. The actual CP-SAT backend is exercised, including infeasibility and frozen assignments; a failed heuristic is not presented as proof of infeasibility.
- Office calendars and revision-bound approvals are enforced. Supply component reservations, holds, gates, correlated delays, and censored dates received targeted corrections.
- Operational ingestion serializes concurrent retries: eight identical concurrent submissions produce one insertion and seven duplicates. Snapshot correction cycles and conflicting identities remain visible in readiness evidence.
- Agent evidence queries use bounded pagination and point oversized records to exports. Persistent jobs enforce quotas, cancellation transitions, and restart recovery.
- Outbox dispatch rechecks approval validity and revision, and receipt reconciliation uses atomic state transitions. The application only exposes a dry-run sink.
- Backup and restore check ownership, manifests, paths, archive bounds, and SQLite consistency. Dedicated workspace ownership prevents a live backup from racing the service.

## What these checks do not establish

These are correctness and workflow checks for a dedicated trusted workspace, not enterprise acceptance or an external security assessment. No customer data calibration, held-out forecast validation, production connector certification, tenant isolation, SSO/RBAC, distributed failover, or enterprise-scale load test has been completed. A reviewer label is not verified identity. There is no live ERP/MES writeback.

The supply adapter is a material-feasibility and lead-time model; the scheduling solver has a separately published finite grammar. Manufacturing does not yet support every physical shop constraint. See [RM-03 coverage](rm-03/COVERAGE.md) and each domain's user guide before interpreting a result. Simulation outputs remain conditional on supplied facts, assumptions, revisions, and model coverage.
