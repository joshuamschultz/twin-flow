"""COMP-020 ReplicationRunner — N replications in parallel, one per process.

Confidence intervals across replications use a SciPy t-quantile. A paired analysis is a
CI on the difference, never two overlapping CIs.
"""

from __future__ import annotations

import math
import multiprocessing
import os
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

import scipy.stats  # type: ignore[import-untyped]  # no stub package; SciPy is the D-036-endorsed CI library

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver, RunResult
from twinflow.plan.loader import WorkOrder

__all__ = ["ReplicationRunner", "DifferenceCI", "confidence_interval", "compare"]


def _run_one_replication(
    model_path: str,
    plan: list[WorkOrder],
    base_seed: int,
    replication_index: int,
) -> RunResult:
    """Per-process worker (D-028). MODULE-LEVEL so macOS `spawn` can pickle it
    by qualified name, and takes only picklable arguments: a model PATH, never
    a `CompiledModel` — its `LocationSpec.OutputSpec` carries `simpleeval`
    closures that never survive a `spawn` pickle. Loads the model fresh inside
    THIS process, then runs exactly one replication so `replication_index`
    threads through to `RngRegistry`'s per-source streams (CRN, D-033).
    """
    compiled = load_model(model_path)
    driver = RunDriver(compiled)
    return driver.run(plan, seed=base_seed, replication_index=replication_index)


class ReplicationRunner:
    """Runs `reps` replications of one configuration in parallel, one
    replication per OS process via `multiprocessing.Pool` (D-028). Each
    replication uses `replication_index=i`, so per-source RNG streams stay
    reproducible and CRN survives across a parallel dispatch (D-033).
    """

    def __init__(self, model_path: str) -> None:
        self._model_path = model_path

    def run(self, plan: list[WorkOrder], reps: int, base_seed: int) -> list[RunResult]:
        """Dispatch `reps` replications, each to its own process, and return
        their `RunResult`s. `base_seed` and the replication index are threaded
        through explicitly to every worker — never a per-worker default."""
        args = [(self._model_path, plan, base_seed, index) for index in range(reps)]
        processes = min(reps, os.cpu_count() or reps)
        with multiprocessing.Pool(processes=processes) as pool:
            return pool.starmap(_run_one_replication, args)


def confidence_interval(values: Sequence[float], confidence: float = 0.95) -> tuple[float, float]:
    """A plain-float (low, high) CI on the mean of `values`: mean +/- t_crit *
    (sample_stdev / sqrt(n)), with the t-quantile from `scipy.stats.t`, cast
    to a plain Python float (Polars has no t-distribution quantile — tech.md).
    """
    n = len(values)
    if n < 2:
        raise ValueError("confidence_interval requires at least 2 values")
    mean = statistics.fmean(values)
    t_critical = float(scipy.stats.t.ppf(1 - (1 - confidence) / 2, df=n - 1))
    stderr = statistics.stdev(values) / math.sqrt(n)
    return (mean - t_critical * stderr, mean + t_critical * stderr)


@dataclass(frozen=True)
class DifferenceCI:
    """A single paired-difference confidence interval. `compare()` never
    returns a pair of overlapping per-config intervals (SDD COMP-020)."""

    mean_difference: float
    low: float
    high: float
    t_critical: float


def compare(
    model_path_a: str,
    model_path_b: str,
    plan: list[WorkOrder],
    reps: int,
    base_seed: int,
    metric: Callable[[RunResult], float],
    confidence: float = 0.95,
) -> DifferenceCI:
    """Run `reps` replications of BOTH configs at the same `base_seed`, so
    replication i of A pairs with replication i of B (CRN, D-033), then return
    ONE interval on `mean(metric(B_i) - metric(A_i))` — never two overlapping
    per-config CIs."""
    results_a = {
        cast(int, result.run_meta["replication_index"]): result
        for result in ReplicationRunner(model_path_a).run(plan, reps, base_seed)
    }
    results_b = {
        cast(int, result.run_meta["replication_index"]): result
        for result in ReplicationRunner(model_path_b).run(plan, reps, base_seed)
    }
    diffs = [metric(results_b[index]) - metric(results_a[index]) for index in range(reps)]

    n = len(diffs)
    mean_difference = statistics.fmean(diffs)
    t_critical = float(scipy.stats.t.ppf(1 - (1 - confidence) / 2, df=n - 1))
    stderr = 0.0 if statistics.pstdev(diffs) == 0.0 else statistics.stdev(diffs) / math.sqrt(n)
    low = mean_difference - t_critical * stderr
    high = mean_difference + t_critical * stderr
    return DifferenceCI(mean_difference=mean_difference, low=low, high=high, t_critical=t_critical)
