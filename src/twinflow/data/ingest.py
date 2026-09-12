"""Bounded conversion from untrusted JSON objects to normalized events."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from twinflow.data.errors import DataFormatError, EventValidationError
from twinflow.data.models import JsonValue, OperationalEvent, immutable_mapping

MAX_RECORD_BYTES = 1_000_000
MAX_IDENTIFIER_LENGTH = 256
MAX_PAYLOAD_DEPTH = 12
MAX_PAYLOAD_KEYS = 2_000


def parse_event(record: Mapping[str, Any]) -> OperationalEvent:
    """Validate and normalize one parsed source record."""
    required = (
        "source",
        "event_id",
        "source_revision",
        "occurred_at",
        "ingested_at",
        "entity_type",
        "entity_id",
        "event_type",
        "payload",
    )
    missing = [name for name in required if name not in record]
    if missing:
        raise EventValidationError(f"missing required fields: {', '.join(missing)}")
    identifiers = {name: _identifier(record[name], name) for name in required[:3] + required[5:8]}
    payload_value = record["payload"]
    if not isinstance(payload_value, dict) or not all(
        isinstance(key, str) for key in payload_value
    ):
        raise EventValidationError("payload must be a JSON object with string keys")
    _validate_json(payload_value, depth=0, key_count=[0])
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(canonical.encode()) > MAX_RECORD_BYTES:
        raise EventValidationError(f"record exceeds {MAX_RECORD_BYTES} bytes")
    supersedes = record.get("supersedes_event_id")
    if supersedes is not None:
        supersedes = _identifier(supersedes, "supersedes_event_id")
        if supersedes == identifiers["event_id"]:
            raise EventValidationError("an event cannot supersede itself")
    return OperationalEvent(
        source=identifiers["source"],
        event_id=identifiers["event_id"],
        source_revision=identifiers["source_revision"],
        occurred_at=_timestamp(record["occurred_at"], "occurred_at"),
        ingested_at=_timestamp(record["ingested_at"], "ingested_at"),
        entity_type=identifiers["entity_type"],
        entity_id=identifiers["entity_id"],
        event_type=identifiers["event_type"],
        payload=immutable_mapping(cast(dict[str, JsonValue], payload_value)),
        supersedes_event_id=supersedes,
        raw_record=canonical,
        raw_digest=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def load_events(path: str | Path) -> Iterator[OperationalEvent]:
    """Stream normalized events from a JSON array/object or JSONL file."""
    source_path = Path(path)
    try:
        with source_path.open(encoding="utf-8") as handle:
            if source_path.suffix.lower() == ".jsonl":
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise DataFormatError(
                            f"invalid JSONL on line {line_number}: {exc.msg}"
                        ) from exc
                    if not isinstance(value, dict):
                        raise DataFormatError(f"JSONL line {line_number} must be an object")
                    yield parse_event(value)
                return
            try:
                document = json.load(handle)
            except json.JSONDecodeError as exc:
                raise DataFormatError(f"invalid JSON: {exc.msg}") from exc
    except OSError as exc:
        raise DataFormatError(f"cannot read {source_path}: {exc}") from exc
    records = document if isinstance(document, list) else [document]
    for index, value in enumerate(records):
        if not isinstance(value, dict):
            raise DataFormatError(f"JSON record {index} must be an object")
        yield parse_event(value)


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventValidationError(f"{name} must be a non-empty string")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise EventValidationError(f"{name} exceeds {MAX_IDENTIFIER_LENGTH} characters")
    return value


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise EventValidationError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventValidationError(f"{name} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventValidationError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _validate_json(value: object, *, depth: int, key_count: list[int]) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise EventValidationError(f"payload exceeds maximum depth {MAX_PAYLOAD_DEPTH}")
    if isinstance(value, float) and not math.isfinite(value):
        raise EventValidationError("payload numbers must be finite")
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1, key_count=key_count)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        key_count[0] += len(value)
        if key_count[0] > MAX_PAYLOAD_KEYS:
            raise EventValidationError(f"payload exceeds {MAX_PAYLOAD_KEYS} keys")
        for item in value.values():
            _validate_json(item, depth=depth + 1, key_count=key_count)
        return
    raise EventValidationError("payload contains a non-JSON value")
