"""Deterministic known-at snapshot reconciliation and quality assessment."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType

from twinflow.data.models import (
    DataDiagnostic,
    DataQualityReport,
    EntityState,
    FreshnessAssessment,
    FreshnessItem,
    FreshnessRule,
    JsonValue,
    OperationalEvent,
    Snapshot,
    immutable_mapping,
)
from twinflow.data.repository import EventStore, snapshot_to_dict


class SnapshotBuilder:
    """Build content-addressed state while excluding facts learned after the cutoff."""

    def __init__(
        self,
        store: EventStore,
        *,
        freshness_rules: Iterable[FreshnessRule] = (),
        delayed_after_seconds: float = 86_400.0,
    ) -> None:
        self._store = store
        self._freshness_rules = tuple(freshness_rules)
        if not math.isfinite(delayed_after_seconds) or delayed_after_seconds < 0:
            raise ValueError("delayed_after_seconds must be non-negative")
        if any(
            not math.isfinite(rule.maximum_age_seconds) or rule.maximum_age_seconds < 0
            for rule in self._freshness_rules
        ):
            raise ValueError("freshness maximum ages must be non-negative")
        self._delayed_after_seconds = delayed_after_seconds

    def as_known_at(
        self, known_at: datetime, *, occurred_through: datetime | None = None, save: bool = False
    ) -> Snapshot:
        """Build a reproducible snapshot from facts available at `known_at`."""
        known_at = _aware_utc(known_at, "known_at")
        through = _aware_utc(occurred_through or known_at, "occurred_through")
        events = self._store.events(known_at=known_at, occurred_through=through)
        active_list, diagnostics = _active_events(events, self._delayed_after_seconds)
        active = tuple(sorted(active_list, key=_event_order))
        state: dict[tuple[str, str], dict[str, JsonValue]] = {}
        watermarks: dict[str, datetime] = {}
        for event in active:
            watermarks[event.source] = max(
                watermarks.get(event.source, event.ingested_at), event.ingested_at
            )
            key = (event.entity_type, event.entity_id)
            if event.event_type == "entity_deleted":
                state.pop(key, None)
                continue
            entity = state.setdefault(key, {})
            for field_name, value in event.payload.items():
                if value is None:
                    entity.pop(field_name, None)
                else:
                    entity[field_name] = value
        entities = tuple(
            EntityState(kind, identifier, immutable_mapping(values))
            for (kind, identifier), values in sorted(state.items())
        )
        quality = DataQualityReport(len(events), tuple(diagnostics))
        freshness = _assess_freshness(active, known_at, self._freshness_rules)
        provisional = Snapshot(
            snapshot_id="",
            known_at=known_at,
            occurred_through=through,
            source_watermarks=MappingProxyType(dict(watermarks)),
            entities=entities,
            event_identities=tuple(event.identity for event in active),
            quality=quality,
            freshness=freshness,
        )
        digest_input = snapshot_to_dict(provisional)
        digest_input.pop("snapshot_id")
        snapshot_id = (
            "snap-"
            + hashlib.sha256(
                json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:20]
        )
        snapshot = replace(provisional, snapshot_id=snapshot_id)
        if save:
            self._store.save_snapshot(snapshot)
        return snapshot


def _active_events(
    events: tuple[OperationalEvent, ...], delayed_after_seconds: float
) -> tuple[list[OperationalEvent], list[DataDiagnostic]]:
    diagnostics: list[DataDiagnostic] = []
    by_identity: dict[tuple[str, str], list[OperationalEvent]] = defaultdict(list)
    for event in events:
        by_identity[(event.source, event.event_id)].append(event)
        lag = (event.ingested_at - event.occurred_at).total_seconds()
        if lag < 0:
            diagnostics.append(
                DataDiagnostic(
                    "future_occurrence", "error", "occurred_at follows ingested_at", event.identity
                )
            )
        elif lag > delayed_after_seconds:
            diagnostics.append(
                DataDiagnostic(
                    "delayed_ingestion",
                    "warning",
                    f"ingestion lag is {lag:.0f} seconds",
                    event.identity,
                )
            )
        if not event.payload:
            diagnostics.append(
                DataDiagnostic("empty_payload", "warning", "event payload is empty", event.identity)
            )
    latest: dict[tuple[str, str], OperationalEvent] = {}
    for key, revisions in by_identity.items():
        latest[key] = max(revisions, key=lambda item: _natural_revision(item.source_revision))
        numeric = sorted(
            int(item.source_revision) for item in revisions if item.source_revision.isdigit()
        )
        if numeric and any(b - a != 1 for a, b in zip(numeric, numeric[1:], strict=False)):
            diagnostics.append(
                DataDiagnostic("revision_gap", "warning", f"revision gap for {key[0]}:{key[1]}")
            )
    suppressed: set[tuple[str, str]] = set()
    for event in latest.values():
        if event.supersedes_event_id is None:
            continue
        target = (event.source, event.supersedes_event_id)
        if target not in latest:
            diagnostics.append(
                DataDiagnostic(
                    "unresolved_correction",
                    "error",
                    f"unknown superseded event {target}",
                    event.identity,
                )
            )
        else:
            suppressed.add(target)
    return [event for key, event in latest.items() if key not in suppressed], diagnostics


def _natural_revision(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"(\d+)", value)
    )


def _event_order(
    event: OperationalEvent,
) -> tuple[datetime, datetime, str, str, tuple[tuple[int, int | str], ...]]:
    return (
        event.occurred_at,
        event.ingested_at,
        event.source,
        event.event_id,
        _natural_revision(event.source_revision),
    )


def _assess_freshness(
    events: tuple[OperationalEvent, ...], as_of: datetime, rules: tuple[FreshnessRule, ...]
) -> FreshnessAssessment:
    items: list[FreshnessItem] = []
    for rule in rules:
        matching = [
            event
            for event in events
            if (rule.source is None or event.source == rule.source)
            and (rule.entity_type is None or event.entity_type == rule.entity_type)
        ]
        latest = max((event.occurred_at for event in matching), default=None)
        if latest is None:
            items.append(FreshnessItem(rule.name, "missing", None, None))
            continue
        age = max(0.0, (as_of - latest).total_seconds())
        items.append(
            FreshnessItem(
                rule.name, "fresh" if age <= rule.maximum_age_seconds else "stale", latest, age
            )
        )
    return FreshnessAssessment(as_of, tuple(items))


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(UTC)
