"""COMP-039 KPI aggregation across replications — turns N per-replication KpiSets
into confidence intervals (mean + low/high), the "confidence range, not a guess"
the README promises. Percentile-based so a skewed floor's spread is reported
honestly. With one replication the interval collapses to the point (lo==hi==mean).
"""

from __future__ import annotations

import polars as pl
import pytest

from twinflow.instrumentation.aggregate import Interval, aggregate_kpis
from twinflow.instrumentation.kpis import KpiSet


def _kpis(
    *,
    on_time_pct: float,
    lateness: dict[str, float | None] | None = None,
    completion: dict[str, float | None] | None = None,
    utilization: dict[str, float] | None = None,
) -> KpiSet:
    """A KpiSet carrying only the fields the aggregator reads; the rest are empty."""
    return KpiSet(
        completion_by_order=completion or {},
        lateness_by_order=lateness or {},
        on_time_pct=on_time_pct,
        utilization_by_cell=utilization or {},
        utilization_by_machine={},
        wait_seconds_by_location={},
        labor_pool_utilization={},
        wip_by_location=pl.DataFrame({"location_id": [], "t": [], "wip": []}),
        machine_hours_by_machine={},
        labor_hours_by_pool_skill={},
        run_hours=0.0,
        setup_hours=0.0,
        event_counts_by_location={},
    )


def test_on_time_pct_becomes_an_interval() -> None:
    reps = [_kpis(on_time_pct=p) for p in (40.0, 50.0, 60.0, 70.0, 80.0)]
    agg = aggregate_kpis(reps, level=0.90)
    assert isinstance(agg.on_time_pct, Interval)
    assert agg.on_time_pct.mean == pytest.approx(60.0)
    assert agg.on_time_pct.p50 == pytest.approx(60.0)
    assert agg.on_time_pct.lo < agg.on_time_pct.mean < agg.on_time_pct.hi
    assert agg.on_time_pct.n == 5
    assert agg.reps == 5
    assert agg.level == 0.90


def test_single_replication_interval_collapses_to_the_point() -> None:
    agg = aggregate_kpis([_kpis(on_time_pct=73.0)], level=0.90)
    iv = agg.on_time_pct
    assert iv.mean == iv.lo == iv.hi == iv.p50 == 73.0
    assert iv.n == 1


def test_per_order_lateness_aggregates_by_key() -> None:
    reps = [
        _kpis(on_time_pct=0.0, lateness={"o1": 1.0, "o2": -2.0}),
        _kpis(on_time_pct=0.0, lateness={"o1": 3.0, "o2": -1.0}),
        _kpis(on_time_pct=0.0, lateness={"o1": 5.0, "o2": 0.0}),
    ]
    agg = aggregate_kpis(reps, level=0.90)
    assert agg.lateness_by_order["o1"].mean == pytest.approx(3.0)
    assert agg.lateness_by_order["o2"].mean == pytest.approx(-1.0)
    assert agg.lateness_by_order["o1"].hi > agg.lateness_by_order["o1"].lo


def test_missing_values_are_dropped_not_counted() -> None:
    """An order that did not complete in some reps is aggregated over the reps
    where it did, and n reflects that."""
    reps = [
        _kpis(on_time_pct=0.0, completion={"o1": 100.0}),
        _kpis(on_time_pct=0.0, completion={"o1": None}),
        _kpis(on_time_pct=0.0, completion={"o1": 200.0}),
    ]
    agg = aggregate_kpis(reps, level=0.90)
    assert agg.completion_by_order["o1"].n == 2
    assert agg.completion_by_order["o1"].mean == pytest.approx(150.0)


def test_utilization_aggregates_per_cell() -> None:
    reps = [
        _kpis(on_time_pct=0.0, utilization={"mill": 0.90, "saw": 0.30}),
        _kpis(on_time_pct=0.0, utilization={"mill": 0.99, "saw": 0.35}),
    ]
    agg = aggregate_kpis(reps, level=0.90)
    assert agg.utilization_by_cell["mill"].mean == pytest.approx(0.945)
    assert agg.utilization_by_cell["saw"].mean == pytest.approx(0.325)


def test_empty_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        aggregate_kpis([], level=0.90)
