"""FastAPI application — the local REST surface the React front end drives.

Endpoints group into four concerns: discovery (`/api/modules`, `/api/models`),
the visual model (`/api/models/{name}/floor`, `/levers`), work (`/api/run`,
`/api/sweep`, `/api/optimize` — each returns a job id), and polling (`/api/jobs`).
Heavy work runs on the `JobStore`'s background threads; the request returns
immediately with an id the UI polls.

`model` in every request names a discovered example under the models root, never a
free path, so the API cannot be steered into reading files outside it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime
from functools import partial
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, Response

from twinflow import __version__
from twinflow.application.artifacts import SignedArtifactStore
from twinflow.application.decision_tools import DecisionTools
from twinflow.application.planning import PlanningService
from twinflow.application.portfolio_port import PortfolioApi, load_portfolio_api
from twinflow.application.workspace import Workspace
from twinflow.mcp.guard import ToolGuard
from twinflow.mcp.spec import ToolServices
from twinflow.model import load_model
from twinflow.modules import (
    COSTS,
    MODELS,
    OBJECTIVES,
    OPTIMIZERS,
    Choice,
    IntRange,
    LeverSpace,
    Scenario,
    ScoringSurface,
    optimize,
)
from twinflow.modules.space import Domain
from twinflow.plan.loader import load_plan
from twinflow.service.actions import action_router
from twinflow.service.audit import ToolAuditor
from twinflow.service.auth import TokenRegistry
from twinflow.service.decisions import decision_router
from twinflow.service.floor import build_floor, suggest_levers
from twinflow.service.jobs import JobStore
from twinflow.service.limits import CallerLimiter
from twinflow.service.middleware import ServiceBoundaryMiddleware
from twinflow.service.planning import PlanningRoutes
from twinflow.service.schemas import OptimizeRequest, RunRequest, SweepRequest
from twinflow.service.serialize import (
    intervals_to_dict,
    kpis_to_dict,
    optimization_to_dict,
)
from twinflow.service.settings import ServiceSettings
from twinflow.service.signing import ServiceSigner
from twinflow.service.workspace import workspace_router

if TYPE_CHECKING:
    from twinflow.mcp.http import McpEndpoint

log = logging.getLogger(__name__)

# The per-caller call burst over `calls_per_minute` (COMP-121).
CALL_BURST = 20


class ModelCatalog:
    """The set of runnable example floors under `root` (each a `model.yaml` +
    sibling `plan.csv`). The only place a `model` name is turned into a path."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def list(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for model_file in sorted(self._root.glob("*/model.yaml")):
            name = model_file.parent.name
            entries.append({"name": name, "has_plan": (model_file.parent / "plan.csv").is_file()})
        return entries

    def resolve(self, name: str) -> tuple[str, str]:
        """(`model_path`, `plan_path`) for example `name`; 404 if either is missing.
        `name` is a single path segment — never a nested or absolute path."""
        if "/" in name or name in (".", "..") or "\\" in name:
            raise HTTPException(status_code=400, detail=f"invalid model name {name!r}")
        model_path = self._root / name / "model.yaml"
        plan_path = self._root / name / "plan.csv"
        if not model_path.is_file():
            raise HTTPException(status_code=404, detail=f"no model {name!r}")
        if not plan_path.is_file():
            raise HTTPException(status_code=404, detail=f"model {name!r} has no plan.csv")
        return str(model_path.resolve()), str(plan_path.resolve())


def _validation_refusal(planning: PlanningRoutes) -> Any:
    """The app's request-validation handler; planning routes answer through the tool guard."""

    async def refuse(request: Request, exc: RequestValidationError) -> Response:
        """422 listing where and why, never the rejected values (NaN or a 5,000-deep list
        in `input` breaks JSON encoding, and echoing client data back is a leak)."""
        guarded = await planning.refuse_invalid(request, exc)
        if guarded is not None:
            return guarded
        detail = [
            {"type": item["type"], "loc": list(item["loc"]), "msg": item["msg"]}
            for item in exc.errors()
        ]
        return JSONResponse({"detail": detail}, status_code=422)

    return refuse


def create_app(
    models_root: str | Path = "examples",
    *,
    workspace_root: str | Path = ".twinflow-workspace",
    settings: ServiceSettings | None = None,
    portfolio_api: PortfolioApi | None = None,
) -> FastAPI:
    """Build the app rooted at `models_root`: REST under `/api`, MCP at `/mcp`.

    `portfolio_api` injects TF-003's portfolio functions; None loads them lazily
    (`load_portfolio_api`), and planning solves report `capability_unavailable` when TF-003
    is not installed. `/mcp` is served only when the `agent` extra is installed; either way
    `/api/health` says which (`mcp: "available" | "unavailable"`).
    """
    configured = settings or ServiceSettings()
    # Loaded once at startup; a bad registry refuses to start. When given it is enforced
    # on loopback too.
    registry = TokenRegistry.load(configured.tokens_file) if configured.tokens_file else None
    workspace = _open_workspace(workspace_root, models_root, configured)
    try:
        services = _tool_services(workspace, models_root, configured, registry, portfolio_api)
    except Exception:
        workspace.close()  # a refused signing key must not keep the owner lock
        raise
    # One guard (and so one rate limiter per caller) for every tool surface.
    guard = _tool_guard(workspace, configured)
    mcp = _mcp_endpoint(services, guard, configured)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with mcp.run() if mcp is not None else nullcontext():
                yield
        finally:
            services.planning.close()
            workspace.close()

    app = FastAPI(title="twinflow", version=__version__, lifespan=lifespan)
    planning = PlanningRoutes(services, guard)  # the same specs and guard as /mcp
    app.add_exception_handler(RequestValidationError, _validation_refusal(planning))
    app.include_router(planning.router)
    app.include_router(workspace_router(workspace))
    app.include_router(decision_router(workspace))
    app.include_router(action_router(workspace))
    app.include_router(_health_router(workspace, mcp_available=mcp is not None))
    catalog = ModelCatalog(models_root)
    app.include_router(_discovery_router(catalog))
    jobs = JobStore()
    app.include_router(_engine_router(catalog, jobs, local_only=configured.local_only))
    app.include_router(_jobs_router(jobs))
    if mcp is not None:
        app.router.routes.extend(mcp.routes)
    _add_boundary_middleware(app, configured, registry)
    return app


def _open_workspace(
    workspace_root: str | Path, models_root: str | Path, configured: ServiceSettings
) -> Workspace:
    return Workspace(
        workspace_root,
        models_root,
        max_active_jobs=configured.max_active_jobs,
        max_replications=configured.max_replications,
        max_records_per_kind=configured.max_records_per_kind,
        max_evidence_rows=configured.max_evidence_rows,
        max_content_bytes=configured.max_request_bytes,
        deployment_mode="local" if configured.local_only else "dedicated",
    )


def _tool_services(
    workspace: Workspace,
    models_root: str | Path,
    configured: ServiceSettings,
    registry: TokenRegistry | None,
    portfolio_api: PortfolioApi | None,
) -> ToolServices:
    """The in-process use cases behind the MCP tools and the REST planning routes.

    Without a signing key there is no artifact store: outside open local mode planning then
    fails closed (`no_planning_profile`). `twinflow serve` refuses a non-loopback bind
    without a key before it gets here (`ServiceSigner.require`).
    """
    artifacts = None
    if configured.signing_key_file is not None:
        signer = ServiceSigner.load(
            configured.signing_key_file, workspace_root=workspace.repository.root
        )
        artifacts = SignedArtifactStore(
            workspace.repository, signer, clock=lambda: datetime.now(UTC)
        )
    planning = PlanningService(
        workspace,
        portfolio=portfolio_api or load_portfolio_api(),
        max_solve_seconds=configured.max_solve_seconds,
    )
    return ToolServices(
        workspace=workspace,
        decisions=DecisionTools(workspace),
        planning=planning,
        artifacts=artifacts,
        models_root=Path(models_root),
        # Open local mode: loopback with no token registry (COMP-120, COMP-127).
        local_mode=configured.local_only and registry is None,
    )


def _tool_guard(workspace: Workspace, configured: ServiceSettings) -> ToolGuard:
    """The guard for authenticated HTTP callers; the caller comes from the middleware."""
    limiter = CallerLimiter(
        calls_per_minute=configured.calls_per_minute,
        burst=CALL_BURST,
        max_concurrent_solves_per_caller=configured.max_concurrent_solves_per_caller,
    )
    return ToolGuard(
        auditor=ToolAuditor(workspace.repository),
        limiter=limiter,
        max_result_bytes=configured.max_tool_result_bytes,
    )


def _mcp_endpoint(
    services: ToolServices, guard: ToolGuard, configured: ServiceSettings
) -> McpEndpoint | None:
    """The `/mcp` endpoint, or None when the optional `agent` extra is not installed."""
    try:
        from twinflow.mcp.http import mcp_endpoint
    except ModuleNotFoundError as exc:
        if exc.name is None or exc.name.split(".")[0] != "mcp":
            raise
        log.info("the mcp package is not installed; /mcp is not served")
        return None
    return mcp_endpoint(
        services,
        guard,
        allowed_hosts=configured.allowed_hosts,
        allowed_origins=configured.mcp_allowed_origins,
        max_request_bytes=configured.max_mcp_request_bytes,
    )


def _add_boundary_middleware(
    app: FastAPI, configured: ServiceSettings, registry: TokenRegistry | None
) -> None:
    """Outermost first on the wire: CORS, trusted Host, then caller auth and body bounds."""
    app.add_middleware(
        ServiceBoundaryMiddleware,
        registry=registry,
        max_request_bytes=configured.max_request_bytes,
        max_mcp_request_bytes=configured.max_mcp_request_bytes,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(configured.allowed_hosts))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.cors_origins),
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
    )


def _health_router(workspace: Workspace, *, mcp_available: bool) -> APIRouter:
    router = APIRouter()
    mcp_status = "available" if mcp_available else "unavailable"

    @router.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mcp": mcp_status}

    @router.get("/health", include_in_schema=False)
    def public_health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/ready", include_in_schema=False)
    def readiness() -> dict[str, str]:
        workspace.repository.list("jobs", 1)
        return {"status": "ready"}

    return router


def _discovery_router(catalog: ModelCatalog) -> APIRouter:
    """Registered modules, adapters and example floors, read-only."""
    router = APIRouter()

    @router.get("/api/modules")
    def modules() -> dict[str, list[str]]:
        """Discovery: every registered objective, cost, optimizer, and model."""
        return {
            "objectives": OBJECTIVES.names(),
            "costs": COSTS.names(),
            "optimizers": OPTIMIZERS.names(),
            "models": MODELS.names(),
        }

    @router.get("/api/adapters")
    def adapters() -> dict[str, list[str]]:
        """Discovery: every registered integration surface (connectors, demand
        generators, forecasters) plus the dispatch rule names the engine accepts.
        Concrete ERP/forecaster integrations register into these same lists."""
        from twinflow.adapters.connectors import ACTUAL_SOURCES, PLAN_SOURCES, RESULT_SINKS
        from twinflow.adapters.demand import DEMAND_GENERATORS
        from twinflow.adapters.forecast import FORECASTERS
        from twinflow.policy import DISPATCH_NAMES

        return {
            "plan_sources": PLAN_SOURCES.names(),
            "result_sinks": RESULT_SINKS.names(),
            "actual_sources": ACTUAL_SOURCES.names(),
            "demand_generators": DEMAND_GENERATORS.names(),
            "forecasters": FORECASTERS.names(),
            "dispatch_policies": list(DISPATCH_NAMES),
        }

    @router.get("/api/models")
    def models() -> list[dict[str, Any]]:
        return catalog.list()

    @router.get("/api/models/{name}/floor")
    def floor(name: str) -> dict[str, Any]:
        model_path, _plan = catalog.resolve(name)
        return build_floor(load_model(model_path))

    @router.get("/api/models/{name}/levers")
    def levers(name: str) -> list[dict[str, Any]]:
        model_path, _plan = catalog.resolve(name)
        return suggest_levers(load_model(model_path))

    return router


def _engine_router(catalog: ModelCatalog, jobs: JobStore, *, local_only: bool) -> APIRouter:
    """Local-only run, sweep and optimize jobs on the example floors."""
    router = APIRouter()

    def require_local(remote_alternative: str) -> None:
        if not local_only:
            raise HTTPException(
                403, f"Use the bounded workspace {remote_alternative} API in remote deployments"
            )

    @router.post("/api/run")
    def run(request: RunRequest) -> dict[str, Any]:
        require_local("experiment")
        model_path, plan_path = catalog.resolve(request.model)
        return jobs.submit("run", partial(_run_work, model_path, plan_path, request.reps)).to_dict()

    @router.post("/api/sweep")
    def sweep(request: SweepRequest) -> dict[str, Any]:
        require_local("experiment")
        model_path, plan_path = catalog.resolve(request.model)
        return jobs.submit("sweep", partial(_sweep_work, model_path, plan_path, request)).to_dict()

    @router.post("/api/optimize")
    def optimize_endpoint(request: OptimizeRequest) -> dict[str, Any]:
        require_local("scheduling")
        model_path, plan_path = catalog.resolve(request.model)
        space = _build_space(request)
        if request.objective not in OBJECTIVES:
            raise HTTPException(status_code=400, detail=f"unknown objective {request.objective!r}")
        if request.optimizer not in OPTIMIZERS:
            raise HTTPException(status_code=400, detail=f"unknown optimizer {request.optimizer!r}")
        work = partial(_optimize_work, model_path, plan_path, space, request)
        return jobs.submit("optimize", work).to_dict()

    return router


def _jobs_router(jobs: JobStore) -> APIRouter:
    """Polling for the engine jobs."""
    router = APIRouter()

    @router.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [job.to_dict() for job in jobs.list()]

    @router.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        return job.to_dict()

    return router


def _run_work(model_path: str, plan_path: str, reps: int) -> dict[str, Any]:
    from dataclasses import asdict

    from twinflow.adapters.fulfillment import compute_fulfillment

    compiled = load_model(model_path)
    plan = load_plan(plan_path, compiled.registry)
    surface = ScoringSurface(model_path, plan, reps=reps)
    evaluation = surface.evaluate(Scenario(levers={}))
    return {
        "kpis": kpis_to_dict(evaluation.kpis),
        "intervals": intervals_to_dict(evaluation.intervals),
        "fulfillment": asdict(compute_fulfillment(evaluation.kpis)),
        "inventory": asdict(evaluation.inventory),
    }


def _sweep_work(model_path: str, plan_path: str, request: SweepRequest) -> dict[str, Any]:
    compiled = load_model(model_path)
    plan = load_plan(plan_path, compiled.registry)
    surface = ScoringSurface(model_path, plan, reps=request.reps)
    keys = list(request.sweep)
    points: list[dict[str, Any]] = []
    for combo in product(*(request.sweep[key] for key in keys)):
        levers = dict(zip(keys, combo, strict=True))
        evaluation = surface.evaluate(Scenario(levers=levers))
        points.append(
            {
                "levers": levers,
                "kpis": kpis_to_dict(evaluation.kpis),
                "intervals": intervals_to_dict(evaluation.intervals),
            }
        )
    return {"points": points}


def _optimize_work(
    model_path: str, plan_path: str, space: LeverSpace, request: OptimizeRequest
) -> dict[str, Any]:
    compiled = load_model(model_path)
    plan = load_plan(plan_path, compiled.registry)
    result = optimize(
        model_path,
        plan,
        space,
        OBJECTIVES.create(request.objective),
        OPTIMIZERS.create(request.optimizer),
        budget=request.budget,
        reps=request.reps,
        seed=request.seed,
    )
    return optimization_to_dict(result)


def _build_space(request: OptimizeRequest) -> LeverSpace:
    """Turn an `OptimizeRequest.space` into a `LeverSpace` (400 on a malformed
    domain — neither a `{min,max}` range nor a `{choices}` set)."""
    domains: dict[str, Domain] = {}
    for path, spec in request.space.items():
        if spec.choices is not None:
            domains[path] = Choice(tuple(spec.choices))
        elif spec.min is not None and spec.max is not None:
            domains[path] = IntRange(spec.min, spec.max, spec.step)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"lever {path!r} needs either min+max or choices",
            )
    if not domains:
        raise HTTPException(status_code=400, detail="optimize needs at least one lever")
    return LeverSpace(domains)
