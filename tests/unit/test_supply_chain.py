"""Unit tests for the supply-chain reorder mechanism: supplier lead time, the
single-order-in-transit (s, S) guard, and multi-echelon replenishment.

The driver's reorder hook and delivery process are exercised directly against a small
SimPy environment, so the timing (an order arriving exactly `lead_time` later) and the
guard (never two orders in transit at once) are asserted without a full model run.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import simpy

from twinflow.instrumentation.inventory import InventoryLog, compute_inventory_kpis
from twinflow.model import CompiledModel, StockConfig
from twinflow.plan.driver import RunDriver
from twinflow.primitives.part import PartTypeRegistry
from twinflow.primitives.stock import Stock


def _driver() -> RunDriver:
    """A driver with no floor — only its stock/reorder helpers are used here."""
    return RunDriver(
        CompiledModel(
            locations=[],
            routing={},
            bom={},
            labor_pools=[],
            registry=PartTypeRegistry({}),
            stocks=[],
        )
    )


def _wire(
    env: simpy.Environment,
    inventory_log: InventoryLog,
    config: StockConfig,
    stocks: dict[str, Stock] | None = None,
) -> Stock:
    driver = _driver()
    stock = Stock(
        thing=config.name,
        uom=config.uom,
        env=env,
        initial_qty=config.initial,
        on_level_change=driver._inventory_recorder(inventory_log),
    )
    all_stocks = {config.name: stock, **(stocks or {})}
    inventory_log.record(config.name, env.now, config.initial, config.initial, "seed")
    stock.set_reorder_hook(driver._make_reorder_hook(config, all_stocks, inventory_log, env))
    return stock


def test_order_arrives_after_lead_time_and_only_one_in_transit(tmp_path: Path) -> None:
    env = simpy.Environment()
    log = InventoryLog()
    config = StockConfig("resin", "kg", initial=10, reorder_point=8, refill_to=20, lead_time=100)
    stock = _wire(env, log, config)

    def consumer() -> object:
        yield from stock.pull("resin", 3, "kg")  # level 10 -> 7 (no order: pre-pull saw 10)
        yield from stock.pull("resin", 1, "kg")  # pre-pull sees 7 < 8 -> ORDER, level -> 6
        yield from stock.pull("resin", 1, "kg")  # pre-pull sees 6 < 8 but in transit: no 2nd order

    env.process(consumer())
    env.run()

    path = log.flush(tmp_path)
    frame = pl.read_parquet(path)
    orders = frame.filter(pl.col("kind") == "order")
    replenish = frame.filter(pl.col("kind") == "replenish")

    assert orders.height == 1  # the (s, S) guard: exactly one order despite two sub-point pulls
    assert replenish["t"].to_list() == [100.0]  # delivered exactly lead_time later
    kpis = compute_inventory_kpis(path, horizon=100.0)
    assert kpis.orders_placed["resin"] == 1
    assert stock.level == 18.0  # 5 on hand + 13 ordered (order-up-to 20 from level 7)


def test_stockout_does_not_deadlock(tmp_path: Path) -> None:
    """A thin buffer with a long lead runs dry; the blocked consumer resumes when the
    in-transit order arrives — a real stockout, never a deadlock."""
    env = simpy.Environment()
    log = InventoryLog()
    config = StockConfig("resin", "kg", initial=5, reorder_point=4, refill_to=20, lead_time=50)
    stock = _wire(env, log, config)
    finished: list[float] = []

    def consumer() -> object:
        yield from stock.pull("resin", 5, "kg")  # empties it; the sub-point order is placed
        yield from stock.pull("resin", 5, "kg")  # blocks until the order arrives at t=50
        finished.append(env.now)

    env.process(consumer())
    env.run()

    assert finished == [50.0]  # unblocked exactly when the replenishment landed
    kpis = compute_inventory_kpis(log.flush(tmp_path), horizon=60.0)
    assert kpis.stockout_seconds["resin"] > 0.0


def test_multi_echelon_upstream_reorders_when_drawn_down(tmp_path: Path) -> None:
    env = simpy.Environment()
    log = InventoryLog()
    upstream_cfg = StockConfig(
        "bulk", "kg", initial=100, reorder_point=60, refill_to=200, lead_time=10
    )
    upstream = _wire(env, log, upstream_cfg)
    working_cfg = StockConfig(
        "resin", "kg", initial=50, reorder_point=40, refill_to=100, lead_time=5, supplier="bulk"
    )
    working = _wire(env, log, working_cfg, stocks={"bulk": upstream})

    def consumer() -> object:
        yield from working.pull("resin", 15, "kg")  # 50 -> 35, orders 65 from bulk
        yield env.timeout(1)
        yield from working.pull("resin", 5, "kg")

    env.process(consumer())
    env.run()

    kpis = compute_inventory_kpis(log.flush(tmp_path), horizon=100.0)
    # working ordered up-to-100 from bulk; that draw dropped bulk below 60, so bulk
    # ordered externally too — the echelon propagated.
    assert kpis.orders_placed["resin"] >= 1
    assert kpis.orders_placed["bulk"] >= 1
    assert kpis.total_ordered["bulk"] > 0.0
