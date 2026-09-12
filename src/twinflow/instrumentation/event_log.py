"""COMP-021 EventLog — owns EVENT_LOG_SCHEMA and writes it.

One ProcessExecution record per firing carrying queue_arrival_time, material_ready_time,
actual_start, actual_end, release_time. Buffers plain tuples, converts once at run end,
writes zstd Parquet to a temp path then atomically renames. Shared pl.Enum categories.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import polars as pl

# Engine-level process/operation kinds — a small fixed vocabulary, never a client's
# work-center name (D-004: no client logic in engine). "transform" is the single
# bundles-in/bundles-out contract (D-043); "rework" and "scrap" are the two operation
# outcomes structure.md names by name (D-045's rework re-entry, the Scrap bundle case).
# A later phase may compute the category list per-run from the model and apply it
# identically across worker processes instead — the shared-list-applied-identically
# guarantee is what matters (D-017), not literal-constness of this vocabulary.
PROCESS_NAMES = pl.Enum(["transform", "rework", "scrap"])

RecordTuple = tuple[str, str, str, str, float, float, float | None, float, float, float, str, float]

# The ProcessExecution contract (D-034): column name -> Polars dtype, in the exact order
# the committed record tuple carries its fields. Defined once here, re-exported from
# instrumentation/__init__.py, and applied identically by every EventLog instance.
EVENT_LOG_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "location_id": pl.Utf8,
    "part_id": pl.Utf8,
    "lot_id": pl.Utf8,
    "process_name": PROCESS_NAMES,
    "qty": pl.Float64,
    "queue_arrival_time": pl.Float64,
    "material_ready_time": pl.Float64,
    "actual_start": pl.Float64,
    "actual_end": pl.Float64,
    "release_time": pl.Float64,
    "outcome": pl.Utf8,
    "setup_seconds": pl.Float64,
}


class EventLog:
    """append(record_tuple) during the run; flush(run_dir) writes events.parquet atomically."""

    def __init__(self) -> None:
        self._records: list[RecordTuple] = []

    def append(self, record: RecordTuple) -> None:
        """Buffer one plain record tuple in memory. No I/O, no DataFrame conversion."""
        self._records.append(record)

    def flush(self, run_dir: Path) -> Path:
        """Convert buffered records to a DataFrame exactly once, then write zstd Parquet
        atomically: write to a temp path in `run_dir`, then `os.replace()` into place. A
        crash before the replace leaves no readable file at the target path."""
        df = pl.DataFrame(self._records, schema=EVENT_LOG_SCHEMA, orient="row")
        target = run_dir / "events.parquet"
        temp_path = run_dir / f".events.{uuid.uuid4().hex}.parquet.tmp"
        df.write_parquet(temp_path, compression="zstd")
        os.replace(temp_path, target)
        return target
