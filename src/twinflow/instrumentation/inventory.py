"""Inventory instrumentation — stock levels over time -> supply-chain KPIs.

Every change to a stock's level (a consume, a replenishment delivery, the initial seed)
and every replenishment order placed is recorded as one row in a per-run
`inventory.parquet`, written next to the run's `events.parquet`. From that log this
module derives the numbers a supply-chain planner acts on: how much inventory was held
on average (time-weighted), how long a stock sat empty (a real stockout), how many
orders were placed, and how much was replenished.

The level column always carries the level AFTER the event, so the log is a step
function of on-hand inventory over `[0, horizon]` that integrates directly. A model with
no stocks produces an empty log, and every KPI is an empty dict — never a crash.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import polars as pl

# One row per inventory event. `level` is the on-hand level AFTER the event; `delta` is
# the signed change (negative consume, positive replenish, the ordered qty on an order
# row); `kind` is "seed" | "consume" | "replenish" | "order".
INVENTORY_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "stock": pl.Utf8,
    "t": pl.Float64,
    "level": pl.Float64,
    "delta": pl.Float64,
    "kind": pl.Utf8,
}

InventoryRow = tuple[str, float, float, float, str]


@dataclass(frozen=True)
class InventoryKpis:
    """Per-stock supply-chain KPIs across one run.

    `average_level` is time-weighted over `[0, horizon]`; `ending_level` is the level
    at the horizon; `stockout_seconds` is the total time a stock sat at level 0;
    `orders_placed` counts replenishment orders; `total_ordered` sums the quantity
    actually delivered.
    """

    average_level: dict[str, float]
    ending_level: dict[str, float]
    stockout_seconds: dict[str, float]
    orders_placed: dict[str, int]
    total_ordered: dict[str, float]


class InventoryLog:
    """append(row) during the run; flush(run_dir) writes inventory.parquet atomically.

    Mirrors `EventLog`: buffers plain tuples, converts once at run end, writes zstd
    Parquet to a temp path then atomically renames. Always writes a file, even with no
    rows, so a consumer can rely on `inventory.parquet` existing next to
    `events.parquet`.
    """

    def __init__(self) -> None:
        self._rows: list[InventoryRow] = []

    def record(self, stock: str, t: float, level: float, delta: float, kind: str) -> None:
        """Buffer one inventory event. No I/O."""
        self._rows.append((stock, float(t), float(level), float(delta), kind))

    def flush(self, run_dir: Path) -> Path:
        """Write `inventory.parquet` atomically (temp file then `os.replace`)."""
        df = pl.DataFrame(self._rows, schema=INVENTORY_SCHEMA, orient="row")
        target = run_dir / "inventory.parquet"
        temp_path = run_dir / f".inventory.{uuid.uuid4().hex}.parquet.tmp"
        df.write_parquet(temp_path, compression="zstd")
        os.replace(temp_path, target)
        return target


def compute_inventory_kpis(inventory_path: str | Path, horizon: float) -> InventoryKpis:
    """Every `InventoryKpis` field from one inventory log. A missing or empty file
    (a model with no stocks) yields all-empty dicts rather than an error."""
    path = Path(inventory_path)
    empty = InventoryKpis({}, {}, {}, {}, {})
    if not path.is_file():
        return empty
    frame = pl.read_parquet(path)
    if frame.height == 0:
        return empty

    average_level: dict[str, float] = {}
    ending_level: dict[str, float] = {}
    stockout_seconds: dict[str, float] = {}
    orders_placed: dict[str, int] = {}
    total_ordered: dict[str, float] = {}

    for stock in sorted(frame["stock"].unique().to_list()):
        rows = frame.filter(pl.col("stock") == stock).sort("t")
        times = rows["t"].to_list()
        levels = rows["level"].to_list()
        kinds = rows["kind"].to_list()
        deltas = rows["delta"].to_list()

        area, empty_time = _integrate(times, levels, horizon)
        average_level[str(stock)] = area / horizon if horizon > 0 else 0.0
        ending_level[str(stock)] = float(levels[-1])
        stockout_seconds[str(stock)] = empty_time
        orders_placed[str(stock)] = sum(1 for kind in kinds if kind == "order")
        total_ordered[str(stock)] = sum(
            float(delta) for delta, kind in zip(deltas, kinds, strict=True) if kind == "replenish"
        )

    return InventoryKpis(
        average_level=average_level,
        ending_level=ending_level,
        stockout_seconds=stockout_seconds,
        orders_placed=orders_placed,
        total_ordered=total_ordered,
    )


def _integrate(times: list[float], levels: list[float], horizon: float) -> tuple[float, float]:
    """Integral of the level step function over `[0, horizon]`, and the total time the
    level was 0. `level[i]` holds from `times[i]` until the next event (or the horizon).
    Any tail of the log past the horizon is ignored."""
    area = 0.0
    empty_time = 0.0
    for index, start in enumerate(times):
        if start >= horizon:
            break
        end = times[index + 1] if index + 1 < len(times) else horizon
        end = min(end, horizon)
        span = end - start
        if span <= 0:
            continue
        area += levels[index] * span
        if levels[index] <= 0.0:
            empty_time += span
    return area, empty_time
