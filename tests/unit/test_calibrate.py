"""Unit tests for calibration (the trust-loop core): the distance function, the
calibration objective, and target validation. These use synthetic `KpiSet`s and never
run the simulator; `calibrate()` end to end over the real surface is covered lightly in
tests/integration/test_modules_surface.py-style paths elsewhere.
"""

from __future__ import annotations

import polars as pl
import pytest

from twinflow.instrumentation.aggregate import AggregatedKpis, Interval
from twinflow.instrumentation.kpis import KpiSet
from twinflow.modules import (
    CalibrationTarget,
    Scenario,
    calibration_objective,
    distance_to_observed,
)
from twinflow.modules.surface import Evaluation


def _kpis(
    on_time: float,
    run_hours: float = 10.0,
    utilization: dict[str, float] | None = None,
) -> KpiSet:
    return KpiSet(
        completion_by_order={"wo-1": 100.0},
        lateness_by_order={"wo-1": -50.0},
        on_time_pct=on_time,
        utilization_by_cell=utilization if utilization is not None else {"cut": 0.5},
        utilization_by_machine={"cut": 0.5},
        wait_seconds_by_location={},
        labor_pool_utilization={"default": 0.5},
        wip_by_location=pl.DataFrame(),
        machine_hours_by_machine={},
        labor_hours_by_pool_skill={},
        run_hours=run_hours,
        setup_hours=0.0,
        event_counts_by_location={},
    )


def _evaluation(kpis: KpiSet) -> Evaluation:
    interval = Interval(kpis.on_time_pct, 0.0, 100.0, kpis.on_time_pct, 3)
    aggregated = AggregatedKpis(interval, {}, {}, {}, reps=3, level=0.9)
    return Evaluation(Scenario(levers={}), kpis, aggregated, None)  # type: ignore[arg-type]


def test_distance_scalar_metric() -> None:
    target = CalibrationTarget(_kpis(80.0), weights={"on_time_pct": 1.0})
    assert distance_to_observed(_kpis(70.0), target) == pytest.approx(10.0)


def test_distance_weighted_multi_metric() -> None:
    target = CalibrationTarget(
        _kpis(80.0, run_hours=10.0), weights={"on_time_pct": 1.0, "run_hours": 0.5}
    )
    # |70-80|*1 + |12-10|*0.5 = 10 + 1 = 11
    assert distance_to_observed(_kpis(70.0, run_hours=12.0), target) == pytest.approx(11.0)


def test_distance_dict_metric_is_mean_abs_gap() -> None:
    observed = _kpis(80.0, utilization={"a": 0.5, "b": 0.4})
    simulated = _kpis(80.0, utilization={"a": 0.6, "b": 0.4})
    target = CalibrationTarget(observed, weights={"utilization_by_cell": 1.0})
    # (|0.6-0.5| + |0.4-0.4|) / 2 = 0.05
    assert distance_to_observed(simulated, target) == pytest.approx(0.05)


def test_calibration_objective_minimises_distance() -> None:
    target = CalibrationTarget(_kpis(80.0), weights={"on_time_pct": 1.0})
    objective = calibration_objective(target)
    assert objective.direction == "min"
    assert objective.score(_evaluation(_kpis(80.0))) == pytest.approx(0.0)
    assert objective.score(_evaluation(_kpis(60.0))) == pytest.approx(20.0)


def test_target_rejects_unknown_metric() -> None:
    with pytest.raises(ValueError, match="unknown calibration metric"):
        CalibrationTarget(_kpis(80.0), weights={"not_a_kpi": 1.0})
