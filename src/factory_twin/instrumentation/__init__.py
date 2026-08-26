"""Layer 4 — ProcessExecution log -> KPIs -> sweep harness. Reads the event table only."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import polars as pl

from factory_twin.instrumentation.event_log import EVENT_LOG_SCHEMA
from factory_twin.instrumentation.kpis import KpiEngine, KpiSet

# EVENT_LOG_SCHEMA is the contract, defined once in event_log.py and re-exported here so
# every consumer imports it from one place. Applied identically in every worker process
# (D-017).
__all__ = ["EVENT_LOG_SCHEMA", "KpiSet", "compute_kpis", "run_sweep"]


def compute_kpis(
    event_paths: str | Path | Sequence[str | Path],
    orders: pl.DataFrame,
    horizon: float,
    labor_pool_capacity: dict[str, int] | int = 1,
) -> KpiSet:
    """Public API: every reported KPI from the single event table. A thin facade
    delegating to `KpiEngine` (COMP-023) — no KPI is computed anywhere outside
    that component."""
    return KpiEngine().compute(
        event_paths=event_paths,
        orders=orders,
        horizon=horizon,
        labor_pool_capacity=labor_pool_capacity,
    )


def run_sweep(model: object, plan: list[object], sweep_spec: object, reps: int) -> object:
    """Public API: run a declared lever grid; select no winner."""
    raise NotImplementedError("T-041")
