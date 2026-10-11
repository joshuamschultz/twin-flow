"""Parallel portfolio pipelines with deterministic results (T-161; COMP-024, REQ-120, D-079).

With `profile.workers > 1` the runner runs pipelines in a `concurrent.futures.
ProcessPoolExecutor` built with the spawn context (`mp_context` whose start method is
"spawn"); with 1 worker it builds no pool. Results are ordered by pipeline index, so the
same request, budget and seed give the same candidates, menu and recommendation with 1 and
4 workers for deterministic methods (tabu is iteration-limited by default, DEV-24; no `sa`).

A pipeline that raises is a `solver_error` issue on its candidate, never a crash of the run.
A solver registered only in this process is invisible in spawned children, so the raising
fake is tested with 1 worker, and the spawn path raises through a picklable `k_lookup`
(module-level function; `atcs` calls it inside `solve`).
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import functools
import importlib
import os
import time
from types import ModuleType
from typing import Any

import pytest
from bench.hmlv import HELD_OUT_SEEDS, hmlv_instance

from twinflow.scheduling import registry
from twinflow.scheduling.model import (
    ObjectiveKind,
    ObjectiveSpec,
    Problem,
    Schedule,
    SchedulingProfile,
    SolveRequest,
    WeightedSum,
    WeightedTerm,
)
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature, SolveContext

RUNNER = "twinflow.scheduling.portfolio.runner"
SEED = HELD_OUT_SEEDS[1]
DETERMINISTIC = ("edd", "atcs", "atcs->tabu", "sb_wt", "atc")
WT = ObjectiveKind.WEIGHTED_TARDINESS
SETUP = ObjectiveKind.TOTAL_SETUP
OBJECTIVE = WeightedSum(
    terms=(WeightedTerm(kind=WT, weight=1.0), WeightedTerm(kind=SETUP, weight=0.2))
)


def _runner() -> ModuleType:
    return importlib.import_module(RUNNER)


def _request(workers: int, *, small: bool = True) -> SolveRequest:
    base = hmlv_instance(SEED, jobs=12, ops_per_job=3) if small else hmlv_instance(SEED)
    fields = {"objective": OBJECTIVE, "workers": workers}
    profile = SchedulingProfile.model_validate(base.profile.model_dump() | fields)
    return base.model_copy(update={"profile": profile})


def _raising_k_lookup(tau: float, r: float, eta: float, m: int) -> tuple[float, float]:
    raise RuntimeError("k-table lookup failed")


def _codes(issues: Any) -> list[str]:
    return [issue.code for issue in issues]


def _candidate(result: Any, candidate_id: str) -> Any:
    return next(c for c in result.candidates if c.id == candidate_id)


def _comparable(result: Any) -> dict[str, Any]:
    """Everything but wall-clock fields: candidates (no schedule elapsed), menu, choice."""
    return {
        "candidates": [
            c.model_dump(mode="json", exclude={"schedule": {"elapsed_seconds"}})
            for c in result.candidates
        ],
        "menu": [entry.model_dump(mode="json") for entry in result.menu],
        "recommended": result.recommended,
        "effective_objective": result.effective_objective,
        "load_ratio": result.load_ratio,
        "atcs_runs": result.atcs_runs,
    }


@functools.cache
def _one_and_four() -> tuple[Any, Any]:
    runner = _runner()
    one = runner.solve_portfolio(_request(1), budget_seconds=20.0, pipelines=DETERMINISTIC)
    four = runner.solve_portfolio(_request(4), budget_seconds=20.0, pipelines=DETERMINISTIC)
    return one, four


# --- determinism -------------------------------------------------------------------------------


def test_one_and_four_workers_give_the_same_candidates_in_candidate_id_order() -> None:
    one, four = _one_and_four()
    assert [c.id for c in one.candidates] == [c.id for c in four.candidates]
    assert _comparable(one)["candidates"] == _comparable(four)["candidates"]


def test_one_and_four_workers_give_the_same_menu_and_recommendation() -> None:
    one, four = _one_and_four()
    assert one.menu == four.menu
    assert one.recommended == four.recommended
    assert one.recommended is not None
    assert _comparable(one) == _comparable(four)


def test_the_deterministic_run_verifies_every_pipeline_stage() -> None:
    one, _ = _one_and_four()
    stage_ids = ["00:edd", "01:atcs", "02:atcs", "02:atcs->tabu", "03:sb_wt", "04:atc"]
    pipeline_ids = [c.id for c in one.candidates if not c.id.startswith("pareto:")]
    assert pipeline_ids == stage_ids
    assert all(_candidate(one, cid).verified for cid in stage_ids)


# --- the pool ----------------------------------------------------------------------------------


@dataclasses.dataclass
class _PoolSpy:
    contexts: list[Any] = dataclasses.field(default_factory=list)
    max_workers: list[Any] = dataclasses.field(default_factory=list)


def _spy_pool(monkeypatch: pytest.MonkeyPatch) -> _PoolSpy:
    spy = _PoolSpy()
    original = concurrent.futures.ProcessPoolExecutor.__init__

    def init(
        self: Any, max_workers: Any = None, mp_context: Any = None, *a: Any, **kw: Any
    ) -> None:
        spy.contexts.append(mp_context)
        spy.max_workers.append(max_workers)
        original(self, max_workers, mp_context, *a, **kw)

    monkeypatch.setattr(concurrent.futures.ProcessPoolExecutor, "__init__", init)
    return spy


def test_four_workers_use_a_process_pool_with_the_spawn_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _spy_pool(monkeypatch)
    result = _runner().solve_portfolio(_request(4), budget_seconds=6.0, pipelines=("edd", "atc"))
    assert spy.contexts, "no ProcessPoolExecutor was built"
    assert all(ctx is not None and ctx.get_start_method() == "spawn" for ctx in spy.contexts)
    assert all(n is not None and 1 <= n <= 4 for n in spy.max_workers), spy.max_workers
    assert _candidate(result, "00:edd").verified and _candidate(result, "01:atc").verified


def test_one_worker_builds_no_process_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _spy_pool(monkeypatch)
    _runner().solve_portfolio(_request(1), budget_seconds=3.0, pipelines=("edd", "atc"))
    assert spy.contexts == []


# --- a raising pipeline ------------------------------------------------------------------------


class _Boom:
    """A registered solver whose `solve` raises (a solver bug)."""

    @property
    def name(self) -> str:
        return "boom"

    @property
    def capabilities(self) -> frozenset[Feature]:
        return frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        raise RuntimeError("solver bug with internals /secret/path")


def test_a_raising_pipeline_is_a_solver_error_candidate_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boom = SolverSpec(
        name="boom",
        family="test",
        factory=_Boom,
        deterministic=True,
        capabilities=frozenset(Feature),
    )
    patched = dict(registry._entries()) | {"boom": (boom, "tests.fake")}
    monkeypatch.setattr(registry, "_entries", lambda: patched)
    result = _runner().solve_portfolio(
        _request(1), budget_seconds=4.0, pipelines=("edd", "boom", "atc")
    )
    failed = _candidate(result, "01:boom")
    assert not failed.verified
    assert failed.schedule.status == "UNKNOWN"
    errors = [i for i in failed.issues if i.code == "solver_error"]
    assert errors, _codes(failed.issues)
    assert all("/secret/path" not in i.message for i in errors), "internals leaked"
    assert _candidate(result, "00:edd").verified
    assert _candidate(result, "02:atc").verified


def test_a_pipeline_raising_in_a_spawned_worker_is_a_solver_error_candidate() -> None:
    result = _runner().solve_portfolio(
        _request(4),
        budget_seconds=6.0,
        pipelines=("edd", "atcs", "atc"),
        k_lookup=_raising_k_lookup,
    )
    failed = _candidate(result, "01:atcs")
    assert not failed.verified
    assert "solver_error" in _codes(failed.issues), _codes(failed.issues)
    assert _candidate(result, "00:edd").verified
    assert _candidate(result, "02:atc").verified


def _exiting_k_lookup(tau: float, r: float, eta: float, m: int) -> tuple[float, float]:
    os._exit(3)  # a worker killed mid-run (as by the OOM killer)


def test_a_worker_process_that_dies_does_not_crash_the_run() -> None:
    result = _runner().solve_portfolio(
        _request(4), budget_seconds=6.0, pipelines=("edd", "atcs"), k_lookup=_exiting_k_lookup
    )
    failed = _candidate(result, "01:atcs")
    assert not failed.verified
    assert "solver_error" in _codes(failed.issues), _codes(failed.issues)


# --- budget ------------------------------------------------------------------------------------


@pytest.mark.slow
def test_budget_respected_with_four_workers() -> None:
    pipelines = ("edd", "atcs->tabu", "sb_wt", "atc", "edd->tabu")
    started = time.monotonic()
    result = _runner().solve_portfolio(
        _request(4, small=False), budget_seconds=10.0, pipelines=pipelines
    )
    elapsed = time.monotonic() - started
    assert elapsed <= 10.0 * 1.1 + 1.0, elapsed
    assert any(c.verified for c in result.candidates)
