"""COMP-022 WaitClassifier + COMP-023 KpiEngine.

WaitClassifier splits total wait into starved/blocked/material-starved by the binding-
constraint rule (the input that became available last owns the whole wait).
KpiEngine computes every KPI from the event table via lazy Polars over scan_parquet.
No KPI is computed anywhere outside this component.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import polars as pl


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


@dataclass(frozen=True, slots=True)
class KpiSet:
    """Every reported KPI, computed once from the event table by `KpiEngine.compute`
    (D-017: all KPIs are Polars expressions over the single ProcessExecution log).
    """

    completion_by_order: dict[str, float | None]
    lateness_by_order: dict[str, float | None]
    on_time_pct: float
    utilization_by_cell: dict[str, float]
    utilization_by_machine: dict[str, float]
    wait_seconds_by_location: dict[str, dict[str, float]]
    labor_pool_utilization: dict[str, float]
    wip_by_location: pl.DataFrame
    machine_hours_by_machine: dict[str, float]
    labor_hours_by_pool_skill: dict[tuple[str, str], float]
    run_hours: float
    setup_hours: float
    event_counts_by_location: dict[str, int]


class KpiEngine:
    """The five headline outputs plus the objective-agnostic minimum set (D-017,
    D-034, D-042). Reads the event table only via lazy `scan_parquet`; no KPI is
    computed anywhere outside this component.
    """

    def compute(
        self,
        event_paths: str | Path | Sequence[str | Path],
        orders: pl.DataFrame,
        horizon: float,
        labor_pool_capacity: dict[str, int] | int = 1,
    ) -> KpiSet:
        """Compute every `KpiSet` field from one lazy scan over `event_paths`.

        `event_paths` may name one run's `events.parquet` or several (a sweep-wide
        glob); `pl.scan_parquet` combines them into a single lazy plan so no caller
        loop calls `read_parquet` per file (D-017). `orders` links to events via
        `lot_id`; an order's completion is the max `release_time` across every
        event row whose `lot_id` belongs to that order. `labor_pool_capacity` is a
        per-run fact supplied by the caller, not an event-table column — the
        committed schema has no operator/pool column (documented v1 gap; the
        single "default" pool is attributed every row's run interval per D-045,
        operator held for the same interval as the machine).
        """
        paths = self._normalize_event_paths(event_paths)
        capacity = self._resolve_labor_capacity(labor_pool_capacity)

        events = (
            pl.scan_parquet(paths)
            .with_columns((pl.col("actual_end") - pl.col("actual_start")).alias("busy_seconds"))
            .collect()
        )

        busy_seconds_by_location = self._busy_seconds_by_location(events)
        utilization_by_cell = {
            location_id: busy / horizon for location_id, busy in busy_seconds_by_location.items()
        }
        machine_hours_by_machine = {
            location_id: busy / 3600.0 for location_id, busy in busy_seconds_by_location.items()
        }
        run_hours = sum(machine_hours_by_machine.values())
        total_busy_seconds = sum(busy_seconds_by_location.values())
        setup_hours = self._setup_hours(events)

        completion_by_order, lateness_by_order, on_time_pct = self._order_kpis(events, orders)

        return KpiSet(
            completion_by_order=completion_by_order,
            lateness_by_order=lateness_by_order,
            on_time_pct=on_time_pct,
            utilization_by_cell=utilization_by_cell,
            utilization_by_machine=dict(utilization_by_cell),
            wait_seconds_by_location=self._wait_seconds_by_location(events, horizon),
            labor_pool_utilization={"default": total_busy_seconds / (horizon * capacity)},
            wip_by_location=self._wip_by_location(events),
            machine_hours_by_machine=machine_hours_by_machine,
            labor_hours_by_pool_skill={("default", "default"): run_hours},
            run_hours=run_hours,
            setup_hours=setup_hours,
            event_counts_by_location=self._event_counts_by_location(events),
        )

    @staticmethod
    def _normalize_event_paths(event_paths: str | Path | Sequence[str | Path]) -> list[str]:
        """Accept a single path or a sequence of paths; `scan_parquet` gets one list
        either way, so the single-path convenience form never needs its own branch
        downstream."""
        if isinstance(event_paths, (str, Path)):
            return [str(event_paths)]
        return [str(path) for path in event_paths]

    @staticmethod
    def _resolve_labor_capacity(labor_pool_capacity: dict[str, int] | int) -> int:
        """v1 has exactly one pool, "default" (tech.md: one undifferentiated labor
        pool). An int applies directly; a dict is looked up by that pool name,
        defaulting to 1 (a real, auditable default, not a skipped divide)."""
        if isinstance(labor_pool_capacity, int):
            return labor_pool_capacity
        return labor_pool_capacity.get("default", 1)

    @staticmethod
    def _setup_hours(events: pl.DataFrame) -> float:
        """Total setup/changeover hours: sum(setup_seconds) / 3600 across every
        firing. Real once changeover time is compiled (Tier 0); a model that
        declares no `changeover_seconds` charges 0 and this stays 0.0, reported
        separately from run_hours so setup is never conflated with processing."""
        if "setup_seconds" not in events.columns:
            return 0.0
        return float(events["setup_seconds"].sum()) / 3600.0

    @staticmethod
    def _busy_seconds_by_location(events: pl.DataFrame) -> dict[str, float]:
        """sum(actual_end - actual_start) per location_id — the shared basis for
        utilization and machine hours."""
        grouped = events.group_by("location_id").agg(
            pl.col("busy_seconds").sum().alias("busy_seconds")
        )
        return {
            str(location_id): float(busy)
            for location_id, busy in zip(
                grouped["location_id"], grouped["busy_seconds"], strict=True
            )
        }

    @staticmethod
    def _order_kpis(
        events: pl.DataFrame, orders: pl.DataFrame
    ) -> tuple[dict[str, float | None], dict[str, float | None], float]:
        """completion = max(release_time) over the order's lot(s); an order whose
        lot never appears in the event table reports `None` for both completion
        and lateness, and is excluded from `on_time_pct`'s denominator (unknown is
        not the same as late)."""
        lot_completion = events.group_by("lot_id").agg(
            pl.col("release_time").max().alias("completion")
        )
        joined = (
            orders.join(lot_completion, on="lot_id", how="left")
            .group_by("order_id")
            .agg(
                pl.col("due_date").first().alias("due_date"),
                pl.col("completion").max().alias("completion"),
            )
        )

        completion_by_order: dict[str, float | None] = {}
        lateness_by_order: dict[str, float | None] = {}
        for row in joined.iter_rows(named=True):
            completion = row["completion"]
            due_date = row["due_date"]
            completion_by_order[row["order_id"]] = completion
            lateness_by_order[row["order_id"]] = (
                completion - due_date if completion is not None else None
            )

        known_lateness = [v for v in lateness_by_order.values() if v is not None]
        on_time = sum(1 for lateness in known_lateness if lateness <= 0)
        on_time_pct = 100.0 * on_time / len(known_lateness) if known_lateness else 0.0
        return completion_by_order, lateness_by_order, on_time_pct

    @staticmethod
    def _wait_seconds_by_location(
        events: pl.DataFrame, horizon: float
    ) -> dict[str, dict[str, float]]:
        """How each work CENTER spent the run: BUSY (processing), BLOCKED (finished a
        job but holding it, unable to release downstream), or STARVED (idle, nothing
        to work on). A CENTER view, not a per-job queue view - so the bottleneck runs
        busy and shows ~0 starved, while the centers it feeds sit idle waiting on it
        and show high starved. `blocked = release_time - actual_end` (held after
        processing); `busy = actual_end - actual_start`; `starved` is the idle
        remainder of the horizon. `material_starved` is reserved for finite-material
        gating and is 0 in v1 (until reorder-point stocks land)."""
        grouped = events.group_by("location_id").agg(
            (pl.col("actual_end") - pl.col("actual_start")).sum().alias("busy"),
            (pl.col("release_time") - pl.col("actual_end")).sum().alias("blocked"),
        )
        totals: dict[str, dict[str, float]] = {}
        for row in grouped.iter_rows(named=True):
            busy = float(row["busy"])
            blocked = float(row["blocked"])
            totals[str(row["location_id"])] = {
                "starved": max(0.0, horizon - busy - blocked),
                "blocked": blocked,
                "material_starved": 0.0,
            }
        return totals

    @staticmethod
    def _wip_by_location(events: pl.DataFrame) -> pl.DataFrame:
        """WIP-over-time sweep line (structure.md "Derived, not stored"): entries
        at `queue_arrival_time` (+1), exits at `release_time` (-1), sorted by
        `(location_id, t, delta)` so -1 sorts before +1 on exact ties, then
        `cum_sum().over("location_id")`."""
        entries = events.select(
            pl.col("location_id"),
            pl.col("queue_arrival_time").alias("t"),
            pl.lit(1, dtype=pl.Int64).alias("delta"),
        )
        exits = events.select(
            pl.col("location_id"),
            pl.col("release_time").alias("t"),
            pl.lit(-1, dtype=pl.Int64).alias("delta"),
        )
        sweep = pl.concat([entries, exits]).sort(["location_id", "t", "delta"])
        return sweep.with_columns(
            pl.col("delta").cum_sum().over("location_id").alias("wip")
        ).select("location_id", "t", "wip")

    @staticmethod
    def _event_counts_by_location(events: pl.DataFrame) -> dict[str, int]:
        """Row count per location_id via `pl.len()` — counts every row including
        nulls, unlike `col(x).count()`, which silently skips them."""
        counts = events.group_by("location_id").agg(pl.len().alias("count"))
        return {
            str(location_id): int(count)
            for location_id, count in zip(counts["location_id"], counts["count"], strict=True)
        }
