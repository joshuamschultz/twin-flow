"""`twinflow run --orders`: one line per order, then the busiest work centers (TF-006).

A job-shop planner asks three things of an order book: when will each order be done, how
likely is it to be late, and which machine holds everything up. `order_lines` answers from
the per-run KPIs the run already computed. With a work calendar (`plan_origin`) the times
print as dates; without one, as hours from the start. The busiest-center share is of
on-shift time (`on_shift_busy`), so a job paused overnight does not count its night.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from twinflow.instrumentation.kpis import KpiEngine, KpiSet, ShiftCalendar
from twinflow.model import CompiledModel
from twinflow.plan.loader import WorkOrder
from twinflow.primitives.calendar import WorkingCalendar

BUSIEST = 3


def _percentile(values: Sequence[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))]


def _when(seconds: float | None, origin: datetime | None, *, due: bool = False) -> str:
    if seconds is None or math.isinf(seconds):
        return "none" if due else "not done"
    if origin is None:
        return f"{seconds / 3600:.1f}"
    moment = origin + timedelta(seconds=seconds)
    if due and (moment.hour, moment.minute) == (0, 0):
        # Due at midnight means due by the end of the day before: print it that way.
        return (moment - timedelta(days=1)).strftime("%a %b %d 24:00")
    return moment.strftime("%a %b %d %H:%M")


def _due(order: WorkOrder) -> float | None:
    try:
        return float(order.due_date)
    except ValueError:
        return None


def on_shift_busy(
    compiled: CompiledModel, runs: Sequence[tuple[str | Path, float]]
) -> dict[str, float]:
    """Mean busy share of on-shift time per work center over `runs` (event log, horizon).

    Each center is measured against the calendar of the labor pool that staffs it; a
    pool with no calendar is open 24/7, which gives the whole-clock share."""
    calendar_of_pool: dict[str, ShiftCalendar] = {
        pool.name: WorkingCalendar(pool.calendar)
        for pool in compiled.labor_pools
        if pool.calendar is not None
    }
    pool_of_skill = {skill: pool.name for pool in compiled.labor_pools for skill in pool.skills}
    calendars: dict[str, ShiftCalendar] = {}
    for spec in compiled.locations:
        calendar = calendar_of_pool.get(pool_of_skill.get(spec.labor_skill, ""))
        if calendar is not None:
            calendars[spec.location_id] = calendar
    engine = KpiEngine()
    busy: dict[str, float] = {}
    for event_path, horizon in runs:
        shares = engine.on_shift_utilization_by_cell(event_path, horizon, calendars)
        for cell, share in shares.items():
            busy[cell] = busy.get(cell, 0.0) + share / len(runs)
    return busy


def order_lines(
    orders: Sequence[WorkOrder],
    per_rep: Sequence[KpiSet],
    origin: datetime | None,
    busy_by_cell: Mapping[str, float],
) -> list[str]:
    """The order table and the busiest work centers (`on_shift_busy`), as plain lines."""
    runs = len(per_rep)
    unit = "" if origin is not None else " (hours from the start)"
    lines = [f"Orders over {runs} {'run' if runs == 1 else 'runs'}{unit}:"]
    rows = [("Order", "Part", "Qty", "Due", "Done (typical)", "Done (bad case)", "Late in")]
    for order in orders:
        if order.qty <= 0:
            continue
        key = order.work_order_id
        done = [k.completion_by_order.get(key) for k in per_rep]
        finished = [math.inf if d is None else d for d in done]  # not done sorts last
        lateness = [k.lateness_by_order.get(key) for k in per_rep]
        late = sum(1 for value in lateness if value is None or value > 0)
        rows.append(
            (
                key,
                order.part,
                str(order.qty),
                _when(_due(order), origin, due=True),
                _when(_percentile(finished, 0.5), origin) if finished else "not done",
                _when(_percentile(finished, 0.9), origin) if finished else "not done",
                f"{late} of {runs}",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines += [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    top = sorted(busy_by_cell.items(), key=lambda item: (-item[1], item[0]))[:BUSIEST]
    if top:
        shares = " | ".join(f"{cell} {share:.0%}" for cell, share in top)
        lines.append(f"Busiest work centers: {shares} (busy share of on-shift time)")
    return lines
