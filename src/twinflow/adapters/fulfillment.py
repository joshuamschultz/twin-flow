"""Orders & deliveries — fulfillment KPIs derived from a run's order outcomes.

An order is delivered when its finished units complete within the horizon; it is on
time when it completes by its due date, and backordered when it does not complete at
all. These read straight off the `KpiSet` the twin already produces (completion and
signed lateness per order), so no engine change is needed — deliveries are a reporting
view over orders the plan already carries.
"""

from __future__ import annotations

from dataclasses import dataclass

from twinflow.instrumentation.kpis import KpiSet


@dataclass(frozen=True)
class FulfillmentKpis:
    """How the plan's orders were fulfilled."""

    orders: int
    delivered: int
    backordered: int
    fill_rate: float
    on_time_deliveries: int
    on_time_delivery_pct: float
    avg_delivery_lateness: float


def compute_fulfillment(kpis: KpiSet) -> FulfillmentKpis:
    """Summarise order fulfillment from a run's `KpiSet`.

    `fill_rate` is the fraction of orders that completed within the horizon;
    `on_time_delivery_pct` is the fraction of *delivered* orders that met their due
    date; `avg_delivery_lateness` is the mean signed lateness over delivered orders
    (negative = early). An order that never completed is a backorder and is excluded
    from the lateness average (unknown is not the same as late).
    """
    completion = kpis.completion_by_order
    orders = len(completion)
    delivered_ids = [oid for oid, done in completion.items() if done is not None]
    delivered = len(delivered_ids)
    backordered = orders - delivered

    delivered_lateness = [
        kpis.lateness_by_order[oid]
        for oid in delivered_ids
        if kpis.lateness_by_order.get(oid) is not None
    ]
    on_time = sum(1 for lateness in delivered_lateness if lateness is not None and lateness <= 0)
    known = [v for v in delivered_lateness if v is not None]

    return FulfillmentKpis(
        orders=orders,
        delivered=delivered,
        backordered=backordered,
        fill_rate=(delivered / orders) if orders else 0.0,
        on_time_deliveries=on_time,
        on_time_delivery_pct=(100.0 * on_time / len(known)) if known else 0.0,
        avg_delivery_lateness=(sum(known) / len(known)) if known else 0.0,
    )
