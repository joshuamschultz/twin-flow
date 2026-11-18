"""MCP over streamable HTTP inside the service app (COMP-119, D-116; REQ-161, REQ-173).

`mcp_endpoint` builds the tool catalogue's `MCPServer` and returns its one route, served at
exactly `/mcp` in the FastAPI app (no `/mcp/` redirect), plus the session manager the app's
lifespan runs. The transport is stateless with JSON responses: every POST carries its own
bearer token (checked by `ServiceBoundaryMiddleware`) and no session state is kept.

DNS-rebinding protection is on: the SDK refuses a Host outside `allowed_hosts` (each host is
also allowed with any port, `host:*`) with 421 and an Origin outside `allowed_origins` with
403, before any tool runs.

The `mcp` package is the optional `agent` extra; `service/app.py` imports this module
lazily and serves without `/mcp` when it is absent.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import BaseRoute

from twinflow.mcp.guard import ToolGuard
from twinflow.mcp.spec import ToolServices
from twinflow.mcp.tools import build_server

__all__ = ["MCP_PATH", "McpEndpoint", "mcp_endpoint"]

MCP_PATH = "/mcp"


@dataclass(frozen=True)
class McpEndpoint:
    """The routes to add to the app and the session manager its lifespan must run."""

    routes: tuple[BaseRoute, ...]
    run: Callable[[], AbstractAsyncContextManager[None]]


def mcp_endpoint(
    services: ToolServices,
    guard: ToolGuard,
    *,
    allowed_hosts: Iterable[str],
    allowed_origins: Iterable[str],
    max_request_bytes: int,
) -> McpEndpoint:
    """The stateless streamable HTTP endpoint for the whole catalogue, behind `guard`."""
    server = build_server(services, guard)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=max_request_bytes,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_with_any_port(allowed_hosts),
            allowed_origins=list(allowed_origins),
        ),
    )
    return McpEndpoint(routes=tuple(app.routes), run=server.session_manager.run)


def _with_any_port(hosts: Iterable[str]) -> list[str]:
    """Each host as given and in the SDK's any-port form (`host:*`)."""
    return [form for host in hosts for form in (host, f"{host}:*")]
