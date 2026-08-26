"""Layer 4 — ProcessExecution log -> KPIs -> sweep harness. Reads the event table only."""

from __future__ import annotations

from factory_twin.instrumentation.event_log import EVENT_LOG_SCHEMA

# EVENT_LOG_SCHEMA is the contract, defined once in event_log.py and re-exported here so
# every consumer imports it from one place. Applied identically in every worker process
# (D-017).
__all__ = ["EVENT_LOG_SCHEMA", "compute_kpis", "run_sweep"]


def compute_kpis(event_paths: list[object], horizon_table: object) -> object:
    """Public API: every reported KPI from the single event table."""
    raise NotImplementedError("T-037")


def run_sweep(model: object, plan: list[object], sweep_spec: object, reps: int) -> object:
    """Public API: run a declared lever grid; select no winner."""
    raise NotImplementedError("T-041")
