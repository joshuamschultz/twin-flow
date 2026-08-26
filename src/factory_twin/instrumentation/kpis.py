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
        pass

    def classify(
        self,
        queue_arrival_time: float,
        material_ready_time: float | None,
        actual_start: float,
        actual_end: float,
        release_time: float,
    ) -> tuple[float, float, float]:
        """Split one row's total wait into (starved, blocked, material_starved) seconds.

        Binding-constraint rule (D-034): blocked is always
        `release_time - actual_end`. Pre-start wait (`actual_start -
        queue_arrival_time`) is attributed entirely to whichever input became
        available last — material if `material_ready_time` is at or after
        `actual_start`, otherwise the resource (starved), including the
        nullable case where no material gate is tracked at all.
        """
        pre_start_wait = actual_start - queue_arrival_time
        blocked = release_time - actual_end

        if material_ready_time is None or material_ready_time < actual_start:
            starved = pre_start_wait
            material_starved = 0.0
        else:
            starved = 0.0
            material_starved = pre_start_wait

        return starved, blocked, material_starved


class KpiEngine:
    """The five headline outputs plus the objective-agnostic minimum set."""

    def __init__(self) -> None:
        raise NotImplementedError("T-037")
