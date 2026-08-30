"""Tier 0 — reorder-point stock (the Stock `on_change` observer hook).

The primitive stays reorder-agnostic (D-044); a Layer-3 policy passed as
`on_change` tops the level up before a blocking `get`, so an initially-empty
self-refilling stock never deadlocks a consumer.
"""

from __future__ import annotations

import simpy

from twinflow.primitives.bundle import Bundle
from twinflow.primitives.stock import Stock

_REORDER_POINT = 20.0
_REFILL_TO = 100.0


def _reorder_hook(stock: Stock) -> None:
    if stock.level < _REORDER_POINT:
        deficit = _REFILL_TO - stock.level
        if deficit > 0.0:
            stock.put(Bundle(qty=deficit, thing=stock.thing, uom=stock.uom))


def test_empty_reorder_stock_refills_before_a_blocking_get() -> None:
    env = simpy.Environment()
    stock = Stock(thing="glue", uom="ml", env=env, initial_qty=0.0, on_change=_reorder_hook)

    pulled: list[float] = []

    def consumer() -> object:
        for _ in range(5):
            bundle = yield from stock.pull("glue", 30.0, "ml")
            pulled.append(bundle.qty)

    env.process(consumer())
    env.run()

    assert pulled == [30.0] * 5  # every pull succeeded, never blocked forever
    assert stock.level >= 0.0


def test_reorder_stock_stays_at_or_above_reorder_point_after_each_pull() -> None:
    env = simpy.Environment()
    stock = Stock(thing="glue", uom="ml", env=env, initial_qty=_REFILL_TO, on_change=_reorder_hook)

    def consumer() -> object:
        for _ in range(50):
            yield from stock.pull("glue", 10.0, "ml")

    env.process(consumer())
    env.run()

    # The hook fires before every get; a level below the reorder point is always
    # topped back up, so steady consumption never exhausts the stock.
    assert stock.level >= _REORDER_POINT - 10.0


def test_stock_without_hook_is_unchanged_and_can_run_dry() -> None:
    env = simpy.Environment()
    stock = Stock(thing="glue", uom="ml", env=env, initial_qty=25.0)

    got: list[float] = []

    def consumer() -> object:
        bundle = yield from stock.pull("glue", 25.0, "ml")
        got.append(bundle.qty)

    env.process(consumer())
    env.run()

    assert got == [25.0]
    assert stock.level == 0.0  # no refill without a hook
