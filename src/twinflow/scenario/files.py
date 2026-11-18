"""Read the input files a capsule points at (TF-006 business showcases).

A planner keeps the product table, the order book and the purchase orders in their own
files, the way the ERP exports them. A capsule opened from a folder may point at them:

- ``model: shop.yaml`` reads the whole model from a YAML file;
- a list one level inside ``model`` or ``snapshot`` may be the name of a CSV file, for
  example ``products: data/products.csv``. Each row becomes one item. A cell that is a
  number becomes a number, a blank cell leaves the field out, and a header such as
  ``delay_risk.minimum_days`` makes a nested field. A column named ``id`` or ending in
  ``_id`` stays text, so ``0042`` keeps its zeros.

Every file must sit inside the capsule's folder. Each problem is a ``ValidationIssue``
that names the capsule field, the file and the fix.
"""

from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from typing import Any

from twinflow.model.loader import parse_document

SECTIONS = ("model", "snapshot")
CSV_SUFFIX = ".csv"
YAML_SUFFIXES = (".yaml", ".yml")


def inline_files(data: dict[str, Any], folder: Path, max_bytes: int) -> dict[str, Any]:
    """A copy of the parsed capsule with every file reference replaced by its content."""
    from twinflow.scenario.capsule import CapsuleValidationError, ValidationIssue

    def fail(path: str, message: str, suggestion: str) -> CapsuleValidationError:
        return CapsuleValidationError([ValidationIssue(path, message, suggestion=suggestion)])

    out = dict(data)
    model = out.get("model")
    if isinstance(model, str) and model.lower().endswith(YAML_SUFFIXES):
        raw = _read(folder, model, "$.model", max_bytes, fail)
        try:
            parsed = parse_document(raw)
        except ValueError as exc:
            raise fail("$.model", f"{model} is not valid YAML: {exc}", "Fix the YAML.") from exc
        if not isinstance(parsed, dict):
            raise fail("$.model", f"{model} must hold named fields.", "Start from an example.")
        out["model"] = parsed
    for section in SECTIONS:
        block = out.get(section)
        if not isinstance(block, dict):
            continue
        inlined = dict(block)
        for key, value in block.items():
            if isinstance(value, str) and value.lower().endswith(CSV_SUFFIX):
                where = f"$.{section}.{key}"
                raw = _read(folder, value, where, max_bytes, fail)
                inlined[key] = _rows(value, raw, where, fail)
        out[section] = inlined
    return out


def _read(folder: Path, name: str, where: str, max_bytes: int, fail: Any) -> bytes:
    base = folder.resolve()
    target = (base / name).resolve()
    if Path(name).is_absolute() or not target.is_relative_to(base):
        raise fail(
            where,
            f"{name} must be inside the capsule's folder.",
            "Move the file next to the capsule and write its name, like data/products.csv.",
        )
    if not target.is_file():
        raise fail(
            where,
            f"{where[2:]} points at {name}, but there is no such file next to the capsule.",
            "Check the file name, or put the file in the capsule's folder.",
        )
    raw = target.read_bytes()
    if len(raw) > max_bytes:
        raise fail(where, f"{name} is larger than {max_bytes} bytes.", "Use a smaller file.")
    return raw


def _rows(name: str, raw: bytes, where: str, fail: Any) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise fail(where, f"{name} is not UTF-8 text.", "Save the file as CSV UTF-8.") from exc
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or not any(cell.strip() for cell in header):
        raise fail(where, f"{name} has no header row.", "Put the column names on the first row.")
    columns = [cell.strip() for cell in header]
    rows = []
    for number, cells in enumerate(reader, start=2):
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) > len(columns):
            raise fail(
                where,
                f"{name} row {number} has more cells than the header has columns.",
                "Remove the extra cells, or quote a cell that holds a comma.",
            )
        row: dict[str, Any] = {}
        for column, cell in zip(columns, cells, strict=False):
            if cell.strip():
                _put(row, column, _value(column, cell.strip()))
        rows.append(row)
    return rows


def _put(row: dict[str, Any], column: str, value: object) -> None:
    *parents, last = column.split(".")
    target = row
    for part in parents:
        target = target.setdefault(part, {})
    target[last] = value


def _value(column: str, cell: str) -> object:
    name = column.rsplit(".", 1)[-1]
    if name == "id" or name.endswith("_id"):
        return cell
    try:
        return int(cell)
    except ValueError:
        pass
    try:
        number = float(cell)
    except ValueError:
        return cell
    return number if math.isfinite(number) else cell
