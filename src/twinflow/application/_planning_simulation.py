"""Plan simulation for the planning service (COMP-139; REQ-127, REQ-136, REQ-138..REQ-141).

`Simulator` runs `simulate_plan` and `simulate_portfolio` for `PlanningService`, which keeps
the keyed claim around each call (TF-005 D-120). Every call:

1. re-verifies each plan against its reconstructed request (`plan_source.check_plan`,
   D-087); a blocked plan stores nothing and is not claimed;
2. takes the policy from the `[simulation]` table of the problem's signed profile,
   re-verified now, or of the stored signed profile `profile_digest` names (D-104); a
   scenario plan uses `SimulationPolicy()` with the scenario capsule's `CapsuleSource`;
3. caps the run (REQ-139): replications = `reps` or the policy value, refused above
   min(policy cap, service cap); budget = min(request, policy, service cap), clamped;
   workers = min(policy, service cap);
4. runs `evaluate_candidates` on the planning `SolveRunner` (wall limit = budget + grace,
   D-105) and returns the records to store by reference (`_simulation_records`).

The engine module is imported by its full name at call time (`load_engine`), so a missing
engine is `CapabilityUnavailable` while the rest of planning keeps working.

Portfolio candidates (D-108): recommended, then menu entries in menu order, then the rest by
candidate id. A candidate stored as unverified, or failing re-verification, is skipped with
`candidate_unverified`; verified candidates past min(policy, service) candidate caps get
`candidate_cap`.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from twinflow.application._planning_claims import check_budget, check_seed
from twinflow.application._planning_models import (
    CandidateSummary,
    CapabilityUnavailable,
    NotFound,
    PlanningError,
    PortfolioSimulation,
    SimulateResult,
    SimulationComparison,
    SimulationEvidencePage,
    SizeLimit,
)
from twinflow.application._planning_records import RecordRow, StoredProblem
from twinflow.application._planning_runner import SolveRunner
from twinflow.application._simulation_records import (
    SimulationContext,
    candidate_summary,
    evaluation_row,
    evidence_page,
    new_evaluation_id,
    new_simulation_id,
    simulation_comparison,
    simulation_row,
)
from twinflow.application.artifacts import ArtifactSignatureInvalid
from twinflow.application.plan_source import ReconstructedPlan, check_plan
from twinflow.application.planning_artifacts import PlanningArtifacts
from twinflow.application.repository import WorkspaceRepository
from twinflow.execution.policy import SimulationPolicy, policy_digest
from twinflow.execution.sources import CapsuleSource, DistributionSource, resolve
from twinflow.scheduling import ObjectiveSpec, Schedule, ScheduleIssue

if TYPE_CHECKING:
    from twinflow.application._simulation_records import PairedDeltaFn
    from twinflow.execution.evaluate import CandidateEvaluation, CandidateInput, EvaluationResult

ENGINE_MODULE = "twinflow.execution.evaluate"
MAX_EVIDENCE_LIMIT = 1_000

Stored = tuple[str | None, list[RecordRow], Any]
"""`(result id or None when nothing is stored, records, result)` for the keyed claim."""

log = logging.getLogger(__name__)


# --- inputs and limits ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimulationLimits:
    """Service caps (`ServiceSettings`): seconds, replications, candidates, workers."""

    max_seconds: float
    max_replications: int
    max_candidates: int
    max_workers: int

    def __post_init__(self) -> None:
        check_budget(self.max_seconds)
        if min(self.max_replications, self.max_candidates, self.max_workers) < 1:
            raise ValueError("simulation caps must be at least 1")


@dataclass(frozen=True)
class SimulationRequest:
    """The caller's simulate settings, checked on construction."""

    reps: int | None
    seed: int
    time_budget_seconds: float | None
    baseline: bool
    profile_digest: str | None

    def __post_init__(self) -> None:
        if self.reps is not None and self.reps < 1:
            raise ValueError("reps must be at least 1")
        check_seed(self.seed)
        check_budget(self.time_budget_seconds)

    def inputs(self) -> dict[str, Any]:
        """The settings as idempotency-digest inputs."""
        return {
            "reps": self.reps,
            "seed": self.seed,
            "time_budget_seconds": self.time_budget_seconds,
            "baseline": self.baseline,
            "profile_digest": self.profile_digest,
        }


@dataclass(frozen=True)
class Engine:
    """The engine entry points, read from `twinflow.execution.evaluate` at call time."""

    evaluate_candidates: Callable[..., EvaluationResult]
    candidate: type[CandidateInput]
    paired_delta: PairedDeltaFn
    budget_exhausted: type[Exception]


def load_engine() -> Engine:
    """The execution engine, or `CapabilityUnavailable` when it cannot be imported."""
    try:
        module = importlib.import_module(ENGINE_MODULE)
    except ImportError:
        raise CapabilityUnavailable("The simulation engine is not installed") from None
    return Engine(
        evaluate_candidates=module.evaluate_candidates,
        candidate=module.CandidateInput,
        paired_delta=module.paired_delta,
        budget_exhausted=module.BudgetExhausted,
    )


@dataclass(frozen=True)
class _Setup:
    """Problem identity, policy, source and load targets of one simulate call."""

    source_kind: str
    problem_id: str
    problem_digest: str
    policy: SimulationPolicy
    source: DistributionSource
    targets: tuple[float, ...]


@dataclass(frozen=True)
class _RunSettings:
    replications: int
    budget_seconds: float
    workers: int


# --- the simulator -------------------------------------------------------------------------------


class Simulator:
    """Simulate stored plans over one repository on the planning runner."""

    def __init__(
        self, repository: WorkspaceRepository, runner: SolveRunner, limits: SimulationLimits
    ) -> None:
        self._repository = repository
        self._runner = runner
        self._limits = limits

    def simulate_plan(
        self, engine: Engine, plan_id: str, request: SimulationRequest, artifacts: PlanningArtifacts
    ) -> Stored:
        """One plan (ERP or scenario), plus the dispatch baseline when asked."""
        check = check_plan(self._repository, plan_id)
        if check.plan is None:
            return None, [], _blocked_plan(plan_id, check.issues)
        plan = check.plan
        setup = self._plan_setup(plan, request, artifacts)
        rule = setup.policy.baseline_rule if request.baseline else None
        candidate = engine.candidate(plan.plan_id, plan.compiled, plan.schedule)
        result, context, run_issues = self._evaluate(engine, [candidate], setup, request, rule)
        rows, summaries, baseline = _rows(result, context, {plan_id: plan.schedule}, rule, {})
        outcome = SimulateResult(
            plan_id=plan_id,
            status=result.status,
            simulation_id=summaries[0].simulation_id,
            issues=run_issues,
            summary=summaries[0],
            baseline=baseline,
        )
        rows[0][2]["result"] = outcome.model_dump(mode="json")
        return f"simulations/{summaries[0].simulation_id}", rows, outcome

    def simulate_portfolio(
        self,
        engine: Engine,
        portfolio_id: str,
        request: SimulationRequest,
        artifacts: PlanningArtifacts,
    ) -> Stored:
        """The portfolio's verified candidates in D-108 order up to the cap, one evaluation."""
        portfolio = self._get("portfolios", portfolio_id)
        problem = StoredProblem.from_record(self._get("problems", portfolio["problem_id"]))
        setup = self._erp_setup(problem, request, artifacts)
        cap = min(setup.policy.max_candidates, self._limits.max_candidates)
        plans, issues = select_candidates(self._repository, candidate_order(portfolio), cap)
        if not plans:
            return None, [], _blocked_portfolio(portfolio_id, request.seed, issues)
        rule = setup.policy.baseline_rule if request.baseline else None
        result, context, run_issues = self._evaluate(
            engine, _inputs(engine, plans), setup, request, rule
        )
        evaluation_id = new_evaluation_id()
        schedules = {plan.plan_id: plan.schedule for plan in plans}
        link = {"evaluation_id": evaluation_id}
        rows, summaries, baseline = _rows(result, context, schedules, rule, link)
        outcome = PortfolioSimulation(
            evaluation_id=evaluation_id,
            portfolio_id=portfolio_id,
            status=result.status,
            replications=result.replications,
            seed=request.seed,
            candidates=summaries,
            baseline=baseline,
            ranking=result.ranking,
            issues=(*issues, *run_issues),
        )
        ids = {str(s.plan_id): s.simulation_id for s in summaries}
        base_id = baseline.simulation_id if baseline is not None else None
        dumped = outcome.model_dump(mode="json")
        rows.append(evaluation_row(evaluation_id, problem.problem_id, dumped, ids, base_id))
        return f"evaluations/{evaluation_id}", rows, outcome

    def comparison(
        self,
        engine: Engine,
        plan_ids: Sequence[str],
        simulation_ids: Sequence[str],
        objective: ObjectiveSpec,
    ) -> SimulationComparison:
        """The `CompareResult.simulation` block from stored simulations (read only)."""
        records = [self._get("simulations", simulation_id) for simulation_id in simulation_ids]
        return simulation_comparison(plan_ids, records, objective, engine.paired_delta)

    def evidence(
        self, simulation_id: str, *, entity_id: str | None, offset: int, limit: int
    ) -> SimulationEvidencePage:
        """One page of a stored simulation's operation then job rows."""
        if offset < 0 or not 1 <= limit <= MAX_EVIDENCE_LIMIT:
            raise ValueError(f"offset must be >= 0 and limit between 1 and {MAX_EVIDENCE_LIMIT}")
        record = self._get("simulations", simulation_id)
        return evidence_page(record, entity_id=entity_id, offset=offset, limit=limit)

    # --- setup ---------------------------------------------------------------------------------

    def _plan_setup(
        self, plan: ReconstructedPlan, request: SimulationRequest, artifacts: PlanningArtifacts
    ) -> _Setup:
        if plan.source_kind == "erp":
            problem = StoredProblem.from_record(self._get("problems", plan.problem_id))
            return self._erp_setup(problem, request, artifacts)
        if request.profile_digest is not None:
            raise ValueError("profile_digest applies to ERP plans only")
        record = self._get("schedule_plans", plan.plan_id)
        scenario = self._get("scenarios", record["scenario_id"])
        return _Setup(
            source_kind="scenario",
            problem_id=plan.problem_id,
            problem_digest=plan.problem_digest,
            policy=SimulationPolicy(),
            source=CapsuleSource(scenario["capsule"]["model"]),
            targets=tuple(plan.request.profile.robustness_load_targets),
        )

    def _erp_setup(
        self, problem: StoredProblem, request: SimulationRequest, artifacts: PlanningArtifacts
    ) -> _Setup:
        policy = _signed_policy(problem, request.profile_digest, artifacts)
        return _Setup(
            source_kind="erp",
            problem_id=problem.problem_id,
            problem_digest=problem.problem_digest,
            policy=policy,
            source=resolve(policy),
            targets=tuple(problem.request.profile.robustness_load_targets),
        )

    def _run_settings(self, policy: SimulationPolicy, request: SimulationRequest) -> _RunSettings:
        """Replications, budget and workers under the policy and service caps (REQ-139)."""
        cap = min(policy.max_replications, self._limits.max_replications)
        if request.reps is not None and request.reps > cap:
            raise SizeLimit(f"reps above the replication cap of {cap}")
        budgets = [policy.time_budget_seconds, self._limits.max_seconds]
        if request.time_budget_seconds is not None:
            budgets.append(request.time_budget_seconds)
        return _RunSettings(
            replications=request.reps
            if request.reps is not None
            else min(policy.replications, cap),
            budget_seconds=min(budgets),
            workers=min(policy.workers, self._limits.max_workers),
        )

    # --- running -------------------------------------------------------------------------------

    def _evaluate(
        self,
        engine: Engine,
        candidates: Sequence[CandidateInput],
        setup: _Setup,
        request: SimulationRequest,
        baseline_rule: str | None,
    ) -> tuple[EvaluationResult, SimulationContext, tuple[ScheduleIssue, ...]]:
        """`evaluate_candidates` on the runner; the result, its record context and the
        issues of the run (a clamped budget, replications stopped at the event cap)."""
        run = self._run_settings(setup.policy, request)

        def evaluate() -> EvaluationResult:
            return engine.evaluate_candidates(
                candidates,
                source=setup.source,
                policy=setup.policy,
                targets=setup.targets,
                seed=request.seed,
                replications=run.replications,
                budget_seconds=run.budget_seconds,
                workers=run.workers,
                baseline_rule=baseline_rule,
            )

        try:
            result = self._runner.run(evaluate, run.budget_seconds)
        except engine.budget_exhausted:
            raise PlanningError(
                "The budget ended before one replication finished", code="budget_exhausted"
            ) from None
        log.info(
            "simulated %d plan(s): %s, %d replication(s)",
            len(candidates),
            result.status,
            result.replications,
        )
        issues = (*_budget_issues(request, run), *_event_limit_issues(result))
        return result, _context(setup, request.seed, run.budget_seconds), issues

    def _get(self, kind: str, record_id: str) -> dict[str, Any]:
        try:
            return self._repository.get(kind, record_id)
        except KeyError:
            raise NotFound(f"Unknown {kind} id") from None


# --- portfolio candidates (D-108) ----------------------------------------------------------------


def candidate_order(portfolio: Mapping[str, Any]) -> list[str]:
    """Plan ids: recommended, then menu entries in menu order, then the rest by id."""
    candidate_ids = list(portfolio["candidate_ids"])
    recommended = portfolio["recommended"]
    first = [recommended] if recommended in candidate_ids else []
    menu = [entry["candidate_id"] for entry in portfolio["menu"]]
    ordered = dict.fromkeys([*first, *menu, *sorted(candidate_ids)])
    known = set(candidate_ids)
    return [f"{portfolio['id']}:{cid}" for cid in ordered if cid in known]


def select_candidates(
    repository: WorkspaceRepository, plan_ids: Sequence[str], cap: int
) -> tuple[list[ReconstructedPlan], list[ScheduleIssue]]:
    """Verified, re-verified candidates up to `cap`, and one issue per skipped plan.

    Plans past the cap are not re-verified: their stored flag decides between
    `candidate_unverified` and `candidate_cap`.
    """
    chosen: list[ReconstructedPlan] = []
    issues: list[ScheduleIssue] = []
    for plan_id in plan_ids:
        plan, issue = _candidate(repository, plan_id, len(chosen) >= cap, cap)
        if plan is not None:
            chosen.append(plan)
        if issue is not None:
            issues.append(issue)
    return chosen, issues


def _candidate(
    repository: WorkspaceRepository, plan_id: str, full: bool, cap: int
) -> tuple[ReconstructedPlan | None, ScheduleIssue | None]:
    """The re-verified plan to simulate, or the issue that skips it."""
    if not repository.get("plans", plan_id).get("verified"):
        return None, _candidate_issue("candidate_unverified", plan_id, "is not verified")
    if full:
        return None, _candidate_issue("candidate_cap", plan_id, f"is past the cap of {cap}")
    check = check_plan(repository, plan_id)
    if check.plan is None:
        return None, _candidate_issue("candidate_unverified", plan_id, "does not re-verify")
    return check.plan, None


def _inputs(engine: Engine, plans: Sequence[ReconstructedPlan]) -> list[CandidateInput]:
    """Engine inputs; plans of one compiled problem share it, so one shop is built for them."""
    first = plans[0].compiled
    return [
        engine.candidate(
            plan.plan_id, first if plan.compiled == first else plan.compiled, plan.schedule
        )
        for plan in plans
    ]


def _candidate_issue(code: str, plan_id: str, reason: str) -> ScheduleIssue:
    return ScheduleIssue(
        code=code, path="$.candidates", message=f"plan {plan_id} {reason}; not simulated"
    )


# --- helpers -------------------------------------------------------------------------------------


def _signed_policy(
    problem: StoredProblem, profile_digest: str | None, artifacts: PlanningArtifacts
) -> SimulationPolicy:
    """The `[simulation]` table of the re-verified signed profile (D-104); fails closed."""
    try:
        if profile_digest is None:
            artifacts.for_solve(problem.provenance, problem.profile, problem.request.profile)
            return problem.profile.simulation
        profile, _ = artifacts.profile(profile_digest)
    except ArtifactSignatureInvalid:
        raise PlanningError(
            "A signed artifact failed verification", code="artifact_signature_invalid"
        ) from None
    return profile.simulation


def _budget_issues(request: SimulationRequest, run: _RunSettings) -> tuple[ScheduleIssue, ...]:
    """`budget_clamped` when the requested budget was above the policy or service cap."""
    requested = request.time_budget_seconds
    if requested is None or requested <= run.budget_seconds:
        return ()
    message = (
        f"time_budget_seconds {requested:g} was clamped to {run.budget_seconds:g} "
        "(the policy and service caps)"
    )
    return (ScheduleIssue(code="budget_clamped", path="$.time_budget_seconds", message=message),)


def _event_limit_issues(result: EvaluationResult) -> tuple[ScheduleIssue, ...]:
    """`event_limit` when any replication stopped at its event cap (REQ-143, D-109)."""
    evaluations = [*result.candidates, *([result.baseline] if result.baseline else [])]
    runs = [run for evaluation in evaluations for run in evaluation.runs]
    stopped = sum(1 for run in runs if run.termination == "event_limit")
    if not stopped:
        return ()
    message = (
        f"{stopped} of {len(runs)} replication runs stopped at the event cap "
        "(50 events per operation plus 10,000); their unfinished operations are censored"
    )
    return (ScheduleIssue(code="event_limit", path="$.summary", message=message),)


def _context(setup: _Setup, seed: int, budget_seconds: float) -> SimulationContext:
    return SimulationContext(
        source_kind=setup.source_kind,
        problem_id=setup.problem_id,
        problem_digest=setup.problem_digest,
        seed=seed,
        policy_digest=policy_digest(setup.policy),
        confidence_level=setup.policy.confidence_level,
        source_name=setup.source.name,
        source_digest=setup.source.digest,
        budget_seconds=budget_seconds,
    )


def _rows(
    result: EvaluationResult,
    context: SimulationContext,
    schedules: Mapping[str, Schedule],
    baseline_rule: str | None,
    extra: Mapping[str, Any],
) -> tuple[list[RecordRow], tuple[CandidateSummary, ...], CandidateSummary | None]:
    """One `simulations` row per candidate and for the baseline, with their summaries."""
    stored = [
        _stored(evaluation, result, context, schedules[str(evaluation.plan_id)], None, extra)
        for evaluation in result.candidates
    ]
    baseline = None
    if result.baseline is not None:
        baseline = _stored(result.baseline, result, context, None, baseline_rule, extra)
        stored.append(baseline)
    rows = [row for _, row in stored]
    summaries = tuple(summary for summary, _ in stored[: len(result.candidates)])
    return rows, summaries, baseline[0] if baseline is not None else None


def _stored(
    evaluation: CandidateEvaluation,
    result: EvaluationResult,
    context: SimulationContext,
    schedule: Schedule | None,
    baseline_rule: str | None,
    extra: Mapping[str, Any],
) -> tuple[CandidateSummary, RecordRow]:
    """The summary of one evaluation under a fresh simulation id, and its record."""
    summary = candidate_summary(
        evaluation, result, simulation_id=new_simulation_id(), seed=context.seed
    )
    row = simulation_row(
        evaluation,
        summary,
        result,
        context,
        schedule=schedule,
        baseline_rule=baseline_rule,
        extra=extra,
    )
    return summary, row


def _blocked_plan(plan_id: str, issues: tuple[ScheduleIssue, ...]) -> SimulateResult:
    return SimulateResult(
        plan_id=plan_id,
        status="blocked",
        simulation_id=None,
        issues=issues,
        summary=None,
        baseline=None,
    )


def _blocked_portfolio(
    portfolio_id: str, seed: int, issues: Sequence[ScheduleIssue]
) -> PortfolioSimulation:
    return PortfolioSimulation(
        evaluation_id=None,
        portfolio_id=portfolio_id,
        status="blocked",
        replications=0,
        seed=seed,
        candidates=(),
        baseline=None,
        ranking=(),
        issues=tuple(issues),
    )
