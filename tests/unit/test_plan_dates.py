"""A plan may give start and due as calendar dates (TF-006 business showcases).

An ERP exports an order book with dates, not seconds. When the shop has a work calendar,
its `origin` is time zero, and `load_plan(..., origin=...)` turns each date into seconds
from that moment. A date with no time means the start of that day for `start_date` and
the end of that day for `due_date`. Without an origin the cells are read as they are.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from twinflow.model import load_model, plan_origin
from twinflow.plan.loader import load_plan
from twinflow.primitives.part import PartTypeRegistry

ORIGIN = datetime.fromisoformat("2026-11-16T07:00:00-06:00")  # a Monday, first shift


def _registry() -> PartTypeRegistry:
    return PartTypeRegistry({"shaft": {"attributes": {}, "uom": "piece"}})


def _plan(tmp_path: Path, *rows: str) -> str:
    path = tmp_path / "orders.csv"
    path.write_text("work_order_id,part,qty,start_date,due_date\n" + "\n".join(rows) + "\n")
    return str(path)


def test_dates_become_seconds_from_the_calendar_start(tmp_path: Path) -> None:
    path = _plan(tmp_path, "WO-1,shaft,5,2026-11-16 07:00,2026-11-17 15:30")

    (order,) = load_plan(path, _registry(), origin=ORIGIN)

    assert float(order.start_date) == 0.0
    assert float(order.due_date) == (24 + 8.5) * 3600


def test_a_due_date_with_no_time_means_the_end_of_that_day(tmp_path: Path) -> None:
    path = _plan(tmp_path, "WO-1,shaft,5,2026-11-17,2026-11-20")

    (order,) = load_plan(path, _registry(), origin=ORIGIN)

    assert float(order.start_date) == 17 * 3600  # Tue 00:00 is 17 h after Mon 07:00
    assert float(order.due_date) == (4 * 24 + 17) * 3600  # Sat 00:00


def test_a_start_before_the_calendar_start_releases_at_once(tmp_path: Path) -> None:
    path = _plan(tmp_path, "WO-1,shaft,5,2026-11-10,2026-11-20")

    (order,) = load_plan(path, _registry(), origin=ORIGIN)

    assert float(order.start_date) == 0.0


def test_seconds_still_work_beside_an_origin(tmp_path: Path) -> None:
    path = _plan(tmp_path, "WO-1,shaft,5,0,3600")

    (order,) = load_plan(path, _registry(), origin=ORIGIN)

    assert (order.start_date, order.due_date) == ("0", "3600")


def test_a_bad_date_names_the_row_and_the_column(tmp_path: Path) -> None:
    path = _plan(tmp_path, "WO-1,shaft,5,2026-11-16,next tuesday")

    with pytest.raises(ValueError, match=r"row 2: due_date 'next tuesday' is not a date"):
        load_plan(path, _registry(), origin=ORIGIN)


def test_without_an_origin_dates_are_read_as_they_are(tmp_path: Path) -> None:
    path = _plan(tmp_path, "WO-1,shaft,5,2026-11-16,2026-11-20")

    (order,) = load_plan(path, _registry())

    assert (order.start_date, order.due_date) == ("2026-11-16", "2026-11-20")


def test_plan_origin_is_the_work_calendar_start() -> None:
    root = Path(__file__).resolve().parents[2]
    with_calendar = load_model(str(root / "examples" / "hmlv-calendar" / "model.yaml"))
    without = load_model(str(root / "examples" / "cnc-shop" / "model.yaml"))

    assert plan_origin(with_calendar) == datetime.fromisoformat("2026-03-02T07:00:00-06:00")
    assert plan_origin(without) is None
