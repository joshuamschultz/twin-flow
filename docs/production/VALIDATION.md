# Production capability validation matrix

This matrix maps the ten Western Spring specifications in [`SPEC.md`](SPEC.md) to the
Python contracts and focused tests delivered in `0.4.0a1`. “Implemented” means the
declared typed input is scheduled or accounted for and independently checked by the
library. It does not mean that Western Spring's calendars, recipes, capacities, or
commitments have been calibrated or accepted on the plant floor.

| Spec | Source contract | Focused evidence | Status in 0.4.0a1 |
|---|---|---|---|
| TF-WS-001 coupled resources/conveyor | `Phase`, `ResourceUse`, phase segments and resource occupations | `test_scheduling.py`, `test_schedule_integrity.py` auxiliary-use and occupation-forgery cases | Implemented for declared simultaneous resources and phase spans; the caller must model its actual pipeline and dwell rule |
| TF-WS-002 labor/shift/restart | `Resource.windows`, qualifications, phase uses, `RestartRule` | `test_runtime.py` attendance, off-shift hold, and restart cases | Implemented for declared deterministic windows, attendance, holds, and restart rules |
| TF-WS-003 oven batches/recipes | `ThermalRecipe`, `BatchRequirement`, `BatchAssignment` | `test_scheduling.py`, `test_schedule_integrity.py` compatibility, capacity, duration, and repeated-visit cases | Implemented for declared batch capacity, recipe compatibility, and fixed thermal phases |
| TF-WS-004 WIP/lots/attribution | `FlowLot`, `LotLedger`, runtime active-WIP expansion; legacy `WorkOrder` snapshot fields | `test_lots.py`, `test_runtime_integrity.py`, `test_des_wip_integrity.py` | Implemented for explicit lots, partial flow, completed good, and retained active time/setup state |
| TF-WS-005 shared physical resources | rich `Resource.id`; legacy DES machine registry | `test_des_identity.py`, `test_des_wip_integrity.py`, scheduler capacity tests | Implemented: repeated legacy locations share one named machine's capacity/setup state, and rich schedules share resource IDs |
| TF-WS-006 recipes/setup/preferences | `RecipeAlternative` revision/resource/preference fields and explicit setup phases | `test_scheduling.py`, `test_schedule_integrity.py` equal-duration preference, fallback, and full-setup cases | Implemented for declared alternatives, revisions, hard eligibility, preference cost, and per-job setup |
| TF-WS-007 materials/coils/mass | `MaterialLot`, requirements, consumptions, atomic reservations, reweigh | `test_materials.py`, `test_material_integrity.py`, runtime completion tests | Implemented for measured identified lots, exact UOM, readiness, conservation, and replay |
| TF-WS-008 qualification/rework | `QualificationPlan`, `QualificationState`, `QualificationWorkflow`, `QualificationSampleAccount` | `test_qualification.py`, `test_state_integrity.py`, `test_runtime_completion.py` | Implemented as a bounded downstream-test gate with revision identity, adjustment work, sample accounts, and explicit partial exhaustion |
| TF-WS-009 outside processing/shipments | `ExternalOperation`, `ExternalLedger`, `ExternalWorkflow`, optional vendor operation | `test_external.py`, `test_runtime_integrity.py`, `test_runtime_completion.py` | Implemented for dispatch, known or unknown vendor capacity, partial receipt/loss, downstream release, chronology, and separate customer shipment |
| TF-WS-010 commitments/backlog | `ProductionJob`, `ScenarioCohort`, `JobResult`, runtime requested/executed scope | `test_commitments.py`, `test_schedule_integrity.py`, `test_runtime_completion.py` | Implemented for declared promises and cohorts; unresolved customer obligations have no fabricated job result or service fraction |

## Validation commands

From the reviewed release checkout:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q tests/production
ruff check src/twinflow/production tests/production
mypy
```

Pin consumers to the reviewed release commit rather than a moving branch. Python release
metadata is `0.4.0a1` and web metadata is `0.4.0-alpha.1`. The API and capsule schema
versions are separate contracts and did not change with this package release.

The deterministic baseline returns `FEASIBLE` only when its result passes the independent
verifier. It returns `UNKNOWN` when its placement policy cannot establish feasibility;
it does not claim global optimality. The legacy CP-SAT scheduler remains a separate API.

## Release validation evidence

Recorded on 2026-09-12 for the final release tree:

- Full Python suite: **689 passed in 34.08 seconds**; production-focused suite: **70
  passed**.
- `ruff check src tests` and the formatter check passed.
- Strict `mypy` passed across **120 source files**.
- The isolated Python source and wheel build passed.
- The web `0.4.0-alpha.1` build and formatting checks passed.
- Both runnable Python examples in the production guide passed against the installed
  package.
