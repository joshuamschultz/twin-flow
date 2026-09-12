"""Replication aggregation with distinct outcome quantiles and sampling uncertainty.

A single replication is one sample of a random floor. This module reports outcome
quantiles separately from a Student-t confidence interval on an estimated mean.
Completion distributions name censoring and do not calculate quantiles from only
the completed subset. The legacy `Interval` view remains for compatible consumers;
its `lo` and `hi` now carry the mean confidence interval.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import scipy.stats  # type: ignore[import-untyped]

from twinflow.instrumentation.kpis import KpiSet

DEFAULT_LEVEL = 0.90


@dataclass(frozen=True, slots=True)
class Interval:
    """Compatibility view: mean CI (`lo`, `hi`) plus observed median."""

    mean: float
    lo: float
    hi: float
    p50: float
    n: int


@dataclass(frozen=True, slots=True)
class OutcomeDistribution:
    """Truthful separation of outcome spread, mean uncertainty, and censoring."""

    mean: float | None
    mean_ci: tuple[float, float] | None
    quantiles: dict[str, float] | None
    observed_count: int
    censored_count: int
    estimability: str


@dataclass(frozen=True, slots=True)
class AggregatedKpis:
    """Per-metric intervals across `reps` replications at confidence `level`."""

    on_time_pct: Interval
    lateness_by_order: dict[str, Interval]
    completion_by_order: dict[str, Interval]
    utilization_by_cell: dict[str, Interval]
    reps: int
    level: float
    completion_distributions: dict[str, OutcomeDistribution] = field(default_factory=dict)
    lateness_distributions: dict[str, OutcomeDistribution] = field(default_factory=dict)


def aggregate_kpis(per_rep: Sequence[KpiSet], level: float = DEFAULT_LEVEL) -> AggregatedKpis:
    """Aggregate per-replication KpiSets into confidence intervals."""
    if not per_rep:
        raise ValueError("aggregate_kpis needs at least one replication")

    if not 0.0 < level < 1.0:
        raise ValueError("level must be between zero and one")
    completion_maps = [k.completion_by_order for k in per_rep]
    lateness_maps = [k.lateness_by_order for k in per_rep]
    return AggregatedKpis(
        on_time_pct=_interval([k.on_time_pct for k in per_rep], level),
        lateness_by_order=_interval_by_key([k.lateness_by_order for k in per_rep], level),
        completion_by_order=_interval_by_key([k.completion_by_order for k in per_rep], level),
        utilization_by_cell=_interval_by_key([k.utilization_by_cell for k in per_rep], level),
        reps=len(per_rep),
        level=level,
        completion_distributions=_distributions_by_key(completion_maps, level),
        lateness_distributions=_distributions_by_key(lateness_maps, level),
    )


def _interval(values: Sequence[float | None], level: float) -> Interval:
    present = [float(v) for v in values if v is not None]
    arr = np.array(present, dtype=float)
    if arr.size == 0:
        raise ValueError("interval needs at least one observed value")
    mean = float(arr.mean())
    if arr.size == 1:
        low = high = mean
    else:
        critical = float(scipy.stats.t.ppf((1.0 + level) / 2.0, arr.size - 1))
        half_width = critical * float(arr.std(ddof=1)) / float(np.sqrt(arr.size))
        low, high = mean - half_width, mean + half_width
    return Interval(
        mean=mean,
        lo=low,
        hi=high,
        p50=float(np.percentile(arr, 50.0)),
        n=len(present),
    )


def _interval_by_key(
    dicts: Sequence[Mapping[str, float | None]], level: float
) -> dict[str, Interval]:
    keys = {key for d in dicts for key in d}
    result: dict[str, Interval] = {}
    for key in sorted(keys):
        present = [d[key] for d in dicts if d.get(key) is not None]
        if present:
            result[key] = _interval(present, level)
    return result


def _distributions_by_key(
    dicts: Sequence[Mapping[str, float | None]], level: float
) -> dict[str, OutcomeDistribution]:
    keys = {key for values in dicts for key in values}
    result: dict[str, OutcomeDistribution] = {}
    for key in sorted(keys):
        samples = [values.get(key) for values in dicts]
        observed = [float(value) for value in samples if value is not None]
        censored = len(samples) - len(observed)
        if not observed:
            result[key] = OutcomeDistribution(None, None, None, 0, censored, "not_estimable")
            continue
        interval = _interval(observed, level)
        quantiles = None
        estimability = "not_estimable" if censored else "estimated"
        if not censored:
            array = np.asarray(observed, dtype=float)
            quantiles = {
                "p10": float(np.percentile(array, 10.0)),
                "p50": float(np.percentile(array, 50.0)),
                "p90": float(np.percentile(array, 90.0)),
            }
        result[key] = OutcomeDistribution(
            interval.mean,
            (interval.lo, interval.hi),
            quantiles,
            len(observed),
            censored,
            estimability,
        )
    return result
