"""Layer 4 — ProcessExecution log -> KPIs -> sweep harness. Reads the event table only."""

from __future__ import annotations

# EVENT_LOG_SCHEMA is the contract, defined once here and applied identically in every
# worker process (D-017). Populated by T-019.
EVENT_LOG_SCHEMA: object | None = None


def compute_kpis(event_paths: list[object], horizon_table: object) -> object:
    """Public API: every reported KPI from the single event table."""
    raise NotImplementedError("T-037")


def run_sweep(model: object, plan: list[object], sweep_spec: object, reps: int) -> object:
    """Public API: run a declared lever grid; select no winner."""
    raise NotImplementedError("T-041")
