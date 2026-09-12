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

from itertools import product
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

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
from twinflow.service.floor import build_floor, suggest_levers
from twinflow.service.jobs import JobStore
from twinflow.service.schemas import OptimizeRequest, RunRequest, SweepRequest
from twinflow.service.serialize import (
    intervals_to_dict,
    kpis_to_dict,
    optimization_to_dict,
)


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


def create_app(
    models_root: str | Path = "examples", *, workspace_root: str | Path = ".twinflow-workspace"
) -> FastAPI:
    """Build the app rooted at `models_root`. A fresh `JobStore` lives for the
    app's lifetime."""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from twinflow.application.workspace import Workspace
    from twinflow.service.workspace import workspace_router

    workspace = Workspace(workspace_root, models_root)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        workspace.close()

    app = FastAPI(title="twinflow", version="0.1.0", lifespan=lifespan)
    app.include_router(workspace_router(workspace))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    catalog = ModelCatalog(models_root)
    jobs = JobStore()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/modules")
    def modules() -> dict[str, list[str]]:
        """Discovery: every registered objective, cost, optimizer, and model."""
        return {
            "objectives": OBJECTIVES.names(),
            "costs": COSTS.names(),
            "optimizers": OPTIMIZERS.names(),
            "models": MODELS.names(),
        }

    @app.get("/api/adapters")
    def adapters() -> dict[str, list[str]]:
        """Discovery: every registered integration surface (connectors, demand
        generators, forecasters, dispatch policies). Concrete ERP/RL/forecaster
        integrations register into these same lists."""
        from twinflow.adapters.connectors import ACTUAL_SOURCES, PLAN_SOURCES, RESULT_SINKS
        from twinflow.adapters.demand import DEMAND_GENERATORS
        from twinflow.adapters.dispatch import DISPATCH_POLICIES
        from twinflow.adapters.forecast import FORECASTERS

        return {
            "plan_sources": PLAN_SOURCES.names(),
            "result_sinks": RESULT_SINKS.names(),
            "actual_sources": ACTUAL_SOURCES.names(),
            "demand_generators": DEMAND_GENERATORS.names(),
            "forecasters": FORECASTERS.names(),
            "dispatch_policies": DISPATCH_POLICIES.names(),
        }

    @app.get("/api/models")
    def models() -> list[dict[str, Any]]:
        return catalog.list()

    @app.get("/api/models/{name}/floor")
    def floor(name: str) -> dict[str, Any]:
        model_path, _plan = catalog.resolve(name)
        return build_floor(load_model(model_path))

    @app.get("/api/models/{name}/levers")
    def levers(name: str) -> list[dict[str, Any]]:
        model_path, _plan = catalog.resolve(name)
        return suggest_levers(load_model(model_path))

    @app.post("/api/run")
    def run(request: RunRequest) -> dict[str, Any]:
        model_path, plan_path = catalog.resolve(request.model)

        def work() -> dict[str, Any]:
            from dataclasses import asdict

            from twinflow.adapters.fulfillment import compute_fulfillment

            compiled = load_model(model_path)
            plan = load_plan(plan_path, compiled.registry)
            surface = ScoringSurface(model_path, plan, reps=request.reps)
            evaluation = surface.evaluate(Scenario(levers={}))
            return {
                "kpis": kpis_to_dict(evaluation.kpis),
                "intervals": intervals_to_dict(evaluation.intervals),
                "fulfillment": asdict(compute_fulfillment(evaluation.kpis)),
                "inventory": asdict(evaluation.inventory),
            }

        return jobs.submit("run", work).to_dict()

    @app.post("/api/sweep")
    def sweep(request: SweepRequest) -> dict[str, Any]:
        model_path, plan_path = catalog.resolve(request.model)

        def work() -> dict[str, Any]:
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

        return jobs.submit("sweep", work).to_dict()

    @app.post("/api/optimize")
    def optimize_endpoint(request: OptimizeRequest) -> dict[str, Any]:
        model_path, plan_path = catalog.resolve(request.model)
        space = _build_space(request)
        if request.objective not in OBJECTIVES:
            raise HTTPException(status_code=400, detail=f"unknown objective {request.objective!r}")
        if request.optimizer not in OPTIMIZERS:
            raise HTTPException(status_code=400, detail=f"unknown optimizer {request.optimizer!r}")

        def work() -> dict[str, Any]:
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

        return jobs.submit("optimize", work).to_dict()

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [job.to_dict() for job in jobs.list()]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        return job.to_dict()

    return app


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
