"""Per-firing resource identity and occupied-time evidence."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import polars as pl

RESOURCE_USAGE_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "location_id": pl.Utf8,
    "machine_id": pl.Utf8,
    "labor_pool": pl.Utf8,
    "labor_skill": pl.Utf8,
    "setup_start": pl.Float64,
    "run_start": pl.Float64,
    "run_end": pl.Float64,
    "release_time": pl.Float64,
    "setup_seconds": pl.Float64,
    "run_seconds": pl.Float64,
    "labor_seconds": pl.Float64,
}

ResourceRecord = tuple[str, str, str, str, float, float, float, float, float, float, float]


class ResourceUsageLog:
    """Buffer resource records and atomically write `resource_usage.parquet`."""

    def __init__(self) -> None:
        self._records: list[ResourceRecord] = []

    def append(self, record: ResourceRecord) -> None:
        self._records.append(record)

    def flush(self, run_dir: Path) -> Path:
        frame = pl.DataFrame(self._records, schema=RESOURCE_USAGE_SCHEMA, orient="row")
        target = run_dir / "resource_usage.parquet"
        temporary = run_dir / f".resource-usage.{uuid.uuid4().hex}.parquet.tmp"
        frame.write_parquet(temporary, compression="zstd")
        os.replace(temporary, target)
        return target
