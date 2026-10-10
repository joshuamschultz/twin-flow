"""REST routes for the planning tools (COMP-123; REQ-180, REQ-185).

Each route under `/api/workspace` dispatches to the SAME `ToolSpec` handler through the
SAME `ToolGuard` as `/mcp`: scope, rate limit, input validation, timeout, result size and
exactly one audit row per call. The request body is the tool's own input model (ids
included) and the 200 response is its own output model, so OpenAPI and the MCP schemas
cannot drift. The guard's neutral result becomes an HTTP status here (`STATUS_BY_CODE`).

Two refusals happen before the guard, like the boundary middleware's: a path id that
differs from the body's id (400 `id_mismatch`, nothing runs) and a body that is not a JSON
object (422 `invalid_input`). A well-formed object that FastAPI rejects goes through the
guard anyway (`PlanningRoutes.refuse_invalid`), so it is scope checked and audited.

Ids in paths use the `path` converter: an id is an opaque string, and the SDK quotes it into
one segment, so an id with `/` in it still reaches the guard (and its audited `not_found`).

This module imports no `mcp` SDK module: REST works without the `agent` extra.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.types import DecoratedCallable
from pydantic import BaseModel
from starlette.responses import JSONResponse, Response

from twinflow.mcp import models as m
from twinflow.mcp.catalogue import SPECS
from twinflow.mcp.guard import ToolGuard, ToolResult, correlation_id_of
from twinflow.mcp.spec import ToolServices

__all__ = ["STATUS_BY_CODE", "PlanningRoutes"]

PREFIX = "/api/workspace"
_ROUTE_NAME_PREFIX = "planning."

# The guard's stable error codes as HTTP statuses. An unlisted code is a mapping bug: 500.
STATUS_BY_CODE: Mapping[str, int] = {
    "id_mismatch": 400,
    "unauthenticated": 401,
    "forbidden": 403,
    "not_found": 404,
    "idempotency_conflict": 409,
    "conflict": 409,
    "no_planning_profile": 409,
    "size_limit": 413,
    "invalid_input": 422,
    "blocked": 422,
    "mixed_problems": 422,
    "incomparable": 422,
    "simulation_link_pending": 422,
    "rate_limited": 429,
    "internal_error": 500,
    "artifact_signature_invalid": 500,
    "capability_unavailable": 503,
    "timeout": 504,
}


class PlanningRoutes:
    """The planning router plus the validation-error hook `create_app` installs."""

    def __init__(self, services: ToolServices, guard: ToolGuard) -> None:
        self._services = services
        self._guard = guard
        self.router = _build_router(self)

    async def call(
        self, tool: str, request: Request, arguments: dict[str, Any], **path_ids: str
    ) -> Response:
        """Run `tool` through the guard with the body and the path ids, as an HTTP response."""
        mismatch = _id_mismatch(arguments, path_ids)
        if mismatch is not None:
            return _refusal(
                request, 400, "id_mismatch", f"The path {mismatch} differs from the body"
            )
        result = await self._guard.call(
            SPECS[tool], self._services, {**arguments, **path_ids}, request
        )
        return _http_response(result)

    async def refuse_invalid(
        self, request: Request, exc: RequestValidationError
    ) -> Response | None:
        """The guard's answer for a planning route FastAPI refused; None for other routes.

        The raw path, query and body go to the guard, which scope checks, validates (field
        locations only, never values) and audits the call like any other.
        """
        tool = _tool_of(request)
        if tool is None:
            return None
        body = exc.body if exc.body is not None else {}
        if not isinstance(body, dict):
            return _refusal(request, 422, "invalid_input", "The request body must be a JSON object")
        arguments = {**request.query_params, **body}
        return await self.call(tool, request, arguments, **request.path_params)


def _build_router(routes: PlanningRoutes) -> APIRouter:
    """One route per planning tool; each body and response is the tool's own model."""
    router = APIRouter(prefix=PREFIX, tags=["planning"])
    _add_erp_routes(router, routes)
    _add_plan_routes(router, routes)
    _add_artifact_routes(router, routes)
    return router


def _route(
    router: APIRouter, method: str, path: str, tool: str
) -> Callable[[DecoratedCallable], DecoratedCallable]:
    """The decorator for one tool's route: its output model, a name naming the tool."""
    return router.api_route(
        path,
        methods=[method],
        response_model=SPECS[tool].output_model,
        name=_ROUTE_NAME_PREFIX + tool,
        summary=tool,
    )


def _add_erp_routes(router: APIRouter, routes: PlanningRoutes) -> None:
    @_route(router, "POST", "/erp/validate", "validate_snapshot")
    async def validate_snapshot(request: Request, body: m.SnapshotInput) -> Response:
        return await routes.call("validate_snapshot", request, _dump(body))

    @_route(router, "POST", "/erp/problems", "build_problem_from_erp")
    async def build_problem_from_erp(request: Request, body: m.SnapshotInput) -> Response:
        return await routes.call("build_problem_from_erp", request, _dump(body))


def _add_plan_routes(router: APIRouter, routes: PlanningRoutes) -> None:
    @_route(router, "POST", "/plans/portfolio", "solve_portfolio")
    async def solve_portfolio(request: Request, body: m.SolvePortfolioInput) -> Response:
        return await routes.call("solve_portfolio", request, _dump(body))

    @_route(router, "POST", "/plans/compare", "compare_plans")
    async def compare_plans(request: Request, body: m.ComparePlansInput) -> Response:
        return await routes.call("compare_plans", request, _dump(body))

    @_route(router, "POST", "/plans/{plan_id:path}/resolve", "resolve_with_overrides")
    async def resolve_with_overrides(
        request: Request, plan_id: str, body: m.ResolveInput
    ) -> Response:
        return await routes.call("resolve_with_overrides", request, _dump(body), plan_id=plan_id)

    @_route(router, "POST", "/plans/{plan_id:path}/rush", "insert_rush_order")
    async def insert_rush_order(
        request: Request, plan_id: str, body: m.InsertRushInput
    ) -> Response:
        return await routes.call("insert_rush_order", request, _dump(body), plan_id=plan_id)

    @_route(router, "POST", "/plans/{plan_id:path}/explain", "explain_plan")
    async def explain_plan(request: Request, plan_id: str, body: m.ExplainInput) -> Response:
        return await routes.call("explain_plan", request, _dump(body), plan_id=plan_id)

    @_route(router, "POST", "/plans/{plan_id:path}/simulate", "simulate_plan")
    async def simulate_plan(request: Request, plan_id: str, body: m.SimulateInput) -> Response:
        return await routes.call("simulate_plan", request, _dump(body), plan_id=plan_id)

    @_route(router, "GET", "/plans/{plan_id:path}", "get_plan")
    async def get_plan(
        request: Request,
        plan_id: str,
        offset: int = 0,
        limit: int = 100,
        job_id: str | None = None,
    ) -> Response:
        """The guard validates the bounds (`GetPlanInput`), so they are audited like MCP's."""
        arguments: dict[str, Any] = {"offset": offset, "limit": limit, "job_id": job_id}
        return await routes.call("get_plan", request, arguments, plan_id=plan_id)

    @_route(router, "GET", "/portfolios/{portfolio_id:path}/menu", "get_pareto_menu")
    async def get_pareto_menu(request: Request, portfolio_id: str) -> Response:
        return await routes.call("get_pareto_menu", request, {}, portfolio_id=portfolio_id)


def _add_artifact_routes(router: APIRouter, routes: PlanningRoutes) -> None:
    @_route(router, "GET", "/planning-profile", "get_planning_profile")
    async def get_planning_profile(request: Request) -> Response:
        return await routes.call("get_planning_profile", request, {})

    @_route(router, "PUT", "/planning-profile", "put_planning_profile")
    async def put_planning_profile(request: Request, body: m.PutPlanningProfileInput) -> Response:
        return await routes.call("put_planning_profile", request, _dump(body))

    @_route(router, "GET", "/k-table", "get_k_table")
    async def get_k_table(request: Request) -> Response:
        return await routes.call("get_k_table", request, {})

    @_route(router, "PUT", "/k-table", "put_k_table")
    async def put_k_table(request: Request, body: m.PutKTableInput) -> Response:
        return await routes.call("put_k_table", request, _dump(body))


# --- helpers -------------------------------------------------------------------------------------


def _dump(body: BaseModel) -> dict[str, Any]:
    """The body as the guard's raw arguments; the guard validates it again (one path)."""
    return body.model_dump(mode="json", exclude_unset=True)


def _id_mismatch(arguments: Mapping[str, Any], path_ids: Mapping[str, str]) -> str | None:
    """The first path id the body names with another value, else None."""
    for name, value in path_ids.items():
        if name in arguments and arguments[name] != value:
            return name
    return None


def _tool_of(request: Request) -> str | None:
    """The planning tool the matched route serves, or None for any other route."""
    route = request.scope.get("route")
    if not isinstance(route, APIRoute) or not route.name.startswith(_ROUTE_NAME_PREFIX):
        return None
    return route.name.removeprefix(_ROUTE_NAME_PREFIX)


def _http_response(result: ToolResult) -> Response:
    """The guard's body as JSON with the status its code maps to (`Retry-After` on 429)."""
    if not result.is_error:
        return Response(content=result.text, media_type="application/json")
    status = STATUS_BY_CODE.get(str(result.body.get("code")), 500)
    headers: dict[str, str] = {}
    retry_after = result.body.get("retry_after_seconds")
    if status == 429 and isinstance(retry_after, int | float):
        headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
    return Response(
        content=result.text, status_code=status, media_type="application/json", headers=headers
    )


def _refusal(request: Request, status: int, code: str, message: str) -> Response:
    """A refusal before the guard, in the guard's error shape."""
    body = {"code": code, "message": message, "correlation_id": correlation_id_of(request)}
    return JSONResponse(body, status_code=status)
