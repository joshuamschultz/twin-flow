"""Caller authentication, request bounds, correlation, and sanitized failures (COMP-120).

Every HTTP path except the ``/health`` and ``/ready`` probes is protected, which covers
``/api``, ``/mcp``, ``/openapi.json``, ``/docs`` and ``/redoc`` and fails closed for any
route added later. The correlation id is put in ``scope["state"]["correlation_id"]`` and
echoed as the ``X-Correlation-ID`` response header. Refusals are small JSON bodies
``{code, detail, correlation_id}``; they never carry a token, a hash, a path or a traceback.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qsl

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from twinflow.service.auth import LOCAL_CALLER, Caller, TokenRegistry

log = logging.getLogger(__name__)
_CORRELATION = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
PUBLIC_PATHS = frozenset({"/health", "/ready"})
_TOKEN_QUERY_PARAMETERS = frozenset({"access_token", "token"})
SCHEDULE_PATH = "/api/workspace/schedules"
# REQ-084 admits 1,000,000 calendar intervals, about 30 MB of JSON; 64 MiB leaves headroom.
SCHEDULE_BODY_LIMIT_BYTES = 64 * 1024 * 1024
_MCP_PATH = "/mcp"
_ERP_PREFIX = "/api/workspace/erp/"


class _Refusal(Exception):  # noqa: N818 - internal control flow, never escapes the middleware
    """A request refused before it reaches the application."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail


class ServiceBoundaryMiddleware:
    """Pure ASGI middleware: authenticate the caller, bound the body, tag the correlation id.

    With a ``registry`` every protected request needs ``Authorization: Bearer <token>``
    naming a registered, unexpired token; the caller is placed in
    ``scope["state"]["caller"]``. Without one (open local mode, loopback only) every
    request runs as the ``local`` caller.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        registry: TokenRegistry | None,
        max_request_bytes: int,
        max_mcp_request_bytes: int,
    ) -> None:
        self.app = app
        self.registry = registry
        self.max_request_bytes = max_request_bytes
        self.max_mcp_request_bytes = max_mcp_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # Only HTTP is authenticated; any other scope (websocket) fails closed.
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = Headers(scope=scope)
        correlation_id = _correlation_id(headers.get("x-correlation-id"))
        # The tool guard reads it here, so error bodies match the response header.
        scope.setdefault("state", {})["correlation_id"] = correlation_id
        path = str(scope.get("path", ""))
        try:
            if path not in PUBLIC_PATHS:
                scope.setdefault("state", {})["caller"] = self._caller(scope, headers)
            limit = self._body_limit(path)
            _check_declared_length(headers, limit)
            request_messages = await _buffer_body(receive, limit)
        except _Refusal as refusal:
            await _refuse(scope, refusal, correlation_id, receive, send)
            return
        await self._forward(scope, request_messages, correlation_id, receive, send)

    def _caller(self, scope: Scope, headers: Headers) -> Caller:
        if self.registry is None:
            return LOCAL_CALLER
        query = bytes(scope.get("query_string", b"")).decode("latin-1")
        parameters = parse_qsl(query, keep_blank_values=True)
        if any(name.lower() in _TOKEN_QUERY_PARAMETERS for name, _ in parameters):
            raise _Refusal(400, "token_in_query", "Send the token in the Authorization header")
        authorization = headers.get("authorization", "")
        if _CONTROL_CHARACTERS.search(authorization):
            raise _Refusal(400, "malformed_token", "Malformed Authorization header")
        scheme, _, token = authorization.partition(" ")
        caller = None
        if scheme.lower() == "bearer" and token:
            caller = self.registry.authenticate(token, now=datetime.now(UTC))
        if caller is None:
            raise _Refusal(401, "unauthenticated", "Authentication required")
        return caller

    def _body_limit(self, path: str) -> int:
        if path == SCHEDULE_PATH:
            return SCHEDULE_BODY_LIMIT_BYTES
        if path == _MCP_PATH or path.startswith(_MCP_PATH + "/") or path.startswith(_ERP_PREFIX):
            return self.max_mcp_request_bytes
        return self.max_request_bytes

    async def _forward(
        self,
        scope: Scope,
        request_messages: list[Message],
        correlation_id: str,
        receive: Receive,
        send: Send,
    ) -> None:
        replay = iter(request_messages)

        async def replay_receive() -> Message:
            return next(replay, {"type": "http.disconnect"})

        response_started = False

        async def correlated_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-correlation-id", correlation_id.encode()))
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, replay_receive, correlated_send)
        except Exception:
            log.exception("Unhandled service error correlation_id=%s", correlation_id)
            if response_started:
                raise
            refusal = _Refusal(500, "internal_error", "Internal service error")
            await _refuse(scope, refusal, correlation_id, receive, send)


def _check_declared_length(headers: Headers, limit: int) -> None:
    content_length = headers.get("content-length")
    if content_length is None:
        return
    try:
        declared_length = int(content_length)
    except ValueError:
        declared_length = -1
    if declared_length < 0:
        raise _Refusal(400, "invalid_content_length", "Invalid Content-Length")
    if declared_length > limit:
        raise _Refusal(413, "size_limit", "Request body too large")


async def _buffer_body(receive: Receive, limit: int) -> list[Message]:
    """Read the whole body up front so a chunked upload cannot pass the limit."""
    request_messages: list[Message] = []
    consumed = 0
    while True:
        message = await receive()
        request_messages.append(message)
        if message["type"] != "http.request":
            return request_messages
        consumed += len(message.get("body", b""))
        if consumed > limit:
            raise _Refusal(413, "size_limit", "Request body too large")
        if not message.get("more_body", False):
            return request_messages


def _correlation_id(value: str | None) -> str:
    return value if value is not None and _CORRELATION.fullmatch(value) else uuid.uuid4().hex


async def _refuse(
    scope: Scope, refusal: _Refusal, correlation_id: str, receive: Receive, send: Send
) -> None:
    headers = {"X-Correlation-ID": correlation_id}
    if refusal.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    response = JSONResponse(
        {"code": refusal.code, "detail": refusal.detail, "correlation_id": correlation_id},
        status_code=refusal.status,
        headers=headers,
    )
    await response(scope, receive, send)
