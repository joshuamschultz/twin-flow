"""Unit tests for inventory instrumentation (InventoryLog + compute_inventory_kpis)."""

from __future__ import annotations

from pathlib import Path

from twinflow.instrumentation.inventory import InventoryLog, compute_inventory_kpis


def _write_log(tmp_path: Path) -> Path:
    log = InventoryLog()
    log.record("resin", 0.0, 100.0, 100.0, "seed")
    log.record("resin", 20.0, 60.0, -40.0, "consume")
    log.record("resin", 50.0, 0.0, -60.0, "consume")
    log.record("resin", 50.0, 0.0, 100.0, "order")
    log.record("resin", 80.0, 100.0, 100.0, "replenish")
    return log.flush(tmp_path)


def test_time_weighted_average_level(tmp_path: Path) -> None:
    path = _write_log(tmp_path)
    kpis = compute_inventory_kpis(path, horizon=100.0)
    # 100*20 + 60*30 + 0*30 + 100*20 = 5800; /100 = 58
    assert kpis.average_level["resin"] == 58.0


def test_stockout_seconds_counts_empty_time(tmp_path: Path) -> None:
    path = _write_log(tmp_path)
    kpis = compute_inventory_kpis(path, horizon=100.0)
    assert kpis.stockout_seconds["resin"] == 30.0  # empty from t=50 to t=80


def test_orders_and_replenishment(tmp_path: Path) -> None:
    path = _write_log(tmp_path)
    kpis = compute_inventory_kpis(path, horizon=100.0)
    assert kpis.orders_placed["resin"] == 1
    assert kpis.total_ordered["resin"] == 100.0
    assert kpis.ending_level["resin"] == 100.0


def test_missing_file_returns_empty() -> None:
    kpis = compute_inventory_kpis("does-not-exist.parquet", horizon=100.0)
    assert kpis.average_level == {}
    assert kpis.stockout_seconds == {}


def test_empty_log_returns_empty(tmp_path: Path) -> None:
    path = InventoryLog().flush(tmp_path)
    kpis = compute_inventory_kpis(path, horizon=100.0)
    assert kpis.average_level == {}
    assert kpis.orders_placed == {}
