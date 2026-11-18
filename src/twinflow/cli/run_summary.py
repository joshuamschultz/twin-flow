"""`twinflow scenario run --summary`: the result in a few plain lines (TF-006 showcases).

The full result is a large JSON file. A planner wants the answer: the money per run for a
make-to-stock plant, the on-time share for a shop, and when each customer order can ship
for a supply network. `summary_lines(result, result_file)` turns the result into those
lines. It reads only the result, so it never changes a number.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, tzinfo
from typing import Any


def summary_lines(result: Mapping[str, Any], result_file: str) -> list[str]:
    """Plain lines for a scenario result; the last line names the full result file."""
    domain = str(result.get("domain", "manufacturing"))
    reps = int(result.get("replications", 1))
    seed = result.get("seed", 0)
    head = f"Ran {reps} {'run' if reps == 1 else 'runs'} (seed {seed})."
    if domain == "make_to_stock":
        body = _make_to_stock(result)
    elif domain == "supply_chain":
        body = _supply_chain(result, reps)
    elif domain == "manufacturing":
        body = _manufacturing(result)
    else:
        body = [f"{key}: {_number(value)}" for key, value in result.get("metrics", {}).items()]
    return [head, *body, f"Full result: {result_file}"]


def _number(value: object) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}"
    if isinstance(value, (int, float)):
        return f"{value:,.0f}"
    return str(value)


def _dollars(value: float) -> str:
    return f"-${-value:,.0f}" if value < 0 else f"${value:,.0f}"


def _make_to_stock(result: Mapping[str, Any]) -> list[str]:
    metrics = result.get("metrics", {})
    parts = (
        ("sales", "revenue"),
        ("making", "production"),
        ("setups", "setup"),
        ("holding", "holding"),
        ("lost sales", "lost_sales"),
    )
    money = " | ".join(f"{label} {_dollars(float(metrics.get(key, 0.0)))}" for label, key in parts)
    return [
        f"Profit per run:  {_dollars(float(metrics.get('mean_profit', 0.0)))}",
        f"Fill rate:       {float(metrics.get('fill_rate', 0.0)):.1%} of demand shipped",
        f"Changeovers:     {float(metrics.get('changeovers_per_episode', 0.0)):.1f} per run, "
        f"{float(metrics.get('cross_family_per_episode', 0.0)):.1f} of them to a new family",
        f"Money per run:   {money}",
    ]


def _manufacturing(result: Mapping[str, Any]) -> list[str]:
    metrics = result.get("metrics", {})
    band = result.get("intervals", {}).get("on_time_pct", {})
    lines = [f"On time:   {float(metrics.get('on_time_pct', 0.0)):.0f}% of orders"]
    if band.get("n", 0) > 1:
        lines[0] += f" (range {band['lo']:.0f}% to {band['hi']:.0f}%)"
    lines.append(f"Machine hours per run: {float(metrics.get('run_hours', 0.0)):,.1f}")
    capture = result.get("state_capture")
    if isinstance(capture, Mapping):
        lines.append(
            f"States saved every {_number(capture.get('every_hours'))} h. "
            "Read them with twinflow states and twinflow markov."
        )
    return lines


def _when(text: str, zone: tzinfo | None) -> datetime:
    moment = datetime.fromisoformat(text)
    if zone is None:
        return moment
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)


def _day(moment: datetime) -> str:
    return moment.strftime("%a %b %d")


def _percentile(values: Sequence[datetime], share: float) -> datetime:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))]


def _supply_chain(result: Mapping[str, Any], reps: int) -> list[str]:
    snapshot = result.get("snapshot", {})
    as_of = snapshot.get("as_of")
    zone = datetime.fromisoformat(as_of).tzinfo if isinstance(as_of, str) else None
    orders = {str(row.get("id")): row for row in snapshot.get("orders", [])}
    samples = [rep for rep in result.get("per_replication", []) if isinstance(rep, Mapping)]
    ready: dict[str, list[datetime]] = {order_id: [] for order_id in orders}
    on_time: dict[str, int] = dict.fromkeys(orders, 0)
    blocked: dict[str, int] = dict.fromkeys(orders, 0)
    short: dict[tuple[str, str], list[float]] = {}
    for sample in samples:
        for forecast in sample.get("order_forecasts", []):
            order_id = str(forecast.get("order_id"))
            ready.setdefault(order_id, [])
            if forecast.get("delivery_at"):
                ready[order_id].append(_when(str(forecast["delivery_at"]), zone))
            on_time[order_id] = on_time.get(order_id, 0) + bool(forecast.get("on_time"))
            blocked[order_id] = blocked.get(order_id, 0) + (forecast.get("status") == "blocked")
        for shortage in sample.get("shortages", []):
            key = (str(shortage.get("order_id")), str(shortage.get("item_id")))
            short.setdefault(key, []).append(float(shortage.get("missing_quantity", 0.0)))
    rows = [("Order", "Item", "Qty", "Due", "Ready (typical)", "Ready (bad case)", "On time")]
    for order_id in sorted(ready, key=lambda o: (str(orders.get(o, {}).get("due_at", "")), o)):
        order = orders.get(order_id, {})
        due = order.get("due_at")
        dates = ready[order_id]
        typical = _day(_percentile(dates, 0.5)) if dates else "never"
        bad = _day(_percentile(dates, 0.9)) if dates else "never"
        if blocked.get(order_id) and dates:
            bad = f"short in {blocked[order_id]} runs"
        hits = on_time.get(order_id, 0)
        rows.append(
            (
                order_id,
                str(order.get("item_id", "")),
                _number(order.get("quantity", "")),
                _day(_when(str(due), zone)) if due else "",
                typical,
                bad,
                f"{hits / max(reps, 1):.0%} ({hits} of {reps})",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    if short:
        lines.append("Short of parts:")
        for (order_id, item_id), missing in sorted(short.items()):
            lines.append(
                f"  {order_id} needs {_number(max(missing))} more {item_id} "
                f"(in {len(missing)} of {reps} runs)"
            )
    return lines
