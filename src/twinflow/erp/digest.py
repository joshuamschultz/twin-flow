"""Snapshot digest, size gate and version gate (COMP-111).

`canonical_json` gives one byte string per snapshot content: the JSON-mode dump without
the `digest` field, keys sorted, compact separators, no NaN, and `ensure_ascii=True` so the
bytes are pure ASCII and never depend on a text encoding. `snapshot_digest` is its SHA-256.

`parse_snapshot` is the input boundary. In order it refuses: more than `MAX_SNAPSHOT_BYTES`
of JSON (before parsing bytes), input that is not a JSON object, any `schema_version` but
`twinflow.erp/1` (before model validation, so it is the only issue), every model error (one
hard `schema` issue each, with a `$`-rooted path and no input value), and a supplied digest
that differs from the recomputed one. A refusal is a list of hard issues, never an exception.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from twinflow.erp.issues import ErpIssue
from twinflow.erp.schema import ERP_SCHEMA_VERSION, ErpSnapshot
from twinflow.scheduling.model import MAX_ID_LENGTH

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
V0_SCHEMA_VERSION = "canonical-erp/0"
V0_CHANGE_LIST = "reference/canonical-v0-to-v1-changes.md"
ROOT_PATH = "$"


def canonical_json(snapshot: ErpSnapshot) -> bytes:
    """The canonical ASCII JSON bytes of `snapshot`, excluding its `digest` field."""
    body = snapshot.model_dump(mode="json", exclude={"digest"})
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return text.encode("ascii")


def snapshot_digest(snapshot: ErpSnapshot) -> str:
    """SHA-256 hex digest of `canonical_json(snapshot)`: the snapshot's content address."""
    return hashlib.sha256(canonical_json(snapshot)).hexdigest()


def parse_snapshot(raw: Mapping[str, Any] | bytes) -> ErpSnapshot | list[ErpIssue]:
    """Parse raw JSON bytes or a decoded mapping into a snapshot, or return hard issues."""
    data = _decode(raw)
    if isinstance(data, list):
        return data
    if data.get("schema_version") != ERP_SCHEMA_VERSION:
        return [_version_issue(data.get("schema_version"))]
    try:
        snapshot = ErpSnapshot.model_validate(data)
    except ValidationError as error:
        return [
            _hard("schema", _json_path(detail["loc"]), detail["msg"])
            for detail in error.errors(include_input=False)
        ]
    if snapshot.digest is not None and snapshot.digest != snapshot_digest(snapshot):
        return [_digest_mismatch()]
    return snapshot


# --- decoding and the size gate --------------------------------------------------------------


def _decode(raw: Mapping[str, Any] | bytes) -> dict[str, Any] | list[ErpIssue]:
    """The input as a JSON object, or the hard issue that stops it."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_SNAPSHOT_BYTES:
            return [_size_issue()]
        decoded = _loads(raw)
    else:
        decoded = _sized(raw)
    if isinstance(decoded, ErpIssue):
        return [decoded]
    if not isinstance(decoded, Mapping):
        return [_hard("schema", ROOT_PATH, "a snapshot must be a JSON object")]
    return dict(decoded)


def _loads(raw: bytes) -> Any:
    """Decoded JSON, or a hard issue for bytes that are not JSON (or nest too deep)."""
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return _hard("schema", ROOT_PATH, "snapshot bytes are not valid JSON")


def _sized(data: Mapping[str, Any]) -> Mapping[str, Any] | ErpIssue:
    """The mapping if its compact UTF-8 JSON encoding fits the limit, else a hard issue."""
    try:
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        # Foreign types, cycles or runaway depth: this is not a JSON document.
        return _hard("schema", ROOT_PATH, "snapshot mapping is not JSON-encodable")
    if len(text.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        return _size_issue()
    return data


# --- issues -----------------------------------------------------------------------------------


def _hard(code: str, path: str, message: str) -> ErpIssue:
    return ErpIssue(code=code, severity="hard", path=path, message=message)


def _size_issue() -> ErpIssue:
    return _hard(
        "size_limit",
        ROOT_PATH,
        f"snapshot JSON exceeds {MAX_SNAPSHOT_BYTES} bytes and was not parsed",
    )


def _version_issue(version: object) -> ErpIssue:
    """`unsupported_schema_version`; the rejected value is never echoed back."""
    message = f'schema_version must be "{ERP_SCHEMA_VERSION}"'
    if version == V0_SCHEMA_VERSION:
        message += f"; migrate {V0_SCHEMA_VERSION} snapshots using the v0-to-v1 change list"
        message += f" ({V0_CHANGE_LIST})"
    return _hard("unsupported_schema_version", f"{ROOT_PATH}.schema_version", message)


def _digest_mismatch() -> ErpIssue:
    return _hard(
        "digest_mismatch",
        f"{ROOT_PATH}.digest",
        "supplied digest does not match the SHA-256 of the canonical snapshot JSON",
    )


def _json_path(loc: tuple[int | str, ...]) -> str:
    """A `$`-rooted JSON path such as `$.machines[0].efficiency` from a pydantic location."""
    return ROOT_PATH + "".join(_path_segment(part) for part in loc)


def _path_segment(part: int | str) -> str:
    if isinstance(part, int):
        return f"[{part}]"
    if len(part) > MAX_ID_LENGTH:
        return "[*]"  # an over-long mapping key is itself the rejected value: do not echo it
    if part.isidentifier():
        return f".{part}"
    return f"[{json.dumps(part)}]"
