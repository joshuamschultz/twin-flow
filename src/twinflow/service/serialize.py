"""Serialize engine results to plain JSON-friendly dicts for the API layer.

Keeps every engine dataclass (`KpiSet`, `AggregatedKpis`, `Evaluation`,
`OptimizationResult`) out of the HTTP boundary: the API returns dicts, the front end
reads dicts. The KPI shape mirrors the shipped `KpiJsonSidecar` where they overlap, so
one contract describes a KPI whether it comes from a file or the API.
"""

from __future__ import annotations

from typing import Any

from twinflow.instrumentation.aggregate import AggregatedKpis, Interval, OutcomeDistribution
from twinflow.instrumentation.kpis import KpiSet
from twinflow.modules.optimizers import OptimizationResult
from twinflow.modules.surface import Evaluation


def kpis_to_dict(kpis: KpiSet) -> dict[str, Any]:
    """The representative-replication KPIs a report/table renders."""
    return {
        "on_time_pct": kpis.on_time_pct,
        "completion_by_order": dict(kpis.completion_by_order),
        "lateness_by_order": dict(kpis.lateness_by_order),
        "utilization_by_cell": dict(kpis.utilization_by_cell),
        "wait_seconds_by_location": {k: dict(v) for k, v in kpis.wait_seconds_by_location.items()},
        "machine_hours_by_machine": dict(kpis.machine_hours_by_machine),
        "run_hours": kpis.run_hours,
        "setup_hours": kpis.setup_hours,
        "event_counts_by_location": dict(kpis.event_counts_by_location),
        "order_outcomes": {
            key: {
                "required_qty": outcome.required_qty,
                "accepted_qty": outcome.accepted_qty,
                "scrapped_qty": outcome.scrapped_qty,
                "shipped_qty": outcome.shipped_qty,
                "remaining_qty": outcome.remaining_qty,
                "status": outcome.status,
                "completion_time": outcome.completion_time,
                "due_state": outcome.due_state,
            }
            for key, outcome in kpis.order_outcomes.items()
        },
        "on_time_denominator": kpis.on_time_denominator,
        "completed_order_count": kpis.completed_order_count,
        "censored_order_count": kpis.censored_order_count,
    }


def interval_to_dict(interval: Interval) -> dict[str, float | int]:
    return {
        "mean": interval.mean,
        "lo": interval.lo,
        "hi": interval.hi,
        "p50": interval.p50,
        "n": interval.n,
    }


def distribution_to_dict(distribution: OutcomeDistribution) -> dict[str, Any]:
    """Serialize explicitly named outcome and sampling uncertainty."""
    return {
        "mean": distribution.mean,
        "mean_ci": distribution.mean_ci,
        "quantiles": distribution.quantiles,
        "observed_count": distribution.observed_count,
        "censored_count": distribution.censored_count,
        "estimability": distribution.estimability,
    }


def intervals_to_dict(aggregated: AggregatedKpis) -> dict[str, Any]:
    """The confidence bands across replications — the honest range, not a point."""
    return {
        "reps": aggregated.reps,
        "level": aggregated.level,
        "on_time_pct": interval_to_dict(aggregated.on_time_pct),
        "lateness_by_order": {
            k: interval_to_dict(v) for k, v in aggregated.lateness_by_order.items()
        },
        "completion_by_order": {
            k: interval_to_dict(v) for k, v in aggregated.completion_by_order.items()
        },
        "utilization_by_cell": {
            k: interval_to_dict(v) for k, v in aggregated.utilization_by_cell.items()
        },
        "completion_distributions": {
            key: distribution_to_dict(value)
            for key, value in aggregated.completion_distributions.items()
        },
        "lateness_distributions": {
            key: distribution_to_dict(value)
            for key, value in aggregated.lateness_distributions.items()
        },
    }


def evaluation_to_dict(evaluation: Evaluation) -> dict[str, Any]:
    """One scored scenario: its levers, representative KPIs, and confidence band."""
    return {
        "levers": dict(evaluation.scenario.levers),
        "kpis": kpis_to_dict(evaluation.kpis),
        "intervals": intervals_to_dict(evaluation.intervals),
    }


def optimization_to_dict(result: OptimizationResult) -> dict[str, Any]:
    """The whole study: the winning scenario, its score, and the convergence trail."""
    return {
        "objective": result.objective_name,
        "direction": result.direction,
        "best": evaluation_to_dict(result.best),
        "best_score": result.best_score,
        "evaluations_used": result.evaluations_used,
        "history": [
            {"levers": dict(scenario.levers), "score": score} for scenario, score in result.history
        ],
    }
