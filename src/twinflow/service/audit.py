"""COMP-122 tool audit: one ``audit_events`` row and one ``twinflow.audit`` log record per call.

Only call facts are recorded: who, which tool, the idempotency key, a SHA-256 digest of
the validated input, the outcome and the duration. No argument value, token, path or
environment value is accepted, so none can be written (REQ-170, REQ-176). Every field is
validated before anything is written; a refusal writes nothing.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from twinflow.application.repository import WorkspaceRepository
from twinflow.service.auth import CLASSIFICATION_SCOPES

log = logging.getLogger("twinflow.audit")

_OUTCOME = re.compile(r"^(ok|denied|rate_limited|timeout|error:[a-z0-9_]{1,64})$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# Request keys are client text (1-200 characters, as the API admits); only control
# characters are refused, so a key cannot forge a log line.
_REQUEST_KEY = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_LOG_FIELDS = (
    "caller_id",
    "tool",
    "classification",
    "request_key",
    "input_digest",
    "outcome",
    "duration_ms",
    "correlation_id",
)


class ToolAuditor:
    """Writes the audit trail for tool calls into the workspace repository."""

    def __init__(
        self,
        repository: WorkspaceRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._clock = clock

    def record(
        self,
        *,
        caller_id: str,
        tool: str,
        classification: str,
        request_key: str | None,
        input_digest: str,
        outcome: str,
        duration_ms: float,
        correlation_id: str | None,
    ) -> None:
        """Validate the call facts, then write exactly one row and one log record."""
        _validate(
            caller_id=caller_id,
            tool=tool,
            classification=classification,
            request_key=request_key,
            input_digest=input_digest,
            outcome=outcome,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
        )
        self._repository.append_tool_audit(
            occurred_at=self._clock().isoformat(),
            caller_id=caller_id,
            tool=tool,
            request_key=request_key,
            input_digest=input_digest,
            outcome=outcome,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
        )
        log.info(
            "tool_call",
            extra={
                "caller_id": caller_id,
                "tool": tool,
                "classification": classification,
                "request_key": request_key,
                "input_digest": input_digest,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "correlation_id": correlation_id,
            },
        )


class AuditJsonFormatter(logging.Formatter):
    """One JSON object per ``twinflow.audit`` record, carrying only the audit fields."""

    def format(self, record: logging.LogRecord) -> str:
        """Render the record's audit fields and timestamp as one JSON line."""
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "event": record.getMessage(),
        }
        payload.update({name: getattr(record, name, None) for name in _LOG_FIELDS})
        return json.dumps(payload, sort_keys=True)


def _validate(
    *,
    caller_id: str,
    tool: str,
    classification: str,
    request_key: str | None,
    input_digest: str,
    outcome: str,
    duration_ms: float,
    correlation_id: str | None,
) -> None:
    for name, value in (("caller_id", caller_id), ("tool", tool)):
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"audit {name} is not a plain identifier")
    if request_key is not None and not _REQUEST_KEY.fullmatch(request_key):
        raise ValueError("audit request_key must be 1-200 characters without control characters")
    if correlation_id is not None and not _IDENTIFIER.fullmatch(correlation_id):
        raise ValueError("audit correlation_id is not a plain identifier")
    if classification not in CLASSIFICATION_SCOPES:
        raise ValueError("audit classification is unknown")
    if not _DIGEST.fullmatch(input_digest):
        raise ValueError("audit input_digest must be 64 lowercase hex characters")
    if not _OUTCOME.fullmatch(outcome):
        raise ValueError("audit outcome must be ok, denied, rate_limited, timeout or error:<code>")
    if not math.isfinite(duration_ms) or duration_ms < 0:
        raise ValueError("audit duration_ms must be a finite non-negative number")
