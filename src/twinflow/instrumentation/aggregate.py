"""COMP-039 KPI aggregation across replications.

A single replication is one sample of a random floor; the honest answer is a
range. This module turns N per-replication `KpiSet`s into `Interval`s (mean plus
a low/high confidence band) for the metrics a planner acts on: on-time %, per-
order lateness and completion, and utilization by work center.

The band is percentile-based (nonparametric), so a skewed distribution's spread
is reported as it actually falls rather than assumed symmetric. With one
replication every interval collapses to the point value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from twinflow.instrumentation.kpis import KpiSet

DEFAULT_LEVEL = 0.90


@dataclass(frozen=True, slots=True)
class Interval:
    """A metric summarised across replications: `mean`, the [`lo`, `hi`]
    confidence band, the median `p50`, and the sample count `n`."""

    mean: float
    lo: float
    hi: float
    p50: float
    n: int


@dataclass(frozen=True, slots=True)
class AggregatedKpis:
    """Per-metric intervals across `reps` replications at confidence `level`."""

    on_time_pct: Interval
    lateness_by_order: dict[str, Interval]
    completion_by_order: dict[str, Interval]
    utilization_by_cell: dict[str, Interval]
    reps: int
    level: float


def aggregate_kpis(per_rep: Sequence[KpiSet], level: float = DEFAULT_LEVEL) -> AggregatedKpis:
    """Aggregate per-replication KpiSets into confidence intervals."""
    if not per_rep:
        raise ValueError("aggregate_kpis needs at least one replication")

    return AggregatedKpis(
        on_time_pct=_interval([k.on_time_pct for k in per_rep], level),
        lateness_by_order=_interval_by_key([k.lateness_by_order for k in per_rep], level),
        completion_by_order=_interval_by_key([k.completion_by_order for k in per_rep], level),
        utilization_by_cell=_interval_by_key([k.utilization_by_cell for k in per_rep], level),
        reps=len(per_rep),
        level=level,
    )


def _interval(values: Sequence[float | None], level: float) -> Interval:
    present = [float(v) for v in values if v is not None]
    arr = np.array(present, dtype=float)
    tail = (1.0 - level) / 2.0 * 100.0
    return Interval(
        mean=float(arr.mean()),
        lo=float(np.percentile(arr, tail)),
        hi=float(np.percentile(arr, 100.0 - tail)),
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
