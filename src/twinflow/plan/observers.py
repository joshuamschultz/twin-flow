"""COMP-152 seam: run observers and the read-only run view (TF-006, D-136).

`RunDriver.run(..., observers=())` hands each observer a `RunView` before the run advances,
tells it how far the clock is about to jump before every SimPy step, reports every dispatch
decision, and calls `finalize` once after the event logs are written.

This module uses plain types only. Lower layers (`plan`, `model`, `primitives`, ...) may not
import `twinflow.state` or `twinflow.engines` (D-131), so the concrete state observer that
builds `Snapshot`/`Move` records lives in `twinflow.engines.des` and is passed in here.

Observers must not change the floor: they read through `RunView` and add no SimPy events,
so a run with an observer writes the same event logs as a run without one.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import simpy

from twinflow.primitives.labor import LaborPool
from twinflow.primitives.location import Location
from twinflow.primitives.stock import Stock


@dataclass(frozen=True)
class OrderView:
    """One plan order as the observer sees it. `due_date` is +inf when the plan has none."""

    order_id: str
    part: str
    qty: float
    due_date: float
    delivered: float
    completed_at: float | None


class OrderSource(Protocol):
    """What the view reads from the driver's work tracker."""

    @property
    def active(self) -> int: ...

    def delivered(self, order_id: str) -> float: ...

    def completion_time(self, order_id: str) -> float | None: ...


@dataclass(frozen=True)
class OrderPlan:
    """The static facts of one released order (from the plan)."""

    order_id: str
    part: str
    qty: float
    due_date: float


class RunView:
    """Read-only accessors over one run's live floor. Every call returns fresh plain data."""

    def __init__(
        self,
        env: simpy.Environment,
        stocks: Mapping[str, Stock],
        locations: Mapping[str, Location],
        labor_pools: Mapping[str, LaborPool],
        orders: tuple[OrderPlan, ...] = (),
        tracker: OrderSource | None = None,
    ) -> None:
        self._env = env
        self._stocks = stocks
        self._locations = locations
        self._labor_pools = labor_pools
        self._orders = orders
        self._tracker = tracker

    @property
    def now(self) -> float:
        """The simulation clock (seconds)."""
        return float(self._env.now)

    def peek(self) -> float:
        """The time of the next scheduled SimPy event (+inf when none)."""
        return float(self._env.peek())

    def stock_levels(self) -> dict[str, float]:
        return {name: float(stock.level) for name, stock in self._stocks.items()}

    def queue_lengths(self) -> dict[str, int]:
        """Jobs waiting (not yet started) at each location."""
        return {name: len(location._queue) for name, location in self._locations.items()}

    def in_flight(self) -> dict[str, int]:
        """Jobs being worked at each location."""
        return {name: location._in_flight for name, location in self._locations.items()}

    def setups(self) -> dict[str, str | None]:
        """The setup group of every machine, busy or idle, by machine id (D-137).

        Locations sharing a machine report the same machine once.
        """
        out: dict[str, str | None] = {}
        for location in self._locations.values():
            out.update(location.current_setups())
        return dict(sorted(out.items()))

    def labor_available(self) -> dict[str, int]:
        return {name: int(pool.available) for name, pool in self._labor_pools.items()}

    def labor_headcount(self) -> dict[str, int]:
        return {name: int(pool.headcount) for name, pool in self._labor_pools.items()}

    def open_orders(self) -> int:
        """Released orders not yet complete (0 when the run has no orders)."""
        return 0 if self._tracker is None else int(self._tracker.active)

    def orders(self) -> tuple[OrderView, ...]:
        """Every positive-quantity plan order with its delivery so far."""
        tracker = self._tracker
        return tuple(
            OrderView(
                order.order_id,
                order.part,
                order.qty,
                order.due_date,
                0.0 if tracker is None else tracker.delivered(order.order_id),
                None if tracker is None else tracker.completion_time(order.order_id),
            )
            for order in self._orders
        )


@runtime_checkable
class RunObserver(Protocol):
    """Watches one run. Calls arrive in this order: `start` once, then `on_advance` and
    `on_decision` any number of times, then `finalize` once."""

    def start(self, view: RunView) -> None:
        """Called after orders are released and before the clock moves."""

    def on_advance(self, next_time: float) -> None:
        """Called before each SimPy step: the floor stays as it is now until `next_time`."""

    def on_decision(self, location_id: str, time: float, rule: str) -> None:
        """Called at each dispatch decision, after the rule is chosen."""

    def finalize(self, run_dir: Path, termination_reason: str) -> None:
        """Called once after the event logs are written; the view still shows the end."""


def order_plans(rows: list[tuple[str, str, float, object]]) -> tuple[OrderPlan, ...]:
    """`OrderPlan`s from `(order_id, part, qty, due_date)` rows; a non-numeric due is +inf."""
    plans: list[OrderPlan] = []
    for order_id, part, qty, due in rows:
        try:
            due_value = float(due)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            due_value = math.inf
        plans.append(OrderPlan(order_id, part, float(qty), due_value))
    return tuple(plans)
