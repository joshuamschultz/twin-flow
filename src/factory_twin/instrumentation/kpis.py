"""COMP-022 WaitClassifier + COMP-023 KpiEngine.

WaitClassifier splits total wait into starved/blocked/material-starved by the binding-
constraint rule (the input that became available last owns the whole wait).
KpiEngine computes every KPI from the event table via lazy Polars over scan_parquet.
No KPI is computed anywhere outside this component.
"""

from __future__ import annotations


class WaitClassifier:
    """(category, seconds) per event row; blocked = release_time - actual_end."""

    def __init__(self) -> None:
        raise NotImplementedError("T-035")


class KpiEngine:
    """The five headline outputs plus the objective-agnostic minimum set."""

    def __init__(self) -> None:
        raise NotImplementedError("T-037")
