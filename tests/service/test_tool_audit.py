"""COMP-122 tool audit (T-211).

Requirements: REQ-170 (no token, path, env value or argument value in any audit
output), REQ-176 (exactly one audit row and one structured log line per tool call).

Pinned choices beyond the orchestrator API:
- ``ToolAuditor(repository, clock=...)``: ``clock`` returns a timezone-aware ``datetime``;
  ``occurred_at`` is its ISO-8601 text.
- ``record`` validates before writing (fail closed, nothing written on refusal):
  ``outcome`` must be ``ok``, ``denied``, ``rate_limited``, ``timeout`` or
  ``error:<code>`` with a non-empty code; ``input_digest`` must be 64 lowercase hex
  (so an argument value can never be smuggled in as the digest). Both raise ``ValueError``.
- ``repository.audit_events()`` rows carry the new keys ``caller_id``, ``tool``,
  ``request_key``, ``input_digest``, ``duration_ms``; rows written before the migration
  return ``None`` for them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from twinflow.application.repository import WorkspaceRepository
from twinflow.service.audit import ToolAuditor

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
NEW_COLUMNS = {"caller_id", "tool", "request_key", "input_digest", "duration_ms"}
OLD_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS audit_events ("
    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL, "
    "operation TEXT NOT NULL, object_kind TEXT NOT NULL, object_id TEXT NOT NULL, "
    "outcome TEXT NOT NULL, correlation_id TEXT)"
)


def _digest(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _record_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "caller_id": "arc",
        "tool": "solve_plan",
        "classification": "state_modifying",
        "request_key": "rk-1",
        "input_digest": _digest({"problem_id": "p-1"}),
        "outcome": "ok",
        "duration_ms": 12.5,
        "correlation_id": "corr-1",
    }
    return {**base, **overrides}


@pytest.fixture
def repository(tmp_path: Path) -> WorkspaceRepository:
    return WorkspaceRepository(tmp_path / "workspace")


@pytest.fixture
def auditor(repository: WorkspaceRepository) -> ToolAuditor:
    return ToolAuditor(repository, clock=lambda: NOW)


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "twinflow.audit"]


# --------------------------------------------------------------------------- one row + one log


@pytest.mark.parametrize(
    "outcome", ["ok", "error:invalid_input", "denied", "rate_limited", "timeout"]
)
def test_record_writes_exactly_one_row_with_all_fields(
    repository: WorkspaceRepository,
    auditor: ToolAuditor,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    before = len(repository.audit_events(limit=1000))
    caplog.set_level(logging.INFO, logger="twinflow.audit")

    auditor.record(**_record_kwargs(outcome=outcome))

    rows = repository.audit_events(limit=1000)
    assert len(rows) == before + 1
    row = rows[0]
    assert row["operation"] == "tool_call"
    assert row["object_kind"] == "tool"
    assert row["object_id"] == "solve_plan"
    assert row["outcome"] == outcome
    assert row["caller_id"] == "arc"
    assert row["tool"] == "solve_plan"
    assert row["request_key"] == "rk-1"
    assert row["input_digest"] == _digest({"problem_id": "p-1"})
    assert row["duration_ms"] == pytest.approx(12.5)
    assert row["correlation_id"] == "corr-1"
    assert str(row["occurred_at"]).startswith("2026-10-10T12:00:00")


@pytest.mark.parametrize(
    "outcome", ["ok", "error:invalid_input", "denied", "rate_limited", "timeout"]
)
def test_record_emits_exactly_one_structured_log_record(
    auditor: ToolAuditor, caplog: pytest.LogCaptureFixture, outcome: str
) -> None:
    caplog.set_level(logging.INFO, logger="twinflow.audit")

    auditor.record(**_record_kwargs(outcome=outcome))

    (record,) = _audit_records(caplog)
    fields = _record_kwargs(outcome=outcome)
    for name in (
        "caller_id",
        "tool",
        "classification",
        "request_key",
        "input_digest",
        "outcome",
        "correlation_id",
    ):
        assert getattr(record, name) == fields[name], name
    assert record.duration_ms == pytest.approx(12.5)


def test_record_stores_missing_request_key_as_none(
    repository: WorkspaceRepository, auditor: ToolAuditor
) -> None:
    auditor.record(**_record_kwargs(request_key=None))

    assert repository.audit_events()[0]["request_key"] is None


@pytest.mark.parametrize("outcome", ["boom", "error:", "OK", "", "error"])
def test_record_refuses_unknown_outcome_and_writes_nothing(
    repository: WorkspaceRepository,
    auditor: ToolAuditor,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    caplog.set_level(logging.INFO, logger="twinflow.audit")

    with pytest.raises(ValueError):
        auditor.record(**_record_kwargs(outcome=outcome))

    assert repository.audit_events() == []
    assert _audit_records(caplog) == []


@pytest.mark.parametrize("digest", ["not-a-digest", "A" * 64, "a" * 63, "/etc/secret/tokens.json"])
def test_record_refuses_input_digest_that_is_not_64_hex(
    repository: WorkspaceRepository, auditor: ToolAuditor, digest: str
) -> None:
    with pytest.raises(ValueError):
        auditor.record(**_record_kwargs(input_digest=digest))

    assert repository.audit_events() == []


# --------------------------------------------------------------------------- canary scan


def test_no_token_path_env_or_argument_value_reaches_audit_row_or_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    canary_token = "-".join(("CANARYTOKEN", "tf005", "do", "not", "log"))
    canary_env = "CANARY-ENV-VALUE-tf005"
    canary_arg = "CANARY-ARGUMENT-VALUE-tf005"
    canary_root = tmp_path / "CANARY-PATH-tf005"
    monkeypatch.setenv("TWINFLOW_AUDIT_CANARY", canary_env)
    monkeypatch.setenv("TWINFLOW_TOKENS_FILE", str(canary_root / "tokens.json"))
    repository = WorkspaceRepository(canary_root)
    auditor = ToolAuditor(repository, clock=lambda: NOW)
    caplog.set_level(logging.DEBUG)
    arguments = {"note": canary_arg, "auth": canary_token, "path": str(canary_root)}

    for outcome in ("ok", "error:internal", "denied", "rate_limited", "timeout"):
        auditor.record(**_record_kwargs(outcome=outcome, input_digest=_digest(arguments)))

    rows = repository.audit_events(limit=1000)
    assert len(rows) == 5
    assert all(len(str(row["input_digest"])) == 64 for row in rows)
    haystacks = [json.dumps(rows, default=str)]
    for record in caplog.records:
        haystacks.append(record.getMessage())
        haystacks.append(json.dumps(record.__dict__, default=str))
    for canary in (canary_token, canary_env, canary_arg, "CANARY-PATH-tf005"):
        for haystack in haystacks:
            assert canary not in haystack, canary


# --------------------------------------------------------------------------- migration


def _old_workspace(root: Path) -> Path:
    root.mkdir(parents=True)
    path = root / "workspace.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(OLD_AUDIT_DDL)
        db.execute(
            "INSERT INTO audit_events"
            "(occurred_at,operation,object_kind,object_id,outcome,correlation_id) "
            "VALUES(?,?,?,?,?,?)",
            ("2026-09-01T00:00:00+00:00", "approve", "proposal", "p-9", "ok", "c-old"),
        )
    db.close()
    return path


def _columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(audit_events)")}
    db.close()
    return columns


def test_forward_migration_adds_columns_and_keeps_old_rows(tmp_path: Path) -> None:
    path = _old_workspace(tmp_path / "old")

    repository = WorkspaceRepository(tmp_path / "old")

    assert NEW_COLUMNS <= _columns(path)
    (row,) = repository.audit_events()
    assert row["operation"] == "approve"
    assert row["object_id"] == "p-9"
    assert row["correlation_id"] == "c-old"
    for column in NEW_COLUMNS:
        assert row[column] is None, column


def test_forward_migration_is_idempotent_and_new_rows_follow_old_ones(tmp_path: Path) -> None:
    _old_workspace(tmp_path / "old")
    WorkspaceRepository(tmp_path / "old")

    repository = WorkspaceRepository(tmp_path / "old")  # second open must not fail
    ToolAuditor(repository, clock=lambda: NOW).record(**_record_kwargs())

    rows = repository.audit_events()
    assert [row["operation"] for row in rows] == ["tool_call", "approve"]


def test_fresh_workspace_has_new_columns(tmp_path: Path) -> None:
    repository = WorkspaceRepository(tmp_path / "fresh")

    assert NEW_COLUMNS <= _columns(repository.path)
