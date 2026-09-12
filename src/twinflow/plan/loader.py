"""COMP-018 PlanLoader — client production plans from .xlsx/.csv into WorkOrders.

Column contract only; no transformation logic (D-054). Reads ONE table from ONE
file: the first/only worksheet of an `.xlsx`, or a `.csv` with identical columns.
No formula/macro/embedded object is ever evaluated — a formula cell is rejected
outright rather than passed through (D-007). Raises `ValueError` on unknown part,
missing required column, non-numeric quantity, a partially populated initial-WIP
row, or a formula cell, naming the 1-indexed row (header = row 1).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import openpyxl  # type: ignore[import-untyped]  # no stub package; see D-052 lib choice

from twinflow.primitives.part import PartTypeRegistry

REQUIRED_COLUMNS = ("work_order_id", "part", "qty", "start_date", "due_date")
_WIP_COLUMNS = ("initial_wip_location", "initial_wip_qty", "initial_wip_remaining_time")


@dataclass
class WorkOrder:
    """One row of a client production plan: demand plus optional initial WIP."""

    work_order_id: str
    part: str
    qty: int
    start_date: str
    due_date: str
    initial_wip_location: str | None = None
    initial_wip_qty: int | None = None
    initial_wip_remaining_time: float | None = None
    priority: int = 0
    """Rush priority (A4): a higher number jumps the dispatch queue at every work
    center. Optional `priority` plan column; absent or blank means 0 (normal)."""
    completed_good_qty: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.qty, bool) or not isinstance(self.qty, int) or self.qty < 0:
            raise ValueError("qty must be a non-negative integer")
        if (
            isinstance(self.completed_good_qty, bool)
            or not isinstance(self.completed_good_qty, int)
            or self.completed_good_qty < 0
        ):
            raise ValueError("completed_good_qty must be a non-negative integer")
        wip_fields = (
            self.initial_wip_location,
            self.initial_wip_qty,
            self.initial_wip_remaining_time,
        )
        if any(value is not None for value in wip_fields) and not all(
            value is not None for value in wip_fields
        ):
            raise ValueError("initial WIP fields must be populated together")
        if self.initial_wip_qty is not None and (
            isinstance(self.initial_wip_qty, bool)
            or not isinstance(self.initial_wip_qty, int)
            or self.initial_wip_qty < 0
        ):
            raise ValueError("initial_wip_qty must be a non-negative integer")
        if self.initial_wip_remaining_time is not None and (
            not math.isfinite(self.initial_wip_remaining_time)
            or self.initial_wip_remaining_time < 0
        ):
            raise ValueError("initial_wip_remaining_time must be finite and non-negative")
        if self.qty > 0 and self.completed_good_qty + (self.initial_wip_qty or 0) > self.qty:
            raise ValueError("completed good plus initial WIP exceeds order quantity")


def load_plan(path: str, registry: PartTypeRegistry) -> list[WorkOrder]:
    """Read a plan `.xlsx`/`.csv` into `WorkOrder`s, validated against `registry`.

    See module docstring for the fail-closed rules (D-054, D-007).
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        rows = _read_csv_rows(path)
    elif suffix == ".xlsx":
        rows = _read_xlsx_rows(path)
    else:
        raise ValueError(f"unsupported plan file extension {suffix!r}: {path}")

    if not rows:
        raise ValueError("plan file has no header row")

    header = [str(cell) if cell is not None else "" for cell in rows[0]]
    missing = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing:
        raise ValueError(f"missing required column(s): {', '.join(missing)}")

    work_orders = []
    for row_number, raw_row in enumerate(rows[1:], start=2):
        row = {
            column: (raw_row[index] if index < len(raw_row) else None)
            for index, column in enumerate(header)
        }
        work_orders.append(_parse_row(row, row_number, registry))
    return work_orders


def _read_csv_rows(path: str) -> list[tuple[object, ...]]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return [tuple(row) for row in csv.reader(handle)]


def _read_xlsx_rows(path: str) -> list[tuple[object, ...]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        sheet = workbook.worksheets[0]
        return [tuple(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def _parse_row(row: dict[str, object], row_number: int, registry: PartTypeRegistry) -> WorkOrder:
    for value in row.values():
        _reject_formula(value, row_number)

    part = _to_str(row.get("part"))
    try:
        registry.uom(part)
    except KeyError as exc:
        raise ValueError(f"unknown part {part!r} at row {row_number}") from exc

    location, wip_qty, remaining_time = _parse_wip(row, row_number)

    priority_cell = row.get("priority")
    priority = 0 if _is_blank(priority_cell) else _to_int(priority_cell, row_number, "priority")
    completed_cell = row.get("completed_good_qty")
    completed_good = (
        0
        if _is_blank(completed_cell)
        else _to_int(completed_cell, row_number, "completed_good_qty")
    )

    return WorkOrder(
        work_order_id=_to_str(row.get("work_order_id")),
        part=part,
        qty=_to_int(row.get("qty"), row_number, "qty"),
        start_date=_to_str(row.get("start_date")),
        due_date=_to_str(row.get("due_date")),
        initial_wip_location=location,
        initial_wip_qty=wip_qty,
        initial_wip_remaining_time=remaining_time,
        priority=priority,
        completed_good_qty=completed_good,
    )


def _parse_wip(
    row: dict[str, object], row_number: int
) -> tuple[str | None, int | None, float | None]:
    populated = [column for column in _WIP_COLUMNS if not _is_blank(row.get(column))]
    if not populated:
        return None, None, None
    if len(populated) < len(_WIP_COLUMNS):
        raise ValueError(
            f"partially populated initial_wip columns at row {row_number}: {', '.join(populated)}"
        )
    location = _to_str(row["initial_wip_location"])
    qty = _to_int(row["initial_wip_qty"], row_number, "initial_wip_qty")
    remaining_time = _to_float(
        row["initial_wip_remaining_time"], row_number, "initial_wip_remaining_time"
    )
    return location, qty, remaining_time


def _reject_formula(value: object, row_number: int) -> None:
    if isinstance(value, str) and value.startswith("="):
        raise ValueError(f"formula cell rejected (never evaluated) at row {row_number}: {value!r}")


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip() == ""


def _to_str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _to_int(value: object, row_number: int, field: str) -> int:
    if not isinstance(value, bool) and isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return int(stripped)
        except ValueError:
            pass
    raise ValueError(f"non-numeric {field} at row {row_number}: {value!r}")


def _to_float(value: object, row_number: int, field: str) -> float:
    if not isinstance(value, bool) and isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise ValueError(f"non-numeric {field} at row {row_number}: {value!r}")
