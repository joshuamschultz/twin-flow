"""Replay fidelity on the real fixtures (REQ-131, COMP-142; acceptance proofs for T-181).

`test_fidelity.py` proves exact replay on generated plans per feature group. This suite
proves it on the fixtures the rest of TwinFlow is judged by: with `DeterministicSource` and
no outages, executing a verified plan under plan control reproduces every operation's
planned start, setup end and end within 1e-6 s.

- The TF-005 ERP small shop (`tests/erp/fixtures/small_shop.json` through
  `build_problem_from_erp`): calendars, operators, held and in-progress jobs, cross-center
  options. CP-SAT refuses this problem (batching, pause, unattended are unsupported
  features), so only the baseline plan is replayed.
- The scaled ERP shop (`fixtures.generate.scaled_shop(orders=600)`, about 3,000 operations
  on 80 machines; `slow`).
- Pinedo Example 7.1.1 (`tests/scheduling/fixtures/pinedo_7_1_1.json`), solved with the
  baseline and, when OR-Tools is installed, with CP-SAT.

Each plan is checked first: the solver reports it verified, and it re-verifies exactly as the
simulation service will (`_plans.reverified`, feasibility only). A mismatch message names the
operation, the field, and the planned and simulated times.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from _plans import reverified

from twinflow.erp.build import build_problem_from_erp
from twinflow.erp.profile import load_planning_profile
from twinflow.erp.schema import ErpSnapshot
from twinflow.execution.engine import Limits, PlanControl, RepDraws, execute
from twinflow.execution.shop import build_shop
from twinflow.execution.sources import DeterministicSource
from twinflow.scheduling import BaselineSolver, Schedule, SolveRequest, apply_overrides, solve
from twinflow.scheduling.solver import Solver

ROOT = Path(__file__).resolve().parents[2]
SMALL_SHOP = ROOT / "tests" / "erp" / "fixtures" / "small_shop.json"
PINEDO_7_1_1 = ROOT / "tests" / "scheduling" / "fixtures" / "pinedo_7_1_1.json"
PROFILE_TOML = '[erp.class_weights]\n"A-KEY" = 4\n"B-CORE" = 2\n"C-SPOT" = 1\n'
FIDELITY_TOLERANCE = 1e-6
HAS_ORTOOLS = importlib.util.find_spec("ortools") is not None


def _erp_request(snapshot: ErpSnapshot) -> SolveRequest:
    built = build_problem_from_erp(snapshot, load_planning_profile(PROFILE_TOML))
    assert built.status == "ready", [i.code for i in built.issues if i.severity == "hard"]
    assert built.request is not None
    return built.request


def _small_shop_request() -> SolveRequest:
    snapshot = ErpSnapshot.model_validate_json(SMALL_SHOP.read_text(encoding="utf-8"))
    return _erp_request(snapshot)


def _scaled_shop_request() -> SolveRequest:
    erp_tests = str(ROOT / "tests" / "erp")
    if erp_tests not in sys.path:
        sys.path.insert(0, erp_tests)
    from fixtures.generate import scaled_shop

    return _erp_request(scaled_shop(orders=600))


def _pinedo_request() -> SolveRequest:
    return SolveRequest.model_validate_json(PINEDO_7_1_1.read_text(encoding="utf-8"))


def _verified_plan(request: SolveRequest, solver: Solver, budget: float) -> Schedule:
    """Solve, and assert the plan is verified and re-verifies (test precondition)."""
    result = solve(request, solver=solver, budget_seconds=budget)
    assert result.schedule.status in {"FEASIBLE", "OPTIMAL"}, result.issues[:5]
    assert result.verified is True, result.issues[:5]
    assert reverified(request, apply_overrides(request), result.schedule)
    assert result.schedule.assignments, "a plan with no assignment proves nothing"
    return result.schedule


def _replay(request: SolveRequest, schedule: Schedule) -> Any:
    shop = build_shop(apply_overrides(request))
    return execute(
        shop,
        PlanControl(schedule),
        draws=RepDraws(seed=1, replication=0, source=DeterministicSource()),
        outages=(),
        limits=Limits.for_shop(shop),
    )


def _mismatches(run: Any, schedule: Schedule) -> list[str]:
    """Every (operation, field) whose simulated time differs from the plan, as text."""
    records = {r.operation_id: r for r in run.records}
    found: list[str] = []
    for a in schedule.assignments:
        r = records[a.operation_id]
        if r.machine_id != a.machine_id:
            found.append(f"{a.operation_id}: machine {r.machine_id} != planned {a.machine_id}")
        for name in ("start", "setup_end", "end"):
            simulated, planned = getattr(r, name), getattr(a, name)
            if abs(simulated - planned) > FIDELITY_TOLERANCE:
                found.append(
                    f"{a.operation_id}: {name} simulated {simulated!r}, planned {planned!r}"
                )
    return found


def _assert_replays_exactly(request: SolveRequest, schedule: Schedule) -> None:
    run = _replay(request, schedule)
    assert run.termination == "completed"
    assert len({r.operation_id for r in run.records}) == len(run.records), "recorded twice"
    assert {r.operation_id for r in run.records} == {a.operation_id for a in schedule.assignments}
    mismatches = _mismatches(run, schedule)
    assert not mismatches, f"{len(mismatches)} mismatches, first: {mismatches[:10]}"


# --- TF-005 ERP small shop ------------------------------------------------------------------------


def test_small_shop_baseline_plan_replays_exactly() -> None:
    request = _small_shop_request()

    _assert_replays_exactly(request, _verified_plan(request, BaselineSolver(), 60.0))


# --- scaled ERP shop (600 orders) -----------------------------------------------------------------


@pytest.mark.slow
def test_scaled_shop_baseline_plan_replays_exactly() -> None:
    started = time.perf_counter()
    request = _scaled_shop_request()
    schedule = _verified_plan(request, BaselineSolver(), 60.0)
    prepared = time.perf_counter() - started

    _assert_replays_exactly(request, schedule)

    elapsed = time.perf_counter() - started
    print(
        f"scaled_shop: {len(schedule.assignments)} assignments, build+solve {prepared:.1f} s, "
        f"total with replay {elapsed:.1f} s"
    )


# --- Pinedo Example 7.1.1 -------------------------------------------------------------------------


def test_pinedo_7_1_1_baseline_plan_replays_exactly() -> None:
    request = _pinedo_request()

    _assert_replays_exactly(request, _verified_plan(request, BaselineSolver(), 10.0))


@pytest.mark.skipif(not HAS_ORTOOLS, reason="ortools not installed")
def test_pinedo_7_1_1_cpsat_plan_replays_exactly() -> None:
    from twinflow.scheduling.cpsat import ORToolsSolver

    request = _pinedo_request()

    _assert_replays_exactly(request, _verified_plan(request, ORToolsSolver(), 10.0))
