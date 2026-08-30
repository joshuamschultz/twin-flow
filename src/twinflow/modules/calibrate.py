"""Calibration — tune a model until its simulated KPIs match a real run.

This is the trust loop the buyer signs on: "the twin reproduced my last 8 weeks within
X%." It is *just optimization* over the module surface — the parameters to tune (a
cycle-time spread, a rate, a scrap fraction, a reorder point) are ordinary config
levers, and the objective is the distance between the twin's KPIs and the observed ones.
So calibration reuses the shipped `ScoringSurface` + optimizers with no new machinery.

The observed `KpiSet` comes from a real production event log the same way a simulated
one does — `compute_kpis` over the log (read via an `ActualSource`), since real and
simulated logs share one schema. Calibration proposes parameter values; a human still
decides whether the fit is good enough to trust.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from twinflow.instrumentation.kpis import KpiSet
from twinflow.modules.objectives import MetricObjective, Objective
from twinflow.modules.optimizers import OptimizationResult, Optimizer
from twinflow.modules.space import LeverSpace
from twinflow.modules.surface import ScoringSurface
from twinflow.plan.loader import WorkOrder

# The KPI fields calibration can match on. Scalars diff directly; dict fields diff as the
# mean absolute gap over the union of their keys.
_SCALAR_METRICS = ("on_time_pct", "run_hours", "setup_hours")
_DICT_METRICS = ("utilization_by_cell", "machine_hours_by_machine")
_OPTIONAL_DICT_METRICS = ("lateness_by_order", "completion_by_order")


@dataclass(frozen=True)
class CalibrationTarget:
    """What to match: an observed `KpiSet` and per-metric weights.

    `weights` names which KPIs matter and how much (e.g.
    `{"on_time_pct": 1.0, "run_hours": 0.2}`). A weight is a plain assumption the
    caller supplies; the distance is their weighted sum.
    """

    observed: KpiSet
    weights: dict[str, float] = field(default_factory=lambda: {"on_time_pct": 1.0})

    def __post_init__(self) -> None:
        known = set(_SCALAR_METRICS) | set(_DICT_METRICS) | set(_OPTIONAL_DICT_METRICS)
        unknown = set(self.weights) - known
        if unknown:
            raise ValueError(f"unknown calibration metric(s): {sorted(unknown)}")
        if not self.weights:
            raise ValueError("CalibrationTarget needs at least one weighted metric")


@dataclass(frozen=True)
class CalibrationResult:
    """The calibration outcome: the best-fit parameters and how close they got."""

    best_params: dict[str, Any]
    distance: float
    within_tolerance: bool
    search: OptimizationResult = field(repr=False)


def distance_to_observed(simulated: KpiSet, target: CalibrationTarget) -> float:
    """Weighted distance between a simulated `KpiSet` and the observed one."""
    total = 0.0
    for metric, weight in target.weights.items():
        total += weight * _metric_distance(metric, simulated, target.observed)
    return total


def calibration_objective(target: CalibrationTarget) -> Objective:
    """A minimise objective scoring an `Evaluation` by its distance to `target`."""
    return MetricObjective(
        "calibration_distance",
        "min",
        lambda evaluation: distance_to_observed(evaluation.kpis, target),
    )


def calibrate(
    model_path: str,
    plan: list[WorkOrder],
    space: LeverSpace,
    target: CalibrationTarget,
    optimizer: Optimizer,
    budget: int,
    tolerance: float = 0.0,
    reps: int = 20,
    base_seed: int = 0,
    seed: int = 0,
) -> CalibrationResult:
    """Search `space` for the parameter values whose simulated KPIs are closest to
    `target.observed`. `tolerance` is the distance below which the fit is accepted
    (`within_tolerance`). Everything else mirrors `optimize`.
    """
    surface = ScoringSurface(model_path, plan, reps=reps, base_seed=base_seed)
    result = optimizer.optimize(
        surface, space, calibration_objective(target), budget=budget, seed=seed
    )
    return CalibrationResult(
        best_params=dict(result.best.scenario.levers),
        distance=result.best_score,
        within_tolerance=result.best_score <= tolerance,
        search=result,
    )


def _metric_distance(metric: str, simulated: KpiSet, observed: KpiSet) -> float:
    if metric in _SCALAR_METRICS:
        return float(abs(getattr(simulated, metric) - getattr(observed, metric)))
    if metric in _DICT_METRICS:
        return _dict_distance(getattr(simulated, metric), getattr(observed, metric))
    return _optional_dict_distance(getattr(simulated, metric), getattr(observed, metric))


def _dict_distance(sim: Mapping[str, float], obs: Mapping[str, float]) -> float:
    keys = set(sim) | set(obs)
    if not keys:
        return 0.0
    return sum(abs(sim.get(k, 0.0) - obs.get(k, 0.0)) for k in keys) / len(keys)


def _optional_dict_distance(
    sim: Mapping[str, float | None], obs: Mapping[str, float | None]
) -> float:
    keys = set(sim) | set(obs)
    if not keys:
        return 0.0
    return sum(abs((sim.get(k) or 0.0) - (obs.get(k) or 0.0)) for k in keys) / len(keys)
