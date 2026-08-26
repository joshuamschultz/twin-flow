"""COMP-018 PlanLoader — client production plans from .xlsx/.csv into WorkOrders.

Column contract only; no transformation logic. No formula/macro/embedded object is
ever evaluated. Raises on unknown part or missing required column, naming the row.
"""

from __future__ import annotations


class WorkOrder:
    """{work_order_id, part, qty, start_date, due_date, initial_wip?}."""


def load_plan(path: str) -> list[WorkOrder]:
    raise NotImplementedError("T-031")
