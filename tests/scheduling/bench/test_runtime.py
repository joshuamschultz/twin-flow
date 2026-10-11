"""The runtime budget table on the 600 x 8 scale instance (T-133, T-139, T-143; COMP-027).

SDD "Runtime budgets": each method on `scale_instance()` (600 jobs x 8 ops, 8 centers of 5
machines, HMLV data) returns a verified schedule within its budget, or, where the table
says so, a named refusal fast. Time is wall clock around `solve()` (validation, compile,
the method, verification); building the instance is outside the timing. Shifting
bottleneck must return a verified schedule: a refusal is not acceptable there.

A later method adds one row to `VERIFIED_BUDGETS` or `REFUSAL_BUDGETS`.
"""

from __future__ import annotations

import functools
import importlib.util
import time

import pytest

from bench.hmlv import scale_instance
from twinflow.scheduling.model import SolveRequest, SolveResult
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.solver import solve

DISPATCH_RULES = ("fifo", "edd", "spt", "wspt", "lpt", "ms", "cr", "sst", "lfj", "sqno")

VERIFIED_BUDGETS: tuple[tuple[str, float], ...] = (
    *((rule, 10.0) for rule in DISPATCH_RULES),
    ("atc", 10.0),
    ("atcs", 10.0),
    ("sb_makespan", 90.0),  # defaults: reopt_rounds 2 plus the final round
    ("sb_wt", 90.0),
)
"""(method, seconds): a verified schedule in under `seconds`."""

REFUSAL_BUDGETS: tuple[tuple[str, str, float, float], ...] = (
    ("ortools", "model_too_large", 2.0, 10.0),
)
"""(method, issue code, seconds, budget handed to the solver): UNKNOWN with that code fast."""

needs_ortools = pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None, reason="ortools (scheduling extra) is absent"
)


@functools.cache
def _scale() -> SolveRequest:
    return scale_instance(0)


def _timed(method: str, budget: float) -> tuple[SolveResult, float]:
    request = _scale()
    solver = get_solver(method)
    start = time.perf_counter()
    result = solve(request, solver=solver, budget_seconds=budget)
    elapsed = time.perf_counter() - start
    print(f"\n{method:<12} {elapsed:8.2f} s  (budget {budget:g} s)  {result.schedule.status}")
    return result, elapsed


@pytest.mark.slow
@pytest.mark.parametrize(
    ("method", "seconds"), VERIFIED_BUDGETS, ids=[m for m, _ in VERIFIED_BUDGETS]
)
def test_method_returns_a_verified_schedule_on_600x8_within_its_budget(
    method: str, seconds: float
) -> None:
    result, elapsed = _timed(method, seconds)

    assert result.verified, (method, result.schedule.status, [i.code for i in result.issues])
    assert len(result.schedule.assignments) == 600 * 8
    assert elapsed < seconds, f"{method} took {elapsed:.2f} s, budget {seconds} s"


@pytest.mark.slow
@needs_ortools
@pytest.mark.parametrize(
    ("method", "code", "seconds", "budget"), REFUSAL_BUDGETS, ids=[r[0] for r in REFUSAL_BUDGETS]
)
def test_method_refuses_600x8_by_name_fast(
    method: str, code: str, seconds: float, budget: float
) -> None:
    result, elapsed = _timed(method, budget)

    assert result.schedule.status == "UNKNOWN", result.schedule.status
    assert code in [issue.code for issue in result.issues], [i.code for i in result.issues]
    assert elapsed < seconds, f"{method} took {elapsed:.2f} s to refuse, limit {seconds} s"
