"""Perturbation runs and K-table update (T-164, COMP-025, REQ-122, D-082, Pinedo p.488-489).

API these tests pin (`twinflow.scheduling.learning.perturb`, the implementer builds to it):

- `perturbation_runs(request, center_id, table, delta=0.25) -> tuple[AtcsRun, ...]`: the
  center's (tau, R, eta, m) give (K1, K2) = `ktable.lookup(table, stats)`; ATCS (looked up
  by registry name at call time) runs at that pair and at the 8 neighbors (K1 +- delta K1,
  K2 +- delta K2 and the 4 corners, clamped to [0.5, 10] / [0.05, 10]). Returns 9 records,
  the base first with source `table`, the 8 neighbors with source `perturbed`; every
  record carries the center's statistics and `objective_value` = that run's
  `primary_value` under the request's objective.
- `update_k_table(table, stats, best, runs, *, threshold=0.02, radius=...) -> KTable`
  (pure): `stats` has `tau`, `R`, `eta`, `m` (an `InstanceStats` or an `AtcsRun`); `runs[0]`
  is the base run, `best` the run to adopt. WHEN `best.objective_value <= (1 - threshold)
  * base.objective_value` the nearest entry in (tau, R, eta, log2 m) within `radius` takes
  best's (K1, K2), or a new entry at the stats point is added when none is within the
  radius; the result is a new table and the input is unchanged. Otherwise (or with a
  missing objective value) the result equals the input.
"""

from __future__ import annotations

import importlib
import itertools
import statistics
from types import ModuleType
from typing import Any, Literal

import pytest
from bench.gates import HELD_OUT_BENCH, Bench, bench_profile, reference_values, score
from bench.hmlv import HELD_OUT_SEEDS, TRAINING_SEEDS, hmlv_instance
from kernel_cases import load_request

from twinflow.scheduling.kernel.stats import InstanceStats
from twinflow.scheduling.model import AtcsRun, SolveRequest
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Solver
from twinflow.scheduling.solver import solve

CENTER_1421 = "WC-M1"
TRAINING_BENCH = Bench(instance=hmlv_instance, instance_seeds=TRAINING_SEEDS)
ROUNDS = 10
MAX_WORSENING = 0.01
Source = Literal["table", "perturbed"]


def _kt() -> ModuleType:
    return importlib.import_module("twinflow.scheduling.learning.ktable")


def _pt() -> ModuleType:
    """The module, imported per test so its absence fails each test, not collection."""
    return importlib.import_module("twinflow.scheduling.learning.perturb")


def _stats(tau: float, R: float, eta: float, m: int) -> InstanceStats:  # noqa: N803
    return InstanceStats(
        work_center_id="WC",
        m=m,
        pbar=1.0,
        sbar=eta,
        cmax_hat=10.0,
        tau=tau,
        R=R,
        eta=eta,
        sampled_pairs=0,
    )


def _run(
    stats: InstanceStats, k1: float, k2: float, value: float | None, source: Source
) -> AtcsRun:
    return AtcsRun(
        work_center_id=stats.work_center_id,
        tau=stats.tau,
        R=stats.R,
        eta=stats.eta,
        m=stats.m,
        K1=k1,
        K2=k2,
        source=source,
        objective_value=value,
    )


def _runs(
    stats: InstanceStats, base_value: float | None, best_value: float | None
) -> tuple[tuple[AtcsRun, ...], AtcsRun]:
    """A base run at K (2, 1), one improving neighbor at (2.5, 1.25), the rest worse."""
    base = _run(stats, 2.0, 1.0, base_value, "table")
    best = _run(stats, 2.5, 1.25, best_value, "perturbed")
    worse = tuple(
        _run(stats, 2.0 + 0.5 * a, 1.0 + 0.25 * b, (base_value or 0.0) + 10.0, "perturbed")
        for a, b in itertools.product((-1, 0, 1), repeat=2)
        if (a, b) not in ((0, 0), (1, 1))
    )
    return (base, best, *worse), best


def _entry_at(table: Any, point: tuple[float, float, float, int]) -> Any:
    return next(e for e in table.entries if (e.tau, e.R, e.eta, e.m) == point)


def _expected_neighbors(k1: float, k2: float, delta: float) -> list[tuple[float, float]]:
    def clamp(v: float, lo: float, hi: float) -> float:
        return min(max(v, lo), hi)

    return sorted(
        (clamp(k1 * (1 + a * delta), 0.5, 10.0), clamp(k2 * (1 + b * delta), 0.05, 10.0))
        for a, b in itertools.product((-1, 0, 1), repeat=2)
        if (a, b) != (0, 0)
    )


def _explicit_value(request: SolveRequest, k1: float, k2: float) -> float | None:
    data = request.model_dump(mode="json")
    data["profile"]["method_params"] = {"atcs": {"K1": k1, "K2": k2}}
    result = solve(SolveRequest.model_validate(data), solver=get_solver("atcs"))
    assert result.verified, result.issues
    return result.schedule.primary_value


# --- perturbation_runs on Example 14.2.1 ------------------------------------------------------


def test_perturbation_runs_on_14_2_1_returns_base_and_eight_neighbors() -> None:
    table = _kt().seed_table()
    runs = _pt().perturbation_runs(load_request("pinedo_14_2_1"), CENTER_1421, table)
    assert len(runs) == 9
    assert all(isinstance(r, AtcsRun) for r in runs)
    base, neighbors = runs[0], runs[1:]
    assert base.source == "table"
    assert [r.source for r in neighbors] == ["perturbed"] * 8
    assert (base.K1, base.K2) == pytest.approx(_kt().lookup(table, base))
    expected = _expected_neighbors(base.K1, base.K2, 0.25)
    actual = sorted((r.K1, r.K2) for r in neighbors)
    assert actual == pytest.approx(expected)


def test_perturbation_runs_carry_the_center_statistics() -> None:
    runs = _pt().perturbation_runs(load_request("pinedo_14_2_1"), CENTER_1421, _kt().seed_table())
    base = runs[0]
    assert base.work_center_id == CENTER_1421
    assert (base.tau, base.R, base.eta, base.m) == pytest.approx(
        (0.566038, 0.471698, 0.177778, 1), abs=1e-5
    )
    for run in runs[1:]:
        assert (run.work_center_id, run.tau, run.R, run.eta, run.m) == (
            base.work_center_id,
            base.tau,
            base.R,
            base.eta,
            base.m,
        )


def test_perturbation_runs_objective_values_are_the_real_atcs_values() -> None:
    request = load_request("pinedo_14_2_1")
    runs = _pt().perturbation_runs(request, CENTER_1421, _kt().seed_table())
    for run in runs:
        assert run.objective_value is not None
        assert run.objective_value == pytest.approx(_explicit_value(request, run.K1, run.K2))


def test_perturbation_runs_delta_scales_the_neighbors() -> None:
    table = _kt().seed_table()
    runs = _pt().perturbation_runs(load_request("pinedo_14_2_1"), CENTER_1421, table, delta=0.1)
    base = runs[0]
    actual = sorted((r.K1, r.K2) for r in runs[1:])
    assert actual == pytest.approx(_expected_neighbors(base.K1, base.K2, 0.1))


# --- update_k_table (pure) --------------------------------------------------------------------


def test_update_moves_the_entry_at_the_stats_point_when_improving_by_3_percent() -> None:
    table = _kt().seed_table()
    before = table.model_dump()
    point = (0.4, 0.5, 0.2, 2)
    runs, best = _runs(_stats(*point), 100.0, 97.0)
    updated = _pt().update_k_table(table, _stats(*point), best, runs, threshold=0.02, radius=0.1)
    assert updated is not table
    assert table.model_dump() == before
    assert len(updated.entries) == 256
    changed = [e for e in updated.entries if e not in set(table.entries)]
    assert len(changed) == 1
    assert (changed[0].K1, changed[0].K2) == pytest.approx((2.5, 1.25))
    assert (_entry_at(updated, point).K1, _entry_at(updated, point).K2) == pytest.approx(
        (2.5, 1.25)
    )


def test_update_moves_the_nearest_entry_within_the_radius() -> None:
    table = _kt().seed_table()
    stats = _stats(0.41, 0.5, 0.2, 2)  # 0.01 from (0.4, 0.5, 0.2, 2), >= 0.19 from the rest
    runs, best = _runs(stats, 100.0, 95.0)
    updated = _pt().update_k_table(table, stats, best, runs, threshold=0.02, radius=0.1)
    assert len(updated.entries) == 256
    removed = [e for e in table.entries if e not in set(updated.entries)]
    added = [e for e in updated.entries if e not in set(table.entries)]
    assert [(e.tau, e.R, e.eta, e.m) for e in removed] == [(0.4, 0.5, 0.2, 2)]
    assert len(added) == 1
    assert (added[0].K1, added[0].K2) == pytest.approx((2.5, 1.25))


def test_update_adds_an_entry_when_none_is_within_the_radius() -> None:
    table = _kt().seed_table()
    before = table.model_dump()
    stats = _stats(0.5, 0.35, 0.125, 3)  # >= 0.4 from every seed point
    runs, best = _runs(stats, 100.0, 90.0)
    updated = _pt().update_k_table(table, stats, best, runs, threshold=0.02, radius=0.1)
    assert table.model_dump() == before
    assert len(updated.entries) == 257
    assert set(table.entries) <= set(updated.entries)
    (new,) = [e for e in updated.entries if e not in set(table.entries)]
    assert (new.tau, new.R, new.eta, new.m) == pytest.approx((0.5, 0.35, 0.125, 3))
    assert (new.K1, new.K2) == pytest.approx((2.5, 1.25))
    assert _kt().lookup(updated, stats) == pytest.approx((2.5, 1.25))


def test_update_at_exactly_the_threshold_improvement_updates() -> None:
    table = _kt().seed_table()
    point = (0.4, 0.5, 0.2, 2)
    runs, best = _runs(_stats(*point), 50.0, 49.0)  # exactly 2 percent
    updated = _pt().update_k_table(table, _stats(*point), best, runs, threshold=0.02, radius=0.1)
    assert updated != table
    assert (_entry_at(updated, point).K1, _entry_at(updated, point).K2) == pytest.approx(
        (2.5, 1.25)
    )


@pytest.mark.parametrize(
    ("base_value", "best_value"),
    [(100.0, 99.0), (100.0, 98.5), (100.0, 100.0), (0.0, 0.0), (None, 50.0), (100.0, None)],
    ids=["1pct", "1.5pct", "equal", "zero-base", "no-base-value", "no-best-value"],
)
def test_update_below_threshold_or_without_values_leaves_the_table_equal(
    base_value: float | None, best_value: float | None
) -> None:
    table = _kt().seed_table()
    point = (0.4, 0.5, 0.2, 2)
    runs, best = _runs(_stats(*point), base_value, best_value)
    updated = _pt().update_k_table(table, _stats(*point), best, runs, threshold=0.02, radius=0.1)
    assert updated == table


def test_update_respects_a_custom_threshold() -> None:
    table = _kt().seed_table()
    point = (0.4, 0.5, 0.2, 2)
    runs, best = _runs(_stats(*point), 100.0, 97.0)
    kept = _pt().update_k_table(table, _stats(*point), best, runs, threshold=0.05, radius=0.1)
    assert kept == table


def test_update_with_the_base_as_best_and_an_atcs_run_as_stats_leaves_the_table_equal() -> None:
    table = _kt().seed_table()
    stats = _stats(0.4, 0.5, 0.2, 2)
    runs, _best = _runs(stats, 100.0, 97.0)
    updated = _pt().update_k_table(table, runs[0], runs[0], runs, threshold=0.02, radius=0.1)
    assert updated == table


# --- learning never hurts held-out ATCS (Pinedo p.488-489, slow) ------------------------------


def _atcs_with(table: Any) -> Solver:
    return get_solver("atcs", k_lookup=_kt().as_k_lookup(table))


def _held_out_mean(table: Any) -> float:
    return statistics.fmean(
        score(lambda: _atcs_with(table), seed, bench=HELD_OUT_BENCH) for seed in HELD_OUT_SEEDS
    )


def _training_request(seed: int) -> SolveRequest:
    request = hmlv_instance(seed)
    objective = bench_profile(reference_values(TRAINING_BENCH, seed))
    profile = request.profile.model_copy(update={"objective": objective})
    return request.model_copy(update={"profile": profile})


def _training_round(table: Any) -> Any:
    for seed in TRAINING_SEEDS:
        request = _training_request(seed)
        for center in request.problem.work_centers:
            runs = _pt().perturbation_runs(request, center.id, table)
            valued = [r for r in runs if r.objective_value is not None]
            best = min(valued, key=lambda r: r.objective_value)
            table = _pt().update_k_table(table, runs[0], best, runs)
    return table


@pytest.mark.slow
def test_ten_training_rounds_never_worsen_held_out_atcs_by_more_than_1_percent() -> None:
    table = _kt().seed_table()
    seed_mean = _held_out_mean(table)
    means = []
    for _ in range(ROUNDS):
        table = _training_round(table)
        means.append(_held_out_mean(table))
    print(f"seed table {seed_mean:.4f}; rounds " + " ".join(f"{m:.4f}" for m in means))
    assert all(m <= seed_mean * (1 + MAX_WORSENING) for m in means), (seed_mean, means)
