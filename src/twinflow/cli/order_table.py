"""`twinflow run --orders`: one line per order, then the busiest work centers (TF-006).

A job-shop planner asks three things of an order book: when will each order be done, how
likely is it to be late, and which machine holds everything up. `order_lines` answers from
the per-run KPIs the run already computed, so it adds no new numbers. With a work calendar
(`plan_origin`) the times print as dates; without one, as hours from the start.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta

from twinflow.instrumentation.kpis import KpiSet
from twinflow.plan.loader import WorkOrder

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


def order_lines(
    orders: Sequence[WorkOrder], per_rep: Sequence[KpiSet], origin: datetime | None
) -> list[str]:
    """The order table and the busiest work centers, as plain lines."""
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
    busy: dict[str, float] = {}
    for kpis in per_rep:
        for cell, share in kpis.utilization_by_cell.items():
            busy[cell] = busy.get(cell, 0.0) + share / runs
    top = sorted(busy.items(), key=lambda item: (-item[1], item[0]))[:BUSIEST]
    if top:
        shares = " | ".join(f"{cell} {share:.0%}" for cell, share in top)
        lines.append(f"Busiest work centers: {shares} (busy share of the whole run)")
    return lines
