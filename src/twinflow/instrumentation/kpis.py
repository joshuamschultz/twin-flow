"""COMP-022 WaitClassifier + COMP-023 KpiEngine.

WaitClassifier splits total wait into starved/blocked/material-starved by the binding-
constraint rule (the input that became available last owns the whole wait).
KpiEngine computes every KPI from the event table via lazy Polars over scan_parquet.
No KPI is computed anywhere outside this component.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
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
class OrderOutcome:
    """Quantity-conserving fulfillment evidence for one planned order."""

    order_id: str
    required_qty: float
    accepted_qty: float
    scrapped_qty: float
    shipped_qty: float
    remaining_qty: float
    status: str
    completion_time: float | None
    due_state: str


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
    order_outcomes: dict[str, OrderOutcome] = field(default_factory=dict)
    on_time_denominator: int = 0
    completed_order_count: int = 0
    censored_order_count: int = 0
    busy_hours_by_location: dict[str, float] = field(default_factory=dict)
    """Busy hours (sum of `actual_end - actual_start`) per LOCATION id, always
    keyed by location even when resource attribution keys `machine_hours_by_machine`
    by physical machine instance. The per-stage cycle-time view and the flow
    diagram read this so a shared-machine floor still reports per-step timings."""


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

        resource_events = self._resource_events(event_paths)

        busy_seconds_by_location = self._busy_seconds_by_location(events)
        if resource_events is None:
            utilization_by_cell = {
                location_id: busy / horizon
                for location_id, busy in busy_seconds_by_location.items()
            }
            machine_hours_by_machine = {
                location_id: busy / 3600.0 for location_id, busy in busy_seconds_by_location.items()
            }
            labor_hours = {("default", "default"): sum(machine_hours_by_machine.values())}
        else:
            utilization_by_cell, machine_hours_by_machine, labor_hours = self._resource_attribution(
                resource_events, horizon
            )
        run_hours = sum(busy_seconds_by_location.values()) / 3600.0
        total_busy_seconds = sum(busy_seconds_by_location.values())
        setup_hours = self._setup_hours(events)

        outcomes = self._order_outcomes(events, orders, horizon)
        completion_by_order = {key: value.completion_time for key, value in outcomes.items()}
        lateness_by_order: dict[str, float | None] = {}
        for row in orders.iter_rows(named=True):
            order_id = str(row["order_id"])
            completion_time = completion_by_order[order_id]
            lateness_by_order[order_id] = (
                completion_time - float(row["due_date"]) if completion_time is not None else None
            )
        denominator = sum(outcome.due_state != "not_yet_due" for outcome in outcomes.values())
        on_time = sum(outcome.due_state == "on_time" for outcome in outcomes.values())
        on_time_pct = 100.0 * on_time / denominator if denominator else 0.0

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
            labor_hours_by_pool_skill=labor_hours,
            run_hours=run_hours,
            setup_hours=setup_hours,
            event_counts_by_location=self._event_counts_by_location(events),
            order_outcomes=outcomes,
            on_time_denominator=denominator,
            completed_order_count=sum(o.status == "completed" for o in outcomes.values()),
            censored_order_count=sum(o.status != "completed" for o in outcomes.values()),
            busy_hours_by_location={
                location_id: seconds / 3600.0
                for location_id, seconds in busy_seconds_by_location.items()
            },
        )

    @staticmethod
    def _resource_events(
        event_paths: str | Path | Sequence[str | Path],
    ) -> pl.DataFrame | None:
        """Load the colocated resource ledger for a single run when present."""
        if not isinstance(event_paths, (str, Path)):
            return None
        path = Path(event_paths).parent / "resource_usage.parquet"
        return pl.read_parquet(path) if path.exists() else None

    @staticmethod
    def _resource_attribution(
        resources: pl.DataFrame, horizon: float
    ) -> tuple[dict[str, float], dict[str, float], dict[tuple[str, str], float]]:
        """Compute utilization and occupied hours from named resource intervals."""
        with_duration = resources.with_columns(
            (pl.col("run_end") - pl.col("setup_start")).alias("occupied_seconds")
        )
        machines = with_duration.group_by("location_id", "machine_id").agg(
            pl.col("occupied_seconds").sum()
        )
        machine_hours = {
            str(row["machine_id"]): float(row["occupied_seconds"]) / 3600.0
            for row in machines.iter_rows(named=True)
        }
        cells = machines.group_by("location_id").agg(
            pl.col("occupied_seconds").sum().alias("busy"),
            pl.col("machine_id").n_unique().alias("machines"),
        )
        utilization = {
            str(row["location_id"]): float(row["busy"]) / (horizon * int(row["machines"]))
            for row in cells.iter_rows(named=True)
        }
        labor = with_duration.group_by("labor_pool", "labor_skill").agg(
            pl.col("labor_seconds").sum()
        )
        labor_hours = {
            (str(row["labor_pool"]), str(row["labor_skill"])): float(row["labor_seconds"]) / 3600.0
            for row in labor.iter_rows(named=True)
        }
        return utilization, machine_hours, labor_hours

    @staticmethod
    def _order_outcomes(
        events: pl.DataFrame, orders: pl.DataFrame, horizon: float
    ) -> dict[str, OrderOutcome]:
        """Account accepted terminal output against every row of declared demand."""
        outcomes: dict[str, OrderOutcome] = {}
        modern = {"required_qty", "accepted_qty", "scrapped_qty", "completion_time"}.issubset(
            orders.columns
        )
        if not modern:
            legacy_completion, _lateness, _pct = KpiEngine._order_kpis(events, orders)
            for row in (
                orders.group_by("order_id")
                .agg(pl.col("due_date").first().alias("due_date"))
                .iter_rows(named=True)
            ):
                order_id = str(row["order_id"])
                done = legacy_completion.get(order_id)
                due = float(row["due_date"])
                outcomes[order_id] = OrderOutcome(
                    order_id,
                    1.0,
                    1.0 if done is not None else 0.0,
                    0.0,
                    1.0 if done is not None else 0.0,
                    0.0 if done is not None else 1.0,
                    "completed" if done is not None else "incomplete_at_horizon",
                    done,
                    "on_time"
                    if done is not None and done <= due
                    else ("not_yet_due" if due > horizon else "late"),
                )
            return outcomes

        for row in orders.iter_rows(named=True):
            order_id = str(row["order_id"])
            required = float(row["required_qty"])
            due = float(row["due_date"])
            accepted = float(row["accepted_qty"])
            scrapped = float(row["scrapped_qty"])
            raw_completion = row["completion_time"]
            completion_time = float(raw_completion) if raw_completion is not None else None
            accepted = min(accepted, required)
            remaining = max(0.0, required - accepted)
            status = "completed" if completion_time is not None else "incomplete_at_horizon"
            due_state = (
                "on_time"
                if completion_time is not None and completion_time <= due
                else "not_yet_due"
                if completion_time is None and due > horizon
                else "late"
            )
            outcomes[order_id] = OrderOutcome(
                order_id,
                required,
                accepted,
                scrapped,
                accepted,
                remaining,
                status,
                completion_time,
                due_state,
            )
        return outcomes

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
