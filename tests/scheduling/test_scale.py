"""Scale check: 600 jobs x 8 operations in under 10 seconds (T-111, REQ-075, REQ-084).

One `solver.solve` call validates the request, compiles its overrides, runs the baseline
and verifies the plan; the whole call is timed. Marked `slow`, still run by bare `pytest`.
"""

from __future__ import annotations

import time

import pytest
from generators import SCALE_JOBS, SCALE_OPERATIONS, scale_request

from twinflow.scheduling.seam import Feature
from twinflow.scheduling.solver import solve

WALL_LIMIT_SECONDS = 10.0


@pytest.mark.slow
def test_600_by_8_problem_with_setups_calendars_operators_verifies_under_10_seconds() -> None:
    request = scale_request(seed=0)
    operations = sum(len(job.operations) for job in request.problem.jobs)

    started = time.perf_counter()
    result = solve(request)
    elapsed = time.perf_counter() - started

    print(f"\n600x8 baseline: {elapsed:.2f} s wall, solver {result.schedule.elapsed_seconds:.2f} s")
    assert operations == SCALE_JOBS * SCALE_OPERATIONS
    assert {Feature.SETUPS, Feature.CALENDARS, Feature.OPERATORS} <= set(result.features)
    assert result.schedule.status == "FEASIBLE", result.issues
    assert result.verified, result.issues
    assert len(result.schedule.assignments) == operations
    assert elapsed < WALL_LIMIT_SECONDS, f"600x8 solve took {elapsed:.2f} s"
