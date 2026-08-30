"""Plan-vs-actual reconciliation — the seam that turns the twin from "simulate only"
into "simulate and reconcile."

An `ActualSource` (see `connectors.py`) reads a real production event log; because it
shares the twin's event schema, the shipped `KpiEngine` computes the *actual* KPIs the
same way it computes planned ones. `reconcile` then diffs planned against actual into a
`Variance`: schedule adherence, throughput drift, and per-center utilization gaps — the
numbers a planner uses to trust (or correct) the model.
"""

from __future__ import annotations

from dataclasses import dataclass

from twinflow.instrumentation.kpis import KpiSet


@dataclass(frozen=True)
class Variance:
    """Planned-minus-actual gaps across the metrics a planner reconciles on."""

    planned_on_time_pct: float
    actual_on_time_pct: float
    on_time_delta: float
    planned_run_hours: float
    actual_run_hours: float
    run_hours_delta: float
    utilization_delta_by_cell: dict[str, float]
    lateness_delta_by_order: dict[str, float]


def reconcile(planned: KpiSet, actual: KpiSet) -> Variance:
    """Diff a planned `KpiSet` (from a twin run) against an actual one (from a real
    event log via `compute_kpis`). Deltas are `actual - planned`, so a positive
    `on_time_delta` means the floor beat the plan and a positive `run_hours_delta`
    means it ran longer than modelled.
    """
    utilization_delta = _diff_maps(planned.utilization_by_cell, actual.utilization_by_cell)
    lateness_delta = _diff_optional_maps(planned.lateness_by_order, actual.lateness_by_order)
    return Variance(
        planned_on_time_pct=planned.on_time_pct,
        actual_on_time_pct=actual.on_time_pct,
        on_time_delta=actual.on_time_pct - planned.on_time_pct,
        planned_run_hours=planned.run_hours,
        actual_run_hours=actual.run_hours,
        run_hours_delta=actual.run_hours - planned.run_hours,
        utilization_delta_by_cell=utilization_delta,
        lateness_delta_by_order=lateness_delta,
    )


def _diff_maps(planned: dict[str, float], actual: dict[str, float]) -> dict[str, float]:
    keys = set(planned) | set(actual)
    return {key: actual.get(key, 0.0) - planned.get(key, 0.0) for key in sorted(keys)}


def _diff_optional_maps(
    planned: dict[str, float | None], actual: dict[str, float | None]
) -> dict[str, float]:
    """Diff maps whose values may be `None` (an order that never completed); a
    missing or unknown side is treated as 0 so the delta is always a real number."""
    keys = set(planned) | set(actual)
    result: dict[str, float] = {}
    for key in sorted(keys):
        result[key] = (actual.get(key) or 0.0) - (planned.get(key) or 0.0)
    return result
