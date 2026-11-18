"""Snapshot digest, version gate and size gate (T-195, SDD COMP-111).

`canonical_json` / `snapshot_digest` give one content address per snapshot that ignores
key order and the `digest` field itself. `parse_snapshot` is the input boundary: it refuses
oversized bytes before parsing, refuses any schema version but "twinflow.erp/1" before model
validation, maps pydantic errors to hard `schema` issues with a JSON path, and checks a
supplied digest.
Requirements: REQ-149, REQ-150, REQ-151.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from _factories import Data, full_snapshot, machine, snapshot

from twinflow.erp.digest import canonical_json, parse_snapshot, snapshot_digest
from twinflow.erp.issues import ErpIssue
from twinflow.erp.schema import ErpSnapshot

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
ACCEPTED_VERSION = "twinflow.erp/1"


# --- helpers --------------------------------------------------------------------------------


def parsed(data: Data) -> ErpSnapshot:
    return ErpSnapshot.model_validate(data)


def reversed_keys(value: Any) -> Any:
    """The same JSON value with every object's keys in reverse insertion order."""
    if isinstance(value, dict):
        return {key: reversed_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_keys(item) for item in value]
    return value


def issues_of(result: ErpSnapshot | list[ErpIssue]) -> list[ErpIssue]:
    assert isinstance(result, list), f"expected issues, got {type(result).__name__}"
    assert all(isinstance(issue, ErpIssue) for issue in result), result
    return result


def codes(result: ErpSnapshot | list[ErpIssue]) -> list[str]:
    return [issue.code for issue in issues_of(result)]


def with_digest(data: Data) -> Data:
    return {**data, "digest": snapshot_digest(parsed(data))}


def invalid_body(version: Any) -> Data:
    """A body that fails model validation in several places; only the version should count."""
    data = snapshot()
    data["as_of"] = "2026-10-05T05:00:00"  # naive
    data["plant_timezone"] = "Mars/Base"
    data["machines"] = "not-a-list"
    data["bogus"] = 1
    if version is _MISSING:
        del data["schema_version"]
    else:
        data["schema_version"] = version
    return data


_MISSING = object()


# --- canonical JSON and digest --------------------------------------------------------------


def _record_key(record: Any) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)


def test_canonical_json_is_sorted_compact_json_without_digest() -> None:
    # Top-level collections are sets of keyed rows: their order is not content (SDD Deviations).
    snap = parsed({**full_snapshot(), "digest": "b" * 64})
    body = snap.model_dump(mode="json", exclude={"digest"})
    for name, value in body.items():
        if isinstance(value, list):
            body[name] = sorted(value, key=_record_key)
    expected = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert canonical_json(snap) == expected


def test_digest_stable_under_reordering_of_top_level_collections() -> None:
    from fixtures.generate import small_shop

    snap = small_shop()
    reordered = snap.model_copy(
        update={
            name: tuple(reversed(getattr(snap, name)))
            for name, field in type(snap).model_fields.items()
            if isinstance(getattr(snap, name), tuple)
        }
    )
    assert any(getattr(reordered, n) != getattr(snap, n) for n in ("machines", "production_orders"))
    assert snapshot_digest(reordered) == snapshot_digest(snap)


def test_digest_changes_when_nested_order_changes() -> None:
    # Nested tuples (an order's operations, a calendar's windows) keep their order as content.
    data = full_snapshot()
    order = data["production_orders"][0]
    if len(order["operations"]) < 2:
        order["operations"].append({**order["operations"][0], "op_id": "zz-second", "sequence": 99})
    before = snapshot_digest(parsed(data))
    order["operations"].reverse()
    assert snapshot_digest(parsed(data)) != before


def test_snapshot_digest_is_sha256_hex_of_canonical_json() -> None:
    snap = parsed(full_snapshot())
    digest = snapshot_digest(snap)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == hashlib.sha256(canonical_json(snap)).hexdigest()


def test_digest_stable_under_key_order() -> None:
    data = full_snapshot()
    assert snapshot_digest(parsed(data)) == snapshot_digest(parsed(reversed_keys(data)))


def test_digest_stable_under_json_round_trip() -> None:
    snap = parsed(full_snapshot())
    round_tripped = ErpSnapshot.model_validate_json(snap.model_dump_json())
    re_read = ErpSnapshot.model_validate(json.loads(canonical_json(snap)))
    assert snapshot_digest(round_tripped) == snapshot_digest(snap)
    assert snapshot_digest(re_read) == snapshot_digest(snap)


def test_digest_ignores_the_digest_field() -> None:
    plain = parsed(full_snapshot())
    stamped = parsed({**full_snapshot(), "digest": "c" * 64})
    assert snapshot_digest(stamped) == snapshot_digest(plain)
    assert canonical_json(stamped) == canonical_json(plain)


def test_digest_is_deterministic_across_calls() -> None:
    assert snapshot_digest(parsed(full_snapshot())) == snapshot_digest(parsed(full_snapshot()))


def _set(path: tuple[str | int, ...], value: Any) -> Callable[[Data], None]:
    def mutate(data: Data) -> None:
        target: Any = data
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value

    return mutate


FIELD_CHANGES: dict[str, Callable[[Data], None]] = {
    "as_of": _set(("as_of",), "2026-10-05T05:00:01-05:00"),
    "plant_timezone": _set(("plant_timezone",), "America/Denver"),
    "source.generation": _set(("source", "generation"), "43"),
    "source.extractor_version": _set(("source", "extractor_version"), "1.0.1"),
    "source_issue.severity": _set(("source_issues", 0, "severity"), "hard"),
    "item.attribute": _set(("items", 0, "attributes", "wire_diameter_mm"), 1.25),
    "item.description": _set(("items", 0, "description"), "Spring!"),
    "machine.efficiency": _set(("machines", 0, "efficiency"), 0.91),
    "machine.active": _set(("machines", 0, "active"), False),
    "order.rush": _set(("production_orders", 0, "rush"), True),
    "order.due_date": _set(("production_orders", 0, "due_date"), "2026-10-21"),
    "op.qty_remaining": _set(("production_orders", 0, "operations", 1, "qty_remaining"), 2.0),
    "op.option.preference": _set(
        ("production_orders", 0, "operations", 0, "options", 0, "preference"), 1
    ),
    "calendar.window": _set(("calendars", 0, "week", 0, "windows", 0, "end_minute"), 841),
    "overtime.multiplier": _set(
        ("machines", 0, "overtime", "week", 0, "windows", 0, "cost_multiplier"), 1.75
    ),
    "lineage.key": _set(("vendors", 0, "lineage", "key"), ["k2"]),
    "purchase.receipt.qty": _set(("purchase_lines", 0, "receipts", 0, "qty"), 2.0),
}


@pytest.mark.parametrize("change", FIELD_CHANGES.values(), ids=list(FIELD_CHANGES))
def test_digest_changes_when_a_field_changes(change: Callable[[Data], None]) -> None:
    original = full_snapshot()
    mutated = copy.deepcopy(original)
    change(mutated)
    assert mutated != original
    assert snapshot_digest(parsed(mutated)) != snapshot_digest(parsed(original))


# --- parse_snapshot: happy path ---------------------------------------------------------------


def test_parse_snapshot_mapping_returns_snapshot() -> None:
    result = parse_snapshot(full_snapshot())
    assert isinstance(result, ErpSnapshot)
    assert result == parsed(full_snapshot())


def test_parse_snapshot_bytes_returns_snapshot() -> None:
    raw = json.dumps(full_snapshot()).encode()
    result = parse_snapshot(raw)
    assert isinstance(result, ErpSnapshot)
    assert snapshot_digest(result) == snapshot_digest(parsed(full_snapshot()))


def test_parse_snapshot_accepts_correct_supplied_digest() -> None:
    data = with_digest(full_snapshot())
    result = parse_snapshot(data)
    assert isinstance(result, ErpSnapshot)
    assert result.digest == data["digest"]


# --- parse_snapshot: digest mismatch -----------------------------------------------------------


def test_parse_snapshot_wrong_digest_is_hard_digest_mismatch() -> None:
    data = {**full_snapshot(), "digest": "0" * 64}
    issues = issues_of(parse_snapshot(data))
    assert [issue.code for issue in issues] == ["digest_mismatch"]
    assert issues[0].severity == "hard"


def test_parse_snapshot_tampered_body_with_stale_digest_is_digest_mismatch() -> None:
    data = with_digest(full_snapshot())
    data["machines"][0]["efficiency"] = 0.5
    assert codes(parse_snapshot(data)) == ["digest_mismatch"]


def test_parse_snapshot_digest_mismatch_through_bytes() -> None:
    raw = json.dumps({**full_snapshot(), "digest": "1" * 64}).encode()
    assert codes(parse_snapshot(raw)) == ["digest_mismatch"]


# --- parse_snapshot: version gate --------------------------------------------------------------


@pytest.mark.parametrize(
    "version",
    ["canonical-erp/0", "twinflow.erp/2", _MISSING, "", None, 1, ["twinflow.erp/1"]],
    ids=["v0", "v2", "missing", "empty", "null", "int", "list"],
)
def test_unsupported_version_is_the_only_issue_before_model_validation(version: Any) -> None:
    issues = issues_of(parse_snapshot(invalid_body(version)))
    assert [issue.code for issue in issues] == ["unsupported_schema_version"]
    assert issues[0].severity == "hard"
    assert ACCEPTED_VERSION in issues[0].message


def test_unsupported_version_on_an_otherwise_valid_snapshot() -> None:
    data = {**full_snapshot(), "schema_version": "twinflow.erp/2"}
    assert codes(parse_snapshot(data)) == ["unsupported_schema_version"]


def test_v0_version_message_points_to_the_v0_to_v1_change_list() -> None:
    issues = issues_of(parse_snapshot(invalid_body("canonical-erp/0")))
    assert "v0-to-v1" in issues[0].message.lower()


def test_unsupported_version_through_bytes() -> None:
    raw = json.dumps(invalid_body("canonical-erp/0")).encode()
    assert codes(parse_snapshot(raw)) == ["unsupported_schema_version"]


def test_version_gate_precedes_digest_check() -> None:
    data = {**full_snapshot(), "schema_version": "twinflow.erp/2", "digest": "0" * 64}
    assert codes(parse_snapshot(data)) == ["unsupported_schema_version"]


# --- parse_snapshot: size gate -------------------------------------------------------------------


def test_bytes_over_16_mib_refused_before_parsing() -> None:
    garbage = b"\xff" * (MAX_SNAPSHOT_BYTES + 1)  # not JSON, not even UTF-8: no parse happened
    issues = issues_of(parse_snapshot(garbage))
    assert [issue.code for issue in issues] == ["size_limit"]
    assert issues[0].severity == "hard"


def test_bytes_of_exactly_16_mib_pass_the_size_gate() -> None:
    garbage = b"\xff" * MAX_SNAPSHOT_BYTES
    issues = issues_of(parse_snapshot(garbage))
    assert issues, "garbage must still be refused"
    assert "size_limit" not in [issue.code for issue in issues]
    assert all(issue.severity == "hard" for issue in issues)


# --- parse_snapshot: schema issues ---------------------------------------------------------------


def test_pydantic_error_becomes_hard_schema_issue_with_json_path() -> None:
    data = snapshot()
    data["machines"] = [{**machine(), "efficiency": 0}]
    issues = issues_of(parse_snapshot(data))
    assert [issue.code for issue in issues] == ["schema"]
    issue = issues[0]
    assert issue.severity == "hard"
    assert issue.path.startswith("$")
    assert "machines" in issue.path
    assert "efficiency" in issue.path


def test_every_pydantic_error_is_reported() -> None:
    data = snapshot()
    data["machines"] = [{**machine(), "efficiency": 0}]
    data["plant_timezone"] = "Mars/Base"
    issues = issues_of(parse_snapshot(data))
    assert [issue.code for issue in issues] == ["schema", "schema"]
    paths = " ".join(issue.path for issue in issues)
    assert "efficiency" in paths
    assert "plant_timezone" in paths


def test_schema_issue_does_not_echo_rejected_values() -> None:
    secret = "s3cr3t-value-" + "x" * 40
    data = {**snapshot(), "bogus": secret}
    issues = issues_of(parse_snapshot(data))
    assert codes(issues) == ["schema"]
    assert secret not in issues[0].message


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"", b"[]", b"null", b'"twinflow.erp/1"', b"{" * 10, b"[" * 200_000],
    ids=["text", "empty", "array", "null", "string", "truncated", "deep-nesting"],
)
def test_malformed_bytes_return_hard_issues_without_raising(raw: bytes) -> None:
    issues = issues_of(parse_snapshot(raw))
    assert issues
    assert all(issue.severity == "hard" for issue in issues)


def test_erp_issue_carries_code_severity_path_lineage_message() -> None:
    assert {"code", "severity", "path", "lineage", "message"} <= set(ErpIssue.model_fields)
