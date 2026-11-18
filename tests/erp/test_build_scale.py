"""T-204: the 600-order shop validates and builds in under 5 s, and its JSON stays under 16 MiB.

TF-005 review ruling A: the scaled shop also solves verified with the baseline, and an
overloaded variant (7-day window) solves verified and late instead of UNKNOWN.
"""

from __future__ import annotations

import time

import pytest
from fixtures.generate import scaled_shop

from twinflow.erp.build import BuildResult, build_problem_from_erp
from twinflow.erp.digest import MAX_SNAPSHOT_BYTES
from twinflow.erp.profile import load_planning_profile
from twinflow.scheduling import BaselineSolver, SolveResult, solve

PROFILE = """
[erp.class_weights]
"A-KEY" = 4
"B-CORE" = 2
"C-SPOT" = 1
"""


@pytest.mark.slow
def test_scaled_shop_validates_and_builds_within_budget() -> None:
    snapshot = scaled_shop(orders=600)
    size = len(snapshot.model_dump_json().encode())
    profile = load_planning_profile(PROFILE)

    started = time.perf_counter()
    result = build_problem_from_erp(snapshot, profile)
    elapsed = time.perf_counter() - started

    print(f"scaled_shop: {size / 2**20:.2f} MiB, validate+build {elapsed:.3f} s")
    assert result.status == "ready"
    assert result.request is not None
    assert len(snapshot.production_orders) == 600
    assert size < MAX_SNAPSHOT_BYTES
    assert elapsed < 5.0


# --- TF-005 review ruling A: the horizon is a planning window, not a wall ----------------------


def _scaled_build(erp_toml: str = "") -> BuildResult:
    profile = load_planning_profile(f"[erp]\n{erp_toml}\n{PROFILE}")
    result = build_problem_from_erp(scaled_shop(orders=600), profile)
    assert result.status == "ready", [i.code for i in result.issues if i.severity == "hard"]
    assert result.request is not None
    return result


def _verified(result: BuildResult) -> SolveResult:
    assert result.request is not None
    solved = solve(result.request, solver=BaselineSolver(), budget_seconds=60.0)
    assert solved.schedule.status in {"FEASIBLE", "OPTIMAL"}, solved.issues[:5]
    assert solved.verified is True, solved.issues[:5]
    return solved


@pytest.mark.slow
def test_scaled_shop_builds_and_solves_verified_with_the_baseline() -> None:
    _verified(_scaled_build())


@pytest.mark.slow
def test_scaled_shop_late_material_inside_the_cap_is_planned_not_held() -> None:
    # The late wire PO lands on day 58, past the 56-day window but inside the 224-day cap:
    # its jobs are planned (late), and the hold applies only with `hard_horizon = true`.
    soft = _scaled_build()
    assert [i for i in soft.issues if i.code == "release_after_horizon"] == []
    hard = _scaled_build("hard_horizon = true")
    assert any(i.code == "release_after_horizon" for i in hard.issues)
    assert soft.counts["held_jobs"] < hard.counts["held_jobs"]


@pytest.mark.slow
def test_scaled_shop_overloaded_window_solves_verified_and_late_not_unknown() -> None:
    result = _scaled_build("horizon_days = 7")
    assert result.horizon_seconds == 7 * 86_400.0
    solved = _verified(result)
    assert max(a.end for a in solved.schedule.assignments) > result.horizon_seconds
