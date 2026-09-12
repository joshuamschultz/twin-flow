from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime

import pytest

from twinflow.data import (
    ActualRecord,
    EventConflictError,
    ForecastRecord,
    FreshnessRule,
    SnapshotBuilder,
    SQLiteEventStore,
    parse_event,
    score_forecasts,
)


def _event(
    event_id: str,
    revision: str,
    occurred: str,
    ingested: str,
    payload: dict[str, object],
    *,
    supersedes: str | None = None,
    source: str = "erp",
):
    return parse_event(
        {
            "source": source,
            "event_id": event_id,
            "source_revision": revision,
            "occurred_at": occurred,
            "ingested_at": ingested,
            "entity_type": "order",
            "entity_id": "o-1",
            "event_type": "updated",
            "payload": payload,
            "supersedes_event_id": supersedes,
        }
    )


def test_ingest_is_idempotent_and_preserves_raw_lineage(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    event = _event("e-1", "1", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z", {"qty": 2})
    assert store.ingest([event]).inserted == 1
    result = store.ingest([event])
    assert result.duplicates == 1
    assert store.raw_record(event.identity) == event.raw_record

    conflicting = parse_event({**json.loads(event.raw_record), "payload": {"qty": 3}})
    with pytest.raises(EventConflictError):
        store.ingest([conflicting])


def test_known_at_prevents_late_correction_leakage(tmp_path) -> None:
    original = _event("bad", "1", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z", {"qty": 2})
    correction = _event(
        "fix",
        "1",
        "2026-01-01T00:02:00Z",
        "2026-01-03T00:00:00Z",
        {"qty": 5},
        supersedes="bad",
    )
    store = SQLiteEventStore(tmp_path / "events.db")
    store.ingest([correction, original])
    builder = SnapshotBuilder(store)
    before = builder.as_known_at(datetime(2026, 1, 2, tzinfo=UTC))
    after = builder.as_known_at(datetime(2026, 1, 4, tzinfo=UTC))
    assert before.entities[0].values["qty"] == 2
    assert after.entities[0].values["qty"] == 5
    assert original.identity not in after.event_identities


def test_snapshot_is_deterministic_under_reordering_and_revisions(tmp_path) -> None:
    events = [
        _event("e", "2", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z", {"qty": 4}),
        _event("e", "1", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", {"qty": 3}),
        _event("status", "1", "2026-01-02T00:00:00Z", "2026-01-02T00:01:00Z", {"status": "open"}),
    ]
    expected: tuple[str, dict[str, object]] | None = None
    for index, shuffled in enumerate(itertools.permutations(events)):
        store = SQLiteEventStore(tmp_path / f"events-{index}.db")
        store.ingest(shuffled)
        snapshot = SnapshotBuilder(store).as_known_at(datetime(2026, 1, 3, tzinfo=UTC))
        observed = (snapshot.snapshot_id, dict(snapshot.entities[0].values))
        expected = observed if expected is None else expected
        assert observed == expected
    assert expected is not None and expected[1] == {"qty": 4, "status": "open"}


def test_occurrence_cutoff_and_freshness_are_explicit(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    store.ingest([_event("e", "1", "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z", {"qty": 1})])
    snapshot = SnapshotBuilder(
        store, freshness_rules=[FreshnessRule("orders", 3600, source="erp")]
    ).as_known_at(
        datetime(2026, 1, 3, tzinfo=UTC), occurred_through=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert not snapshot.entities
    assert snapshot.freshness.items[0].status == "missing"
    assert not snapshot.ready


def test_equivalent_source_time_offsets_share_cutoff_semantics(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    store.ingest(
        [
            _event(
                "e",
                "1",
                "2025-12-31T19:00:00-05:00",
                "2025-12-31T19:01:00-05:00",
                {"qty": 1},
            )
        ]
    )
    included = SnapshotBuilder(store).as_known_at(datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
    excluded = SnapshotBuilder(store).as_known_at(datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC))
    assert included.entities[0].values["qty"] == 1
    assert not excluded.entities


def test_backtest_scores_mature_rows_and_retains_unknowns() -> None:
    forecasts = [
        ForecastRecord(
            "a",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 5, tzinfo=UTC),
            {0.9: datetime(2026, 1, 8, tzinfo=UTC)},
            baseline_date=datetime(2026, 1, 10, tzinfo=UTC),
        ),
        ForecastRecord("b", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 7, tzinfo=UTC)),
        ForecastRecord("c", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 7, tzinfo=UTC)),
    ]
    report = score_forecasts(
        forecasts,
        [
            ActualRecord("a", observed_date=datetime(2026, 1, 6, tzinfo=UTC)),
            ActualRecord("b", censored_at=datetime(2026, 2, 1, tzinfo=UTC)),
        ],
    )
    assert (report.scored_count, report.censored_count, report.missing_count) == (1, 1, 1)
    assert report.mean_absolute_error_days == 1
    assert report.quantile_coverage[0].empirical_coverage == 1
    assert report.baseline_improvement_fraction == pytest.approx(0.75)
