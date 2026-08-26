"""COMP-021 EventLog — unit tests (T-018, red phase).

Contract under test (`src/factory_twin/instrumentation/event_log.py` +
`EVENT_LOG_SCHEMA` re-exported from `src/factory_twin/instrumentation/__init__.py`,
Layer 4, D-017 / D-034):

- `EventLog()` buffers plain tuples in memory during a run via `.append(record)`
  and converts to a Polars DataFrame exactly ONCE, at `.flush(run_dir) -> Path`.
  Nothing is written to disk before `flush()` is called.
- `flush(run_dir)` writes `run_dir/events.parquet`, zstd-compressed, by writing to
  a temp path first and atomically renaming into place — a crash between the
  write and the rename must leave NO readable/partial file at the final path.
- `EVENT_LOG_SCHEMA` (module constant, re-exported via `instrumentation/__init__`)
  is a mapping of column name -> Polars dtype. Every record carries the five
  mandatory timestamps: `queue_arrival_time`, `material_ready_time` (nullable),
  `actual_start`, `actual_end`, `release_time`.
- `PROCESS_NAMES` (module constant on `instrumentation.event_log`) is a single
  shared `pl.Enum` category list, applied identically by every `EventLog`
  instance/worker process — never `pl.Categorical`, which builds a per-file
  local dictionary and collides across independently written files (D-017,
  structure.md "Explicitly rejected").

Committed record-tuple contract (this test file fixes it; the implementer
conforms). Field order matches `EVENT_LOG_SCHEMA`'s key order exactly, which is
also asserted directly below:

    index  field                 type            notes
    -----  --------------------  --------------  -----------------------------
    0      location_id           str (Utf8)      near-unique, no dictionary win
    1      part_id               str (Utf8)      near-unique, no dictionary win
    2      lot_id                str (Utf8)      near-unique, no dictionary win
    3      process_name          str             member of PROCESS_NAMES.categories
    4      qty                   float
    5      queue_arrival_time    float           MANDATORY timestamp
    6      material_ready_time   float | None    MANDATORY timestamp, nullable
    7      actual_start          float           MANDATORY timestamp
    8      actual_end            float           MANDATORY timestamp
    9      release_time          float           MANDATORY timestamp
    10     outcome               str

RED-phase note: `EventLog.__init__` currently raises `NotImplementedError`
(T-019), `instrumentation.EVENT_LOG_SCHEMA` is `None`, and
`instrumentation.event_log.PROCESS_NAMES` does not exist yet. Every test below
must fail for one of those three reasons — never an `ImportError` or a syntax
error — because the module itself already imports cleanly.
"""

from __future__ import annotations

import os

import polars as pl
import pytest

import factory_twin.instrumentation.event_log as event_log_module
from factory_twin.instrumentation import EVENT_LOG_SCHEMA
from factory_twin.instrumentation.event_log import EventLog

RECORD_FIELDS = (
    "location_id",
    "part_id",
    "lot_id",
    "process_name",
    "qty",
    "queue_arrival_time",
    "material_ready_time",
    "actual_start",
    "actual_end",
    "release_time",
    "outcome",
)


def _make_record(
    *,
    location_id: str = "cut_01",
    part_id: str = "blank",
    lot_id: str = "lot-0001",
    process_name: str | None = None,
    qty: float = 10.0,
    queue_arrival_time: float = 0.0,
    material_ready_time: float | None = 0.5,
    actual_start: float = 1.0,
    actual_end: float = 5.0,
    release_time: float = 5.5,
    outcome: str = "good",
) -> tuple[str, str, str, str, float, float, float | None, float, float, float, str]:
    """Build one committed-order record tuple. `process_name` defaults to the
    first declared PROCESS_NAMES category so callers don't have to guess a
    value that happens to be valid."""
    if process_name is None:
        process_name = event_log_module.PROCESS_NAMES.categories[0]
    return (
        location_id,
        part_id,
        lot_id,
        process_name,
        qty,
        queue_arrival_time,
        material_ready_time,
        actual_start,
        actual_end,
        release_time,
        outcome,
    )


# ---------------------------------------------------------------------------
# EVENT_LOG_SCHEMA — the contract itself, no I/O
# ---------------------------------------------------------------------------


def test_event_log_schema_declares_all_five_mandatory_timestamp_columns() -> None:
    for column in (
        "queue_arrival_time",
        "material_ready_time",
        "actual_start",
        "actual_end",
        "release_time",
    ):
        assert column in EVENT_LOG_SCHEMA


def test_event_log_schema_key_order_matches_committed_record_tuple_order() -> None:
    assert list(EVENT_LOG_SCHEMA.keys()) == list(RECORD_FIELDS)


def test_event_log_schema_id_columns_are_utf8() -> None:
    """Hard naming rule (structure.md): part and lot ids stay Utf8 — near-unique,
    so dictionary encoding buys nothing and costs a huge dictionary."""
    for column in ("location_id", "part_id", "lot_id"):
        assert EVENT_LOG_SCHEMA[column] == pl.Utf8


def test_event_log_schema_timestamp_columns_are_float64() -> None:
    for column in (
        "queue_arrival_time",
        "material_ready_time",
        "actual_start",
        "actual_end",
        "release_time",
    ):
        assert EVENT_LOG_SCHEMA[column] == pl.Float64


# ---------------------------------------------------------------------------
# PROCESS_NAMES — the shared pl.Enum module constant
# ---------------------------------------------------------------------------


def test_process_names_is_a_shared_polars_enum_with_at_least_two_categories() -> None:
    assert isinstance(event_log_module.PROCESS_NAMES, pl.Enum)
    assert len(event_log_module.PROCESS_NAMES.categories) >= 2


def test_event_log_schema_process_name_column_uses_the_shared_process_names_enum() -> None:
    assert EVENT_LOG_SCHEMA["process_name"] == event_log_module.PROCESS_NAMES


# ---------------------------------------------------------------------------
# Criterion 1 — five timestamps present, material_ready_time nullable,
# round-trips correctly through a real flushed parquet file.
# ---------------------------------------------------------------------------


def test_flush_writes_events_parquet_containing_all_five_timestamp_columns(
    tmp_path,
) -> None:
    log = EventLog()
    log.append(_make_record())

    written_path = log.flush(tmp_path)
    df = pl.read_parquet(written_path)

    for column in (
        "queue_arrival_time",
        "material_ready_time",
        "actual_start",
        "actual_end",
        "release_time",
    ):
        assert column in df.columns


def test_flush_returns_the_path_to_events_parquet_under_run_dir(tmp_path) -> None:
    log = EventLog()
    log.append(_make_record())

    written_path = log.flush(tmp_path)

    assert written_path == tmp_path / "events.parquet"
    assert written_path.exists()


def test_flushed_timestamp_values_round_trip_exactly(tmp_path) -> None:
    log = EventLog()
    log.append(
        _make_record(
            queue_arrival_time=10.25,
            material_ready_time=12.5,
            actual_start=13.0,
            actual_end=20.75,
            release_time=21.0,
        )
    )

    written_path = log.flush(tmp_path)
    row = pl.read_parquet(written_path).row(0, named=True)

    assert row["queue_arrival_time"] == 10.25
    assert row["material_ready_time"] == 12.5
    assert row["actual_start"] == 13.0
    assert row["actual_end"] == 20.75
    assert row["release_time"] == 21.0


def test_material_ready_time_null_round_trips_as_null(tmp_path) -> None:
    """A firing that never waited on material (or whose wait was already
    resolved before instrumentation could stamp it) carries a null
    material_ready_time — required nullable per REQ-028 / D-034."""
    log = EventLog()
    log.append(_make_record(material_ready_time=None))

    written_path = log.flush(tmp_path)
    df = pl.read_parquet(written_path)

    assert df["material_ready_time"][0] is None
    assert df.schema["material_ready_time"] == pl.Float64


def test_multiple_appended_records_all_present_in_flushed_order(tmp_path) -> None:
    log = EventLog()
    log.append(_make_record(lot_id="lot-a", outcome="good"))
    log.append(_make_record(lot_id="lot-b", outcome="scrap"))
    log.append(_make_record(lot_id="lot-c", outcome="good"))

    written_path = log.flush(tmp_path)
    df = pl.read_parquet(written_path)

    assert df.height == 3
    assert df["lot_id"].to_list() == ["lot-a", "lot-b", "lot-c"]
    assert df["outcome"].to_list() == ["good", "scrap", "good"]


def test_id_columns_stay_utf8_in_the_written_parquet_file(tmp_path) -> None:
    log = EventLog()
    log.append(_make_record())

    written_path = log.flush(tmp_path)
    df = pl.read_parquet(written_path)

    for column in ("location_id", "part_id", "lot_id"):
        assert df.schema[column] == pl.Utf8


def test_append_before_flush_writes_nothing_to_disk(tmp_path) -> None:
    """Records buffer as plain tuples in memory; conversion to a DataFrame and
    the write both happen exactly once, at flush()."""
    log = EventLog()
    log.append(_make_record())

    assert not (tmp_path / "events.parquet").exists()


def test_flush_with_no_appended_records_still_writes_valid_empty_parquet(
    tmp_path,
) -> None:
    """A replication that fires zero transforms must still leave a readable
    file at the run's canonical path, or a sweep-wide glob scan breaks on the
    empty run."""
    log = EventLog()

    written_path = log.flush(tmp_path)
    df = pl.read_parquet(written_path)

    assert df.height == 0
    assert set(df.columns) == set(EVENT_LOG_SCHEMA.keys())


# ---------------------------------------------------------------------------
# Criterion 2 — cross-file Enum: two independently written files scan
# together with no SchemaError, because the category list is one shared
# module constant applied identically by both writers.
# ---------------------------------------------------------------------------


def test_two_separately_written_event_files_scan_together_without_schema_error(
    tmp_path,
) -> None:
    categories = list(event_log_module.PROCESS_NAMES.categories)
    assert len(categories) >= 2, "test needs at least two declared process-name categories"
    process_a, process_b = categories[0], categories[-1]

    run_dir_1 = tmp_path / "run-1"
    run_dir_2 = tmp_path / "run-2"
    run_dir_1.mkdir()
    run_dir_2.mkdir()

    log_1 = EventLog()
    log_1.append(_make_record(process_name=process_a, lot_id="lot-a"))
    path_1 = log_1.flush(run_dir_1)

    log_2 = EventLog()
    log_2.append(_make_record(process_name=process_b, lot_id="lot-b"))
    path_2 = log_2.flush(run_dir_2)

    # The failure this guards against: pl.Categorical builds a per-file local
    # dictionary, so this .collect() would raise SchemaError across two
    # independently written files whose dictionaries disagree.
    merged = pl.scan_parquet([path_1, path_2]).collect()

    assert merged.height == 2
    assert set(merged["process_name"].to_list()) == {process_a, process_b}


def test_merged_process_name_column_keeps_the_shared_enum_dtype_and_categories(
    tmp_path,
) -> None:
    categories = list(event_log_module.PROCESS_NAMES.categories)
    process_a, process_b = categories[0], categories[-1]

    run_dir_1 = tmp_path / "run-1"
    run_dir_2 = tmp_path / "run-2"
    run_dir_1.mkdir()
    run_dir_2.mkdir()

    log_1 = EventLog()
    log_1.append(_make_record(process_name=process_a))
    path_1 = log_1.flush(run_dir_1)

    log_2 = EventLog()
    log_2.append(_make_record(process_name=process_b))
    path_2 = log_2.flush(run_dir_2)

    merged = pl.scan_parquet([path_1, path_2]).collect()

    assert merged.schema["process_name"] == event_log_module.PROCESS_NAMES


# ---------------------------------------------------------------------------
# Criterion 3 — atomic write / crash safety: temp-path-then-rename, so a
# crash mid-write leaves no readable/partial file at the final target path.
# ---------------------------------------------------------------------------


def test_crash_during_atomic_rename_leaves_no_file_at_target_path(tmp_path, monkeypatch) -> None:
    """Simulate the process dying exactly at the rename step (after the temp
    file is fully written). Patches both os.rename and os.replace since
    pathlib.Path.rename/.replace both route through one of these at the OS
    boundary — whichever the implementer calls, this catches it."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated crash during atomic rename")

    monkeypatch.setattr(os, "rename", _boom)
    monkeypatch.setattr(os, "replace", _boom)

    log = EventLog()
    log.append(_make_record())

    target = tmp_path / "events.parquet"
    with pytest.raises(RuntimeError):
        log.flush(tmp_path)

    assert not target.exists()
    with pytest.raises(Exception):  # noqa: B017 - proving no readable file, any error is fine
        pl.read_parquet(target)


def test_crash_after_temp_file_has_real_bytes_leaves_no_file_at_target_path(
    tmp_path, monkeypatch
) -> None:
    """Stronger variant: let the Parquet write actually complete (real bytes
    land on disk at whatever path flush() passes to write_parquet), then
    crash. This genuinely exercises the temp-then-rename guarantee — a buggy
    implementation that writes directly to the final target path (skipping
    the temp file) is caught here because the target would already contain
    those real bytes before the injected crash."""
    original_write_parquet = pl.DataFrame.write_parquet

    def _write_then_crash(self: pl.DataFrame, path: object, *args: object, **kwargs: object):
        original_write_parquet(self, path, *args, **kwargs)
        raise RuntimeError("simulated crash after the temp file was written")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", _write_then_crash)

    log = EventLog()
    log.append(_make_record())

    target = tmp_path / "events.parquet"
    with pytest.raises(RuntimeError):
        log.flush(tmp_path)

    assert not target.exists()


def test_successful_flush_leaves_no_stray_temp_file_behind(tmp_path) -> None:
    """An atomic rename MOVES the temp file into place; nothing extra should
    remain in run_dir afterward."""
    log = EventLog()
    log.append(_make_record())

    written_path = log.flush(tmp_path)

    assert list(tmp_path.iterdir()) == [written_path]


# ---------------------------------------------------------------------------
# Adversarial addition beyond the three listed criteria — zstd compression is
# stated in the CONTRACT ("writes zstd Parquet") but not independently
# inspectable via Polars' public API without pyarrow, which is not a project
# dependency. Proven behaviorally instead: 500 near-duplicate rows compress
# far smaller than an uncompressed write of the identical data.
# ---------------------------------------------------------------------------


def test_flushed_parquet_is_meaningfully_compressed_versus_uncompressed(
    tmp_path,
) -> None:
    rows = [_make_record(lot_id=f"lot-{i:04d}") for i in range(500)]

    log = EventLog()
    for record in rows:
        log.append(record)
    written_path = log.flush(tmp_path)

    baseline = pl.DataFrame(rows, schema=EVENT_LOG_SCHEMA, orient="row")
    baseline_path = tmp_path / "baseline_uncompressed.parquet"
    baseline.write_parquet(baseline_path, compression="uncompressed")

    written_size = written_path.stat().st_size
    baseline_size = baseline_path.stat().st_size
    assert written_size < baseline_size * 0.9, (
        f"flushed file ({written_size} bytes) is not meaningfully smaller than an "
        f"uncompressed write of the identical data ({baseline_size} bytes) — "
        "is zstd compression actually applied?"
    )
