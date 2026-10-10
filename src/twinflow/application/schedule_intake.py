"""Bounded schedule upload preview and explicit row mapping."""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import math
import zipfile
from typing import Any

import openpyxl  # type: ignore[import-untyped]

MAX_UPLOAD_BYTES = 1_048_576
MAX_ROWS = 1_000
MAX_COLUMNS = 100
FIELDS = ("order_id", "product", "quantity", "release_second", "due_second")


def decode_upload(filename: str, content_base64: str) -> tuple[list[str], list[list[str]], str]:
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Upload must be valid base64") from exc
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("Upload must contain 1..1048576 bytes")
    digest = hashlib.sha256(content).hexdigest()
    if filename.lower().endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
            reader = csv.reader(io.StringIO(text), strict=True)
            rows = [list(row) for row in reader]
        except (UnicodeError, csv.Error) as exc:
            raise ValueError("CSV must be valid UTF-8 with well-formed rows") from exc
    elif filename.lower().endswith(".xlsx"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if (
                    len(archive.infolist()) > 100
                    or sum(member.file_size for member in archive.infolist()) > 5_242_880
                ):
                    raise ValueError("XLSX decompressed content exceeds safety limit")
            workbook = openpyxl.load_workbook(
                io.BytesIO(content), read_only=True, data_only=False, keep_links=False
            )
            if len(workbook.worksheets) != 1:
                raise ValueError("XLSX must have exactly one worksheet")
            rows = []
            for row in workbook.active.iter_rows():
                if any(cell.data_type == "f" for cell in row):
                    raise ValueError("XLSX formulas are not accepted; upload values only")
                rows.append(["" if cell.value is None else str(cell.value) for cell in row])
                if len(rows) > MAX_ROWS + 1:
                    break
            workbook.close()
        except (zipfile.BadZipFile, OSError, KeyError) as exc:
            raise ValueError("XLSX file is malformed") from exc
    else:
        raise ValueError("Upload filename must end in .csv or .xlsx")
    if not rows or len(rows) < 2 or len(rows) - 1 > MAX_ROWS:
        raise ValueError("Upload must contain a header and 1..1000 data rows")
    headers = rows[0]
    if not headers or any(not header.strip() for header in headers):
        raise ValueError("Every upload column needs a header")
    if len(headers) > MAX_COLUMNS:
        raise ValueError("Upload has more than 100 columns")
    if len(headers) != len(set(headers)):
        raise ValueError("Upload headers must be unique")
    if any(header.lstrip().startswith(("=", "+", "-", "@")) for header in headers):
        raise ValueError("Upload headers cannot contain formula-like values")
    for index, row in enumerate(rows[1:], start=2):
        if len(row) != len(headers):
            raise ValueError(f"Row {index} has {len(row)} cells; expected {len(headers)}")
        if any(value.lstrip().startswith(("=", "+", "-", "@")) for value in row):
            raise ValueError(f"Row {index} contains a formula-like value")
    return headers, rows[1:], digest


def preview_mapping(
    headers: list[str],
    rows: list[list[str]],
    mapping: dict[str, str] | None,
    products: set[str],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if mapping is None:
        return {
            "issues": [
                {
                    "row": None,
                    "field": "mapping",
                    "code": "mapping_required",
                    "message": "Confirm a column for every required field",
                }
            ],
            "orders": [],
            "committable": False,
        }
    if set(mapping) != set(FIELDS) or len(set(mapping.values())) != len(FIELDS):
        issues.append(
            {
                "row": None,
                "field": "mapping",
                "code": "invalid_mapping",
                "message": "Map each required field to a different column",
            }
        )
    for field, column in mapping.items():
        if column not in headers:
            issues.append(
                {
                    "row": None,
                    "field": field,
                    "code": "unknown_column",
                    "message": f"Column {column!r} is absent",
                }
            )
    if issues:
        return {"issues": issues, "orders": [], "committable": False}
    positions = {field: headers.index(mapping[field]) for field in FIELDS}
    orders: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        values = {field: row[position].strip() for field, position in positions.items()}
        row_issues = 0

        def add(field: str, code: str, message: str, row_index: int = row_number) -> None:
            nonlocal row_issues
            row_issues += 1
            issues.append({"row": row_index, "field": field, "code": code, "message": message})

        for field, value in values.items():
            if not value:
                add(field, "missing", "Cell is empty")
        order_id = values["order_id"]
        if order_id in seen:
            add("order_id", "duplicate", "Order ID appears more than once")
        seen.add(order_id)
        if values["product"] and values["product"] not in products:
            add("product", "unknown_product", "Product is absent from scenario routing")
        numeric: dict[str, float] = {}
        for field in ("quantity", "release_second", "due_second"):
            if not values[field]:
                continue
            try:
                numeric[field] = float(values[field])
            except ValueError:
                add(field, "invalid_number", "Use a finite number of pieces or seconds")
                continue
            if not math.isfinite(numeric[field]):
                add(field, "invalid_number", "Use a finite number")
        if "quantity" in numeric and (
            not numeric["quantity"].is_integer() or not 1 <= numeric["quantity"] <= 1_000_000
        ):
            add("quantity", "invalid_quantity", "Quantity must be a positive whole number")
        if "release_second" in numeric and not 0 <= numeric["release_second"] <= 31_536_000:
            add("release_second", "invalid_time", "Release seconds must be 0..31536000")
        if "due_second" in numeric and not 0 < numeric["due_second"] <= 31_536_000:
            add("due_second", "invalid_time", "Due seconds must be 1..31536000")
        if "due_second" in numeric and numeric["due_second"] <= numeric.get("release_second", 0):
            add("due_second", "invalid_time", "Due seconds must follow release")
        if not row_issues:
            orders.append(
                {
                    "row": row_number,
                    "order_id": order_id,
                    "product": values["product"],
                    "quantity": int(numeric["quantity"]),
                    "release_second": numeric["release_second"],
                    "due_second": numeric["due_second"],
                }
            )
    return {"issues": issues, "orders": orders if not issues else [], "committable": not issues}
