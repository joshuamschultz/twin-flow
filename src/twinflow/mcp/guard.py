"""The tool guard (COMP-118 with COMP-120/121/122): every tool call runs through `ToolGuard.call`.

Order, fixed: resolve the caller, scope check, rate limit, input validation and digest,
timeout around the handler (the handler runs on a worker thread), result size, audit.
Every call writes exactly one audit record, whatever its outcome (`ok`, `error:<code>`,
`denied`, `rate_limited`, `timeout`).

The result is transport-neutral (`ToolResult`: the JSON body, its text, an error flag), so
this module imports no `mcp` SDK module and the same pipeline serves REST; `tools.py` turns
it into an MCP `CallToolResult`. An error body is the JSON object
`{code, message, correlation_id, ...}`. Messages are authored text or field
locations only: never an exception's own text from an unexpected failure, a traceback, an
exception class name, a path or an input value (REQ-170). Unexpected failures are logged on
`twinflow.mcp` with the exception class name and the correlation id only, never `exc_info`.

Caller: over HTTP, `ServiceBoundaryMiddleware` puts the authenticated `Caller` in
`request.scope["state"]["caller"]` (and the guard reads a `correlation_id` there when one
is set). Without an HTTP request (in-memory or stdio) the guard's fixed local caller is used
(`ToolGuard.local`). An HTTP request without a caller is refused (fail closed).
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any

import anyio
import anyio.to_thread
from pydantic import BaseModel, ValidationError

from twinflow.application.artifacts import ArtifactSignatureInvalid
from twinflow.application.decision_tools import ScheduleRequestError
from twinflow.application.planning import Blocked, PlanningError
from twinflow.application.repository import WorkspaceRepository
from twinflow.application.solver_port import UnsupportedSolver
from twinflow.data.errors import DataError, EventConflictError
from twinflow.erp.profile import ProfileError
from twinflow.mcp.spec import ToolServices, ToolSpec
from twinflow.scheduling import OptionalDependencyError
from twinflow.service.audit import ToolAuditor
from twinflow.service.auth import SCOPES, Caller
from twinflow.service.limits import (
    DEFAULT_RESULT_LIMIT_BYTES,
    CallerLimiter,
    RateLimited,
    ResultTooLarge,
    ensure_result_size,
)

log = logging.getLogger("twinflow.mcp")

LOCAL_CALLER_ID = "local"
ANONYMOUS_CALLER_ID = "anonymous"
_CORRELATION = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_PLAIN_OUTCOMES = frozenset({"rate_limited", "timeout"})


@dataclass(frozen=True)
class ToolResult:
    """One guarded call's outcome: the JSON body, the same body as text, and the error flag."""

    body: dict[str, Any]
    text: str
    is_error: bool = False


class ToolFailure(Exception):
    """A refused or failed call, already mapped to a stable code and a safe message."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        outcome: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.outcome = outcome or (code if code in _PLAIN_OUTCOMES else f"error:{code}")
        self.details = dict(details or {})


@dataclass
class _CallRecord:
    """What the audit row needs; filled in as the call progresses."""

    caller_id: str = ANONYMOUS_CALLER_ID
    input_digest: str = ""
    request_key: str | None = None
    outcome: str = "ok"
    started: float = field(default_factory=time.monotonic)


class ToolGuard:
    """Scope, limits, timeout, size and audit around every tool handler.

    `limiter` None skips the call-rate and solve-slot checks (local embedding only).
    """

    def __init__(
        self,
        *,
        auditor: ToolAuditor,
        limiter: CallerLimiter | None,
        local_caller: Caller | None = None,
        max_result_bytes: int = DEFAULT_RESULT_LIMIT_BYTES,
    ) -> None:
        self._auditor = auditor
        self._limiter = limiter
        self._local_caller = local_caller
        self._max_result_bytes = max_result_bytes

    @classmethod
    def local(
        cls,
        repository: WorkspaceRepository,
        *,
        caller_id: str = LOCAL_CALLER_ID,
        limiter: CallerLimiter | None = None,
        max_result_bytes: int = DEFAULT_RESULT_LIMIT_BYTES,
    ) -> ToolGuard:
        """A guard for in-memory or stdio use: one fixed caller with every scope.

        The local OS user is the boundary there (COMP-119); audit always applies. Without a
        `limiter` the host process is not rate limited against itself (in-process
        embedding); stdio passes the settings' `CallerLimiter` so its limits apply.
        """
        return cls(
            auditor=ToolAuditor(repository),
            limiter=limiter,
            local_caller=Caller(caller_id=caller_id, scopes=SCOPES),
            max_result_bytes=max_result_bytes,
        )

    async def call(
        self,
        spec: ToolSpec,
        services: ToolServices,
        arguments: Mapping[str, Any],
        request: object | None,
    ) -> ToolResult:
        """Run one tool call through every guard step and audit it exactly once."""
        correlation_id = correlation_id_of(request)
        record = _CallRecord(input_digest=_raw_digest(arguments))
        try:
            caller = self._caller(request)
            record.caller_id = caller.caller_id
            payload = await self._guarded(spec, services, caller, arguments, record)
            result = _success(payload)
        except ToolFailure as failure:
            record.outcome = failure.outcome
            result = _failure(failure, correlation_id)
        return self._audited(spec, record, correlation_id, result)

    async def _guarded(
        self,
        spec: ToolSpec,
        services: ToolServices,
        caller: Caller,
        arguments: Mapping[str, Any],
        record: _CallRecord,
    ) -> bytes:
        if not caller.allows(spec.scope):
            raise ToolFailure(
                "forbidden", f"This caller lacks the {spec.scope!r} scope", outcome="denied"
            )
        if self._limiter is not None:
            with _rate_limit_mapped():
                self._limiter.check_call(caller.caller_id)
        validated = _validate(spec, arguments)
        record.input_digest = _digest(validated.model_dump(mode="json"))
        record.request_key = spec.request_key(validated)
        with _rate_limit_mapped(), self._solve_slot(spec, caller):
            output = await _run_with_timeout(spec, services, caller, validated)
        try:
            return ensure_result_size(output, self._max_result_bytes)
        except ResultTooLarge:
            raise ToolFailure(
                "size_limit", "The result is larger than the service returns"
            ) from None

    def _caller(self, request: object | None) -> Caller:
        if request is None:
            if self._local_caller is None:
                raise ToolFailure("unauthenticated", "Authentication required", outcome="denied")
            return self._local_caller
        caller = _request_state(request).get("caller")
        if not isinstance(caller, Caller):
            raise ToolFailure("unauthenticated", "Authentication required", outcome="denied")
        return caller

    def _solve_slot(self, spec: ToolSpec, caller: Caller) -> AbstractContextManager[None]:
        if spec.solve_slot and self._limiter is not None:
            return self._limiter.solve_slot(caller.caller_id)
        return nullcontext()

    def _audited(
        self, spec: ToolSpec, record: _CallRecord, correlation_id: str, result: ToolResult
    ) -> ToolResult:
        """Write the one audit record; a result that cannot be audited is not returned."""
        try:
            self._auditor.record(
                caller_id=record.caller_id,
                tool=spec.name,
                classification=spec.classification,
                request_key=record.request_key,
                input_digest=record.input_digest,
                outcome=record.outcome,
                duration_ms=(time.monotonic() - record.started) * 1000.0,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # audit failure fails the call closed
            log.error(
                "tool audit failed: tool=%s error=%s correlation_id=%s",
                spec.name,
                type(exc).__name__,
                correlation_id,
            )
            failure = ToolFailure("internal_error", "Internal service error")
            return _failure(failure, correlation_id)
        return result


# --- steps ---------------------------------------------------------------------------------------


def _validate(spec: ToolSpec, arguments: Mapping[str, Any]) -> BaseModel:
    try:
        return spec.input_model.model_validate(dict(arguments))
    except ValidationError as exc:
        raise _invalid_fields(exc) from None


async def _run_with_timeout(
    spec: ToolSpec, services: ToolServices, caller: Caller, validated: BaseModel
) -> BaseModel:
    """Run the sync handler on a worker thread; give up waiting after the tool's timeout.

    A thread cannot be killed: after a timeout the handler finishes in the background and
    its result is discarded. Solve-type work has its own hard limit in the planning service.
    """
    call = functools.partial(spec.handler, services, caller, validated)
    try:
        with anyio.fail_after(spec.timeout_for(services)):
            return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
    except TimeoutError:
        raise ToolFailure("timeout", "The tool did not finish within its time limit") from None
    except ToolFailure:
        raise
    except Exception as exc:  # every failure is mapped; nothing leaks
        raise map_exception(exc, tool=spec.name) from None


@contextmanager
def _rate_limit_mapped() -> Iterator[None]:
    try:
        yield
    except RateLimited as exc:
        raise ToolFailure(
            "rate_limited",
            "Too many calls; retry later",
            details={"retry_after_seconds": exc.retry_after_seconds},
        ) from None


# --- error mapping -------------------------------------------------------------------------------


def _planning(exc: PlanningError) -> ToolFailure:
    details: dict[str, Any] = {}
    if isinstance(exc, Blocked):
        details["issues"] = [issue.model_dump(mode="json") for issue in exc.issues]
    return ToolFailure(exc.code, str(exc), details=details)


def _schedule_request(exc: ScheduleRequestError) -> ToolFailure:
    issues = [{"path": issue.path, "message": issue.message} for issue in exc.issues]
    return ToolFailure(
        "invalid_input", "The scheduling request is not valid", details={"issues": issues}
    )


def _typed_failure(exc: Exception) -> ToolFailure | None:
    """Failures whose payload comes from the exception; its text is authored to be safe."""
    if isinstance(exc, PlanningError):
        return _planning(exc)
    if isinstance(exc, ValidationError):
        return _invalid_fields(exc)
    if isinstance(exc, ScheduleRequestError):
        return _schedule_request(exc)
    if isinstance(exc, UnsupportedSolver):  # names the registered solvers, never the input
        return ToolFailure(exc.code, str(exc), details={"registered": list(exc.registered)})
    if isinstance(exc, ProfileError):  # names key paths only, by contract (COMP-113)
        return ToolFailure("invalid_input", str(exc))
    return None


_REJECTED = "The input was rejected; check it against the tool contract"
_NOT_FOUND = "A referenced record was not found"

# Checked in order; the first matching class wins. Messages are fixed here.
_FIXED_FAILURES: tuple[tuple[type[Exception], str, str], ...] = (
    (
        ArtifactSignatureInvalid,
        "artifact_signature_invalid",
        "A signed artifact failed verification",
    ),
    (ResultTooLarge, "size_limit", "The result is larger than the service returns"),
    (EventConflictError, "conflict", "An event conflicts with one already imported"),
    (OptionalDependencyError, "capability_unavailable", "An optional solver is not installed"),
    (LookupError, "not_found", _NOT_FOUND),
    (DataError, "invalid_input", _REJECTED),
    (ValueError, "invalid_input", _REJECTED),
    (TypeError, "invalid_input", _REJECTED),
)


def map_exception(exc: Exception, *, tool: str) -> ToolFailure:
    """The stable failure for `exc`; unknown failures become `internal_error` and are logged."""
    failure = _typed_failure(exc)
    if failure is not None:
        return failure
    for kind, code, message in _FIXED_FAILURES:
        if isinstance(exc, kind):
            return ToolFailure(code, message)
    log.error("tool %s failed unexpectedly: %s", tool, type(exc).__name__)
    return ToolFailure("internal_error", "Internal service error")


def _invalid_fields(exc: ValidationError) -> ToolFailure:
    """Field locations only, never the rejected values."""
    fields = sorted({".".join(str(part) for part in error["loc"]) or "$" for error in exc.errors()})
    return ToolFailure(
        "invalid_input", "Invalid input at: " + ", ".join(fields), details={"fields": fields}
    )


# --- results -------------------------------------------------------------------------------------


def _success(payload: bytes) -> ToolResult:
    text = payload.decode("utf-8")
    return ToolResult(body=json.loads(text), text=text)


def _failure(failure: ToolFailure, correlation_id: str) -> ToolResult:
    body: dict[str, Any] = {
        "code": failure.code,
        "message": failure.message,
        "correlation_id": correlation_id,
        **failure.details,
    }
    return ToolResult(body=body, text=json.dumps(body, sort_keys=True), is_error=True)


# --- request facts -------------------------------------------------------------------------------


def _request_state(request: object) -> Mapping[str, Any]:
    scope = getattr(request, "scope", None)
    state = scope.get("state") if isinstance(scope, Mapping) else None
    return state if isinstance(state, Mapping) else {}


def correlation_id_of(request: object | None) -> str:
    """The middleware's correlation id when it set one, else a fresh one."""
    value = _request_state(request).get("correlation_id") if request is not None else None
    return value if isinstance(value, str) and _CORRELATION.fullmatch(value) else uuid.uuid4().hex


def _digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _raw_digest(arguments: Mapping[str, Any]) -> str:
    """Digest of the raw arguments, for calls refused before validation."""
    try:
        return _digest(dict(arguments))
    except (TypeError, ValueError):
        return hashlib.sha256(b"").hexdigest()


__all__ = ["ToolFailure", "ToolGuard", "ToolResult", "correlation_id_of", "map_exception"]
