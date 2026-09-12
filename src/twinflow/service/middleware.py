"""Authentication, request bounds, correlation, and sanitized failures."""

from __future__ import annotations

import hmac
import logging
import re
import uuid

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)
_CORRELATION = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ServiceBoundaryMiddleware:
    """Pure ASGI middleware enforcing body limits and optional shared-token auth."""

    def __init__(self, app: ASGIApp, *, api_token: str | None, max_request_bytes: int) -> None:
        self.app = app
        self.api_token = api_token
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        correlation_id = _correlation_id(headers.get("x-correlation-id"))
        path = str(scope.get("path", ""))
        if self.api_token is not None and _protected(path):
            supplied = headers.get("authorization", "")
            expected = f"Bearer {self.api_token}"
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                await _response(
                    scope, 401, "Authentication required", correlation_id, receive, send
                )
                return
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_request_bytes:
                    await _response(
                        scope, 413, "Request body too large", correlation_id, receive, send
                    )
                    return
            except ValueError:
                await _response(scope, 400, "Invalid Content-Length", correlation_id, receive, send)
                return
        consumed = 0

        async def bounded_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_request_bytes:
                    raise _BodyTooLarge
            return message

        async def correlated_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-correlation-id", correlation_id.encode()))
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, bounded_receive, correlated_send)
        except _BodyTooLarge:
            await _response(scope, 413, "Request body too large", correlation_id, receive, send)
        except Exception:
            log.exception("Unhandled service error correlation_id=%s", correlation_id)
            await _response(scope, 500, "Internal service error", correlation_id, receive, send)


class _BodyTooLarge(Exception):
    pass


def _protected(path: str) -> bool:
    return (
        path.startswith("/api")
        or path == "/openapi.json"
        or path.startswith("/docs")
        or path.startswith("/redoc")
    )


def _correlation_id(value: str | None) -> str:
    return value if value is not None and _CORRELATION.fullmatch(value) else uuid.uuid4().hex


async def _response(
    scope: Scope,
    status: int,
    detail: str,
    correlation_id: str,
    receive: Receive,
    send: Send,
) -> None:
    response = JSONResponse(
        {"detail": detail, "correlation_id": correlation_id},
        status_code=status,
        headers={"X-Correlation-ID": correlation_id, "WWW-Authenticate": "Bearer"}
        if status == 401
        else {"X-Correlation-ID": correlation_id},
    )
    await response(scope, receive, send)
