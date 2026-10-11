"""Portfolio runner: pipelines in series and in parallel, Pareto menu, recommendation
(COMP-024, REQ-115, REQ-118, REQ-120, REQ-121, D-079; SDD "Portfolio" pseudo-code, Pinedo
Sec 17.3 p.463-466, Sec 18.3 p.492-495).

`solve_portfolio(request, *, budget_seconds=None, seed=0, pipelines=None, k_lookup=None)`:

1. The request is checked and compiled once (`solver.prepare`); a refusal (budget,
   validator issues) runs no solver and returns the issues.
2. The effective objective: `load.effective_objective(profile, load_ratio(compiled))`, then
   churn as `reschedule` adds it (`objectives.effective_spec`, `profile.churn_weight`, the
   previous plan's size). Every stage runs and is verified with it. A ratio of inf (work but
   no capacity before the horizon) is reported as None plus `load_ratio_unbounded`: the
   model holds finite floats only (D-060).
3. Pipelines (`profile.default_portfolio` when None) are "a->b->c" registry names. Stage k
   of pipeline index i yields candidate f"{i:02d}:{stages up to k}"; it gets the previous
   stage's schedule as warm start only when that schedule verified. Each stage is checked
   exactly like `solve()` (`solver.solve_prepared`). `k_lookup` goes to every `atcs`
   stage's constructor, `churn_weight=profile.churn_weight` to every `reschedule` stage.
   An unknown name is an `unknown_solver` candidate; a solver or worker failure is a
   `solver_error` candidate whose message is the exception class name only.
4. Budget: each pipeline gets budget * min(1, workers / len(pipelines)) * 0.9 from its own
   start, and no pipeline runs past 90 percent of the budget from the run's start; the rest
   is for Pareto search. A stage gets what its pipeline has left (at least 0.01 s).
5. With `profile.workers` > 1 the pipelines run in a spawn-context process pool, otherwise
   in this process. Workers rebuild solvers by registry name, so only names, params and
   the picklable `k_lookup` cross the process boundary. Results are ordered by pipeline
   index, so deterministic methods give the same result with any worker count.
6. Pareto search (`pareto_search`) seeds from the verified candidates of the dispatching
   rule families `dispatch` and `composite` (`registry.available()`); archive points that
   are not a seed's own plan become "pareto:{n}" candidates, decoded and checked like any
   other (`solver.check_schedule`).
7. `menu` = `pareto.menu` of the verified candidates on `profile.menu_axes`; `recommended`
   = `objectives.rank(verified values, effective objective, reference=first verified)[0]`,
   None with a `no_verified_candidate` issue when nothing verified. `atcs_runs` collects
   every candidate schedule's ATCS records in candidate order.

Pure apart from the clock: `time.monotonic` is the only clock, and no file or network I/O.
Deadlines are monotonic instants shared with spawned workers; the monotonic clock is
system-wide on the supported platforms (Linux, macOS).
"""

from __future__ import annotations

import concurrent.futures
import math
import multiprocessing
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from pydantic import TypeAdapter, ValidationError

from twinflow.scheduling import registry
from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.stats import KLookup
from twinflow.scheduling.model import (
    Candidate,
    CompiledProblem,
    ObjectiveKind,
    ObjectiveSpec,
    Pipeline,
    PortfolioResult,
    Schedule,
    ScheduleIssue,
    SolveRequest,
    SolveResult,
)
from twinflow.scheduling.objectives import effective_spec, primary_kind, rank
from twinflow.scheduling.portfolio.load import effective_objective, load_ratio
from twinflow.scheduling.portfolio.pareto import menu
from twinflow.scheduling.portfolio.pareto_search import ArchivePoint, Seed, pareto_search
from twinflow.scheduling.seam import OptionalDependencyError, ParamValue
from twinflow.scheduling.solver import Prepared, check_schedule, prepare, solve_prepared

PIPELINE_SHARE = 0.9
"""Share of the budget the pipelines get; the rest is kept for Pareto search (D-079)."""
MIN_STAGE_SECONDS = 0.01
"""Budget a stage gets when its pipeline has no time left (a budget must be > 0)."""
STAGE_SEPARATOR = "->"
ATCS = "atcs"
RESCHEDULE = "reschedule"
DISPATCH_FAMILIES = frozenset({"dispatch", "composite"})
"""Registry families whose plans seed Pareto search: elementary and composite dispatching
rules (ATC, ATCS; Pinedo Sec 14.2), the fast plans the swap neighborhood starts from."""
PARETO_SOLVER = "pareto_search"
SOLVER_PATH = "solver"
PIPELINES_PATH = "pipelines"
_PIPELINE = TypeAdapter(Pipeline)

PlanKey = tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class _PipelineJob:
    """One pipeline as a picklable work item: names and params, never solver objects.

    `end` is the monotonic instant past which no pipeline budget reaches.
    """

    index: int
    stages: tuple[str, ...]
    prepared: Prepared
    budget: float
    end: float
    seed: int
    k_lookup: KLookup | None
    churn_weight: float


def solve_portfolio(
    request: SolveRequest,
    *,
    budget_seconds: float | None = None,
    seed: int = 0,
    pipelines: Sequence[str] | None = None,
    k_lookup: KLookup | None = None,
) -> PortfolioResult:
    """Run the pipelines, Pareto search, menu and recommendation (module docstring).

    `budget_seconds` None means the profile's `time_budget_seconds`; wall time stays within
    budget * 1.1 + 1 s for solvers that honor their deadline.
    """
    started = time.monotonic()
    profile = request.profile
    budget = profile.time_budget_seconds if budget_seconds is None else budget_seconds
    prepared = prepare(request, budget)
    if prepared.compiled is None or prepared.issues:
        return _refused(profile.objective, prepared.issues, started)
    ratio = load_ratio(prepared.compiled)
    spec = _effective(request, ratio)
    prepared = replace(prepared, compiled=_with_objective(prepared.compiled, spec))
    names = profile.default_portfolio if pipelines is None else tuple(pipelines)
    jobs, issues = _jobs(names, prepared, budget, started, seed, k_lookup)
    candidates = _run_pipelines(jobs, profile.workers)
    dispatch = {info.name for info in registry.available() if info.family in DISPATCH_FAMILIES}
    seeds = [c for c in candidates if c.verified and c.schedule.solver in dispatch]
    deadline = min(time.monotonic() + (1.0 - PIPELINE_SHARE) * budget, started + budget)
    candidates += _pareto_candidates(prepared, spec, seeds, profile.menu_axes, deadline)
    return _result(candidates, spec, ratio, issues, profile.menu_axes, started)


def _effective(request: SolveRequest, ratio: float | None) -> ObjectiveSpec:
    """The load-weighted profile objective with churn against the previous plan."""
    previous = request.previous_plan
    count = len(previous.assignments) if previous is not None else 0
    spec = effective_objective(request.profile, ratio)
    return effective_spec(spec, request.profile.churn_weight, count)


def _with_objective(compiled: CompiledProblem, spec: ObjectiveSpec) -> CompiledProblem:
    """`compiled` whose profile objective is `spec`: what stages run and are verified with."""
    profile = compiled.profile.model_copy(update={"objective": spec})
    return compiled.model_copy(update={"profile": profile})


def _jobs(
    names: Sequence[str],
    prepared: Prepared,
    budget: float,
    started: float,
    seed: int,
    k_lookup: KLookup | None,
) -> tuple[list[_PipelineJob], list[ScheduleIssue]]:
    """A work item per valid pipeline (indices kept) and an issue per invalid one."""
    workers = prepared.request.profile.workers
    share = budget * min(1.0, workers / max(len(names), 1)) * PIPELINE_SHARE
    end = started + budget * PIPELINE_SHARE
    churn_weight = prepared.request.profile.churn_weight
    jobs: list[_PipelineJob] = []
    issues: list[ScheduleIssue] = []
    for index, name in enumerate(names):
        try:
            pipeline = _PIPELINE.validate_python(name)
        except ValidationError:
            message = f"pipeline {index} is not registry names joined by '->'"
            issues.append(
                ScheduleIssue(code="invalid_pipeline", path=PIPELINES_PATH, message=message)
            )
            continue
        stages = tuple(pipeline.split(STAGE_SEPARATOR))
        jobs.append(_PipelineJob(index, stages, prepared, share, end, seed, k_lookup, churn_weight))
    return jobs, issues


# --- pipelines -------------------------------------------------------------------------------


def _run_pipelines(jobs: Sequence[_PipelineJob], workers: int) -> list[Candidate]:
    """Every stage candidate in (pipeline index, stage) order, in process or in a pool."""
    if workers <= 1 or not jobs:
        return [candidate for job in jobs for candidate in _run_pipeline(job)]
    context = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(workers, len(jobs)), mp_context=context
    ) as pool:
        futures = [pool.submit(_run_pipeline, job) for job in jobs]
        return [c for job, future in zip(jobs, futures, strict=True) for c in _collect(job, future)]


def _collect(
    job: _PipelineJob, future: concurrent.futures.Future[tuple[Candidate, ...]]
) -> tuple[Candidate, ...]:
    """The worker's candidates; a dead or failing worker gives `solver_error` per stage."""
    try:
        return future.result()
    except Exception as error:  # a worker crash (BrokenProcessPool) must not end the run
        return tuple(
            _candidate(job.index, job.stages[: k + 1], _not_run_result(name, "solver_error", error))
            for k, name in enumerate(job.stages)
        )


def _run_pipeline(job: _PipelineJob) -> tuple[Candidate, ...]:
    """The stages in series; a verified schedule warm-starts the next stage."""
    deadline = min(time.monotonic() + job.budget, job.end)
    warm: Schedule | None = None
    candidates: list[Candidate] = []
    for k, name in enumerate(job.stages):
        remaining = max(deadline - time.monotonic(), MIN_STAGE_SECONDS)
        result = _run_stage(job, name, remaining, warm)
        candidates.append(_candidate(job.index, job.stages[: k + 1], result))
        warm = result.schedule if result.verified else None
    return tuple(candidates)


def _run_stage(job: _PipelineJob, name: str, budget: float, warm: Schedule | None) -> SolveResult:
    """One stage built by registry name and run through the `solve()` path."""
    try:
        solver = registry.get(name, **_stage_params(job, name))
    except registry.UnknownSolverError as error:
        return _not_run_result(name, "unknown_solver", error)
    except OptionalDependencyError as error:
        return _not_run_result(name, "optional_dependency_missing", error)
    except Exception as error:  # a factory bug must not end the run
        return _not_run_result(name, "solver_error", error)
    return solve_prepared(job.prepared, solver, budget, seed=job.seed, warm_start=warm)


def _stage_params(job: _PipelineJob, name: str) -> dict[str, ParamValue | KLookup]:
    """Constructor params: `k_lookup` for `atcs`, the profile churn weight for `reschedule`."""
    if name == ATCS and job.k_lookup is not None:
        return {"k_lookup": job.k_lookup}
    if name == RESCHEDULE:
        return {"churn_weight": job.churn_weight}
    return {}


def _not_run_result(name: str, code: str, error: Exception) -> SolveResult:
    """An UNKNOWN result for a stage whose solver never ran or whose worker died.

    The message names the stage and the exception class, never the exception text.
    """
    messages = {
        "unknown_solver": f"no solver is registered as {name!r}",
        "optional_dependency_missing": f"solver {name!r} needs an optional dependency",
    }
    message = messages.get(code, type(error).__name__)
    issue = ScheduleIssue(code=code, path=SOLVER_PATH, message=message)
    schedule = Schedule(status="UNKNOWN", assignments=(), solver=name, elapsed_seconds=0.0)
    return SolveResult(schedule=schedule, verified=False, issues=(issue,))


def _candidate(index: int, stages: tuple[str, ...], result: SolveResult) -> Candidate:
    return Candidate(
        id=f"{index:02d}:{STAGE_SEPARATOR.join(stages)}",
        pipeline=stages,
        schedule=result.schedule,
        verified=result.verified,
        objective_values=result.objective_values,
        issues=result.issues,
    )


# --- Pareto search ---------------------------------------------------------------------------


def _pareto_candidates(
    prepared: Prepared,
    spec: ObjectiveSpec,
    seeds: Sequence[Candidate],
    axes: Sequence[ObjectiveKind],
    deadline: float,
) -> list[Candidate]:
    """Archive points reached from the dispatch seeds, minus the seeds' own plans."""
    if not seeds or prepared.compiled is None:
        return []
    index = ProblemIndex.build(prepared.compiled.problem)
    plans = [_seed_of(c.schedule) for c in seeds]
    seed_keys = {_plan_key(sequences) for _, sequences in plans}
    started = time.monotonic()
    points = pareto_search(index, plans, axes, deadline)
    elapsed = time.monotonic() - started
    candidates: list[Candidate] = []
    for point in points:
        if _plan_key(point.sequences) in seed_keys:
            continue
        candidate_id = f"pareto:{len(candidates)}"
        candidate = _pareto_candidate(prepared, spec, index, point, candidate_id, elapsed)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _seed_of(schedule: Schedule) -> Seed:
    """The schedule's machine choice and start-ordered machine sequences."""
    ordered = sorted(schedule.assignments, key=lambda a: (a.start, a.operation_id))
    choice = {a.operation_id: a.machine_id for a in ordered}
    sequences: dict[str, list[str]] = {}
    for a in ordered:
        sequences.setdefault(a.machine_id, []).append(a.operation_id)
    return choice, sequences


def _plan_key(sequences: Mapping[str, Sequence[str]]) -> PlanKey:
    return tuple(sorted((machine, tuple(seq)) for machine, seq in sequences.items()))


def _pareto_candidate(
    prepared: Prepared,
    spec: ObjectiveSpec,
    index: ProblemIndex,
    point: ArchivePoint,
    candidate_id: str,
    elapsed: float,
) -> Candidate | None:
    """An archive point decoded and checked like a solver answer; None if it no longer decodes."""
    decoded = decode(index, point.choice, point.sequences)
    if not isinstance(decoded, Decoded):
        return None
    schedule = decoded.schedule(PARETO_SOLVER).model_copy(update={"elapsed_seconds": elapsed})
    result = check_schedule(prepared, schedule)
    if result.verified:
        primary = result.objective_values[primary_kind(spec)]
        schedule = schedule.model_copy(update={"primary_value": primary})
    return Candidate(
        id=candidate_id,
        pipeline=(),
        schedule=schedule,
        verified=result.verified,
        objective_values=result.objective_values,
        issues=result.issues,
    )


# --- result ----------------------------------------------------------------------------------


def _result(
    candidates: Sequence[Candidate],
    spec: ObjectiveSpec,
    ratio: float | None,
    issues: list[ScheduleIssue],
    axes: Sequence[ObjectiveKind],
    started: float,
) -> PortfolioResult:
    """Menu, recommendation, ATCS records and result issues over the candidates."""
    verified = {c.id: c.objective_values for c in candidates if c.verified}
    recommended = rank(verified, spec, reference=next(iter(verified)))[0] if verified else None
    if not verified:
        message = "no candidate produced a verified schedule"
        issues.append(
            ScheduleIssue(code="no_verified_candidate", path="candidates", message=message)
        )
    if ratio is not None and math.isinf(ratio):
        message = "a work center has work but no working time before the horizon"
        issues.append(
            ScheduleIssue(code="load_ratio_unbounded", path="load_ratio", message=message)
        )
        ratio = None
    return PortfolioResult(
        candidates=tuple(candidates),
        menu=menu(verified, axes),
        recommended=recommended,
        effective_objective=spec,
        load_ratio=ratio,
        atcs_runs=tuple(run for c in candidates for run in c.schedule.atcs_runs),
        issues=tuple(issues),
        elapsed_seconds=time.monotonic() - started,
    )


def _refused(
    objective: ObjectiveSpec, issues: Sequence[ScheduleIssue], started: float
) -> PortfolioResult:
    """A result for a refused request: no candidate ran."""
    return PortfolioResult(
        candidates=(),
        menu=(),
        recommended=None,
        effective_objective=objective,
        load_ratio=None,
        issues=tuple(issues),
        elapsed_seconds=time.monotonic() - started,
    )
