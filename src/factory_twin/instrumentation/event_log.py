"""COMP-021 EventLog — owns EVENT_LOG_SCHEMA and writes it.

One ProcessExecution record per firing carrying queue_arrival_time, material_ready_time,
actual_start, actual_end, release_time. Buffers plain tuples, converts once at run end,
writes zstd Parquet to a temp path then atomically renames. Shared pl.Enum categories.
"""

from __future__ import annotations


class EventLog:
    """append(record_tuple) during the run; flush(run_dir) writes events.parquet atomically."""

    def __init__(self) -> None:
        raise NotImplementedError("T-019")
