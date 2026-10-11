"""Planning service (COMP-117): the one use-case layer behind REST and MCP for ERP planning.

`PlanningService` validates ERP snapshots, builds and stores scheduling problems by content
address, wraps the four TF-003 portfolio functions through `portfolio_port`, and serves plan
reads, comparisons and plan simulations. Requirements: REQ-159, REQ-163, REQ-167,
REQ-168, REQ-169, REQ-177, and TF-004 REQ-127, REQ-136, REQ-138..REQ-141 (REQ-140 supersedes
REQ-171). Decisions: D-119 (plans by reference), D-120 (idempotency, capped synchronous
budget), TF-004 D-104, D-105, D-108 (simulation, see `_planning_simulation`).

* Records: `erp_snapshots` (id = snapshot digest), `problems` (id = problem id), `portfolios`,
  `plans`, `simulations` and `evaluations`; every record and result is
  `validity = "provisional"` with an `evidence_kind`.
* Idempotency: a state-modifying call claims `(caller_id, request_key)` in the workspace
  `requests` table with the digest of its method and inputs, atomically with the records it
  stores. Same digest replays the stored result; another digest is `IdempotencyConflict`.
  A call that times out, fails or is blocked stores no claim, so a retry runs again.
* Budget: `min(requested, the problem profile's time_budget_seconds, max_solve_seconds)`;
  the wall-clock limit is budget + `timeout_grace_seconds`.
* TF-003 absent: the four wrapped methods raise `CapabilityUnavailable`; reads still work.
  `explain_plan` is B-6 and always raises `CapabilityUnavailable`.
* Signed artifacts (COMP-127, REQ-184): problem and plan records carry the `Provenance` of
  their profile and K-table. Immediately before each solve-type call the problem's profile
  is re-verified and the active K-table verified through the caller's `PlanningArtifacts`
  (TF-003 gets it as `k_lookup`). A caller without one is the in-process local embedding:
  only unsigned problems solve there; a signed problem fails closed.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any, NoReturn, TypeVar

from pydantic import BaseModel

from twinflow.application._planning_claims import (
    KeyLocks,
    check_budget,
    check_seed,
    claim_key,
    input_digest,
)
from twinflow.application._planning_models import (
    Blocked,
    BuildRecord,
    CapabilityUnavailable,
    CompareResult,
    CompareRow,
    IdempotencyConflict,
    MenuRecord,
    NotFound,
    PlanningError,
    PlanningTimeout,
    PlanPage,
    PortfolioRecord,
    PortfolioSimulation,
    RushRecord,
    SimulateResult,
    SimulationEvidencePage,
    SizeLimit,
)
from twinflow.application._planning_records import (
    RecordRow,
    StoredProblem,
    build_records,
    menu_record,
    plan_page,
    portfolio_records,
    previous_plan,
    rush_records,
)
from twinflow.application._planning_runner import SolveRunner
from twinflow.application._planning_simulation import (
    SimulationLimits,
    SimulationRequest,
    Simulator,
    load_engine,
)
from twinflow.application.planning_artifacts import PlanningArtifacts, Provenance
from twinflow.application.portfolio_port import PortfolioApi
from twinflow.application.workspace import Workspace
from twinflow.erp.build import RushOrder, build_problem_from_erp, build_rush_job
from twinflow.erp.profile import PlanningProfile, profile_digest
from twinflow.erp.schema import ErpSnapshot
from twinflow.erp.validate import ValidationReport, validate_snapshot
from twinflow.scheduling import ObjectiveKind, Override, rank
from twinflow.scheduling.model import MAX_OVERRIDES

__all__ = [
    "Blocked",
    "BuildRecord",
    "CapabilityUnavailable",
    "CompareResult",
    "IdempotencyConflict",
    "MenuRecord",
    "NotFound",
    "PlanPage",
    "PlanningError",
    "PlanningService",
    "PlanningTimeout",
    "PortfolioRecord",
    "PortfolioSimulation",
    "RushRecord",
    "SimulateResult",
    "SimulationEvidencePage",
    "SizeLimit",
]

MAX_PAGE_LIMIT = 1_000
COMPARE_SIZES = range(2, 11)

ResultT = TypeVar("ResultT", bound=BaseModel)
Computed = tuple[str | None, list[RecordRow], ResultT]
"""`(result id, records, result)`; a None id stores nothing and claims nothing (blocked)."""

log = logging.getLogger(__name__)

# In-process callers that pass no artifacts: local embedding, no artifact store.
_LOCAL_ARTIFACTS = PlanningArtifacts(None, local_mode=True)


class PlanningService:
    """ERP planning use cases over one workspace; TF-003 is optional (`portfolio=None`)."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        portfolio: PortfolioApi | None,
        max_solve_seconds: float = 120.0,
        timeout_grace_seconds: float = 15.0,
        max_workers: int = 4,
        max_simulate_seconds: float = 120.0,
        max_simulation_replications: int = 200,
        max_simulation_candidates: int = 20,
        max_simulation_workers: int = 4,
    ) -> None:
        check_budget(max_solve_seconds)
        limits = SimulationLimits(
            max_seconds=max_simulate_seconds,
            max_replications=max_simulation_replications,
            max_candidates=max_simulation_candidates,
            max_workers=max_simulation_workers,
        )
        self._repository = workspace.repository
        self._portfolio = portfolio
        self._max_solve_seconds = max_solve_seconds
        self._simulation_limits = limits
        self._runner = SolveRunner(max_workers=max_workers, grace_seconds=timeout_grace_seconds)
        self._simulator = Simulator(self._repository, self._runner, limits)
        self._locks = KeyLocks()

    @property
    def max_solve_seconds(self) -> float:
        """The configured cap on any solve's time budget (REQ-169)."""
        return self._max_solve_seconds

    @property
    def max_simulate_seconds(self) -> float:
        """The configured cap on any simulation's time budget (TF-004 REQ-139)."""
        return self._simulation_limits.max_seconds

    def simulation_caps(self) -> dict[str, float | int]:
        """The four simulation caps, named like the `ServiceSettings` fields (REQ-139)."""
        limits = self._simulation_limits
        return {
            "max_simulate_seconds": limits.max_seconds,
            "max_simulation_replications": limits.max_replications,
            "max_simulation_candidates": limits.max_candidates,
            "max_simulation_workers": limits.max_workers,
        }

    @property
    def portfolio_available(self) -> bool:
        """Whether TF-003's portfolio functions are installed."""
        return self._portfolio is not None

    def close(self) -> None:
        """Release the solve pool without waiting for running solves."""
        self._runner.close()

    def claim_lock(self, key: str) -> AbstractContextManager[None]:
        """Serialize in-process duplicates of one claim key, here and in keyed tool calls."""
        return self._locks.hold(key)

    # --- snapshots and problems --------------------------------------------------------------

    def validate_snapshot(
        self, snapshot: ErpSnapshot, profile: PlanningProfile
    ) -> ValidationReport:
        """Every issue of `snapshot` under `profile`; writes no record."""
        return validate_snapshot(snapshot, profile)

    def build_problem(
        self,
        snapshot: ErpSnapshot,
        profile: PlanningProfile,
        *,
        provenance: Provenance | None = None,
    ) -> BuildRecord:
        """Build the scheduling problem and store snapshot and problem once by content address.

        `provenance` names the signed profile (and K-table) `profile` was read from; None
        records `profile` as unsigned. A blocked build stores the snapshot with its report
        and no problem. A repeat call writes nothing and returns the same ids.
        """
        provenance = provenance or Provenance.unsigned(profile)
        if provenance.profile_digest != profile_digest(profile):
            raise ValueError("provenance names another profile")
        result = build_problem_from_erp(snapshot, profile)
        record, rows = build_records(snapshot, profile, result, provenance)
        for kind, record_id, body in rows:
            self._create_once(kind, record_id, body)
        return record

    # --- TF-003 wrappers ---------------------------------------------------------------------

    def solve_portfolio(
        self,
        problem_id: str,
        *,
        request_key: str,
        caller_id: str,
        time_budget_seconds: float | None = None,
        seed: int = 0,
        pipelines: tuple[str, ...] | None = None,
        artifacts: PlanningArtifacts | None = None,
    ) -> PortfolioRecord:
        """Solve a stored problem with the TF-003 portfolio; store one plan per candidate."""
        api = self._api()
        check_budget(time_budget_seconds)
        check_seed(seed)
        inputs = {
            "problem_id": problem_id,
            "time_budget_seconds": time_budget_seconds,
            "seed": seed,
            "pipelines": list(pipelines) if pipelines is not None else None,
        }

        def compute() -> Computed[PortfolioRecord]:
            problem = self._problem(problem_id)
            provenance, k_lookup = self._verified(problem, artifacts)
            budget = self._budget(problem, time_budget_seconds)
            result = self._runner.run(
                lambda: api.solve_portfolio(
                    problem.request,
                    budget_seconds=budget,
                    seed=seed,
                    pipelines=pipelines,
                    k_lookup=k_lookup,
                ),
                budget,
            )
            portfolio_id = f"pf-{uuid.uuid4().hex}"
            origin = {
                "method": "solve_portfolio",
                "seed": seed,
                "budget_seconds": budget,
                **provenance,
            }
            summary, rows = portfolio_records(problem, portfolio_id, result, origin)
            return f"portfolios/{portfolio_id}", rows, summary

        return self._claimed(
            "solve_portfolio", caller_id, request_key, inputs, compute, PortfolioRecord
        )

    def resolve_with_overrides(
        self,
        plan_id: str,
        overrides: Sequence[Override],
        *,
        request_key: str,
        caller_id: str,
        time_budget_seconds: float | None = None,
        seed: int = 0,
        artifacts: PlanningArtifacts | None = None,
    ) -> PortfolioRecord:
        """Re-plan from a stored plan under `overrides`; a new portfolio on the same problem."""
        api = self._api()
        check_budget(time_budget_seconds)
        check_seed(seed)
        if len(overrides) > MAX_OVERRIDES:
            raise SizeLimit(f"At most {MAX_OVERRIDES} overrides are accepted")
        dumped = [override.model_dump(mode="json") for override in overrides]
        inputs = {
            "plan_id": plan_id,
            "overrides": dumped,
            "time_budget_seconds": time_budget_seconds,
            "seed": seed,
        }

        def compute() -> Computed[PortfolioRecord]:
            plan = self._get("plans", plan_id)
            problem = self._problem(plan["problem_id"])
            provenance, _ = self._verified(problem, artifacts)
            budget = self._budget(problem, time_budget_seconds)
            previous = previous_plan(plan)
            result = self._runner.run(
                lambda: api.resolve_with_overrides(
                    problem.request, previous, tuple(overrides), budget_seconds=budget, seed=seed
                ),
                budget,
            )
            portfolio_id = f"pf-{uuid.uuid4().hex}"
            origin = {
                "method": "resolve_with_overrides",
                "previous_plan_id": plan_id,
                "overrides": dumped,
                "seed": seed,
                "budget_seconds": budget,
                **provenance,
            }
            summary, rows = portfolio_records(problem, portfolio_id, result, origin)
            return f"portfolios/{portfolio_id}", rows, summary

        return self._claimed(
            "resolve_with_overrides", caller_id, request_key, inputs, compute, PortfolioRecord
        )

    def insert_rush_order(
        self,
        plan_id: str,
        rush: RushOrder,
        *,
        request_key: str,
        caller_id: str,
        time_budget_seconds: float | None = None,
        seed: int = 0,
        artifacts: PlanningArtifacts | None = None,
    ) -> RushRecord:
        """Insert a rush order into a stored plan; the rush job comes from the item's routing.

        An item without a usable standard routing raises `Blocked` (`no_routing`) before any
        solve and stores nothing.
        """
        api = self._api()
        check_budget(time_budget_seconds)
        check_seed(seed)
        inputs = {
            "plan_id": plan_id,
            "rush": rush.model_dump(mode="json"),
            "time_budget_seconds": time_budget_seconds,
            "seed": seed,
        }

        def compute() -> Computed[RushRecord]:
            plan = self._get("plans", plan_id)
            problem = self._problem(plan["problem_id"])
            provenance, _ = self._verified(problem, artifacts)
            snapshot = ErpSnapshot.model_validate(
                self._get("erp_snapshots", problem.snapshot_digest)["snapshot"]
            )
            rush_job = build_rush_job(snapshot, problem.profile, rush)
            if isinstance(rush_job, tuple):
                raise Blocked("The rush order cannot become a job", rush_job)
            budget = self._budget(problem, time_budget_seconds)
            previous = previous_plan(plan)
            result = self._runner.run(
                lambda: api.insert_rush_order(
                    problem.request, previous, rush_job, budget_seconds=budget, seed=seed
                ),
                budget,
            )
            new_plan_id = f"rush-{uuid.uuid4().hex}"
            origin = {
                "method": "insert_rush_order",
                "previous_plan_id": plan_id,
                "rush_order": inputs["rush"],
                "seed": seed,
                "budget_seconds": budget,
                **provenance,
            }
            summary, rows = rush_records(problem, new_plan_id, result, rush_job, origin)
            return f"plans/{new_plan_id}", rows, summary

        return self._claimed(
            "insert_rush_order", caller_id, request_key, inputs, compute, RushRecord
        )

    def get_pareto_menu(self, portfolio_id: str) -> MenuRecord:
        """The portfolio's Pareto menu on the profile's axes, each entry naming its plan.

        TF-003's `get_pareto_menu(result)` with default axes is, by contract, the stored
        `result.menu`, so the stored menu is returned without re-running TF-003.
        """
        self._api()
        return menu_record(self._get("portfolios", portfolio_id))

    def explain_plan(self, plan_id: str, job_ids: Sequence[str] | None = None) -> NoReturn:
        """Not available until B-6 ships plan explanations."""
        raise CapabilityUnavailable("Plan explanations are not available yet")

    # --- reads -------------------------------------------------------------------------------

    def get_plan(
        self, plan_id: str, *, offset: int = 0, limit: int = 100, job_id: str | None = None
    ) -> PlanPage:
        """One page of a stored plan's assignments with lineage; optionally one job's."""
        if offset < 0 or not 1 <= limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"offset must be >= 0 and limit between 1 and {MAX_PAGE_LIMIT}")
        plan = self._get("plans", plan_id)
        lineage_index = self._get("problems", plan["problem_id"])["lineage_index"]
        return plan_page(plan, lineage_index, offset=offset, limit=limit, job_id=job_id)

    def compare_plans(
        self, plan_ids: Sequence[str], simulation_ids: Sequence[str] | None = None
    ) -> CompareResult:
        """Objective values of 2 to 10 plans of one problem, ranked by the profile objective.

        With `simulation_ids` (one stored plan simulation per plan, same order) the result
        adds paired deltas against the first plan and the simulated ranking (read only).
        """
        if len(plan_ids) not in COMPARE_SIZES:
            raise ValueError("compare_plans needs 2 to 10 plan ids")
        plans = [self._get("plans", plan_id) for plan_id in plan_ids]
        if len({plan["problem_digest"] for plan in plans}) != 1:
            raise PlanningError("Plans of different problems", code="mixed_problems")
        problem = self._problem(plans[0]["problem_id"])
        objective = problem.request.profile.objective
        values = {
            plan["id"]: {ObjectiveKind(k): v for k, v in plan["objective_values"].items()}
            for plan in plans
        }
        try:
            ranking = rank(values, objective)
        except KeyError:
            raise PlanningError(
                "A plan has no value for the profile objective", code="incomparable"
            ) from None
        rows = tuple(
            CompareRow(
                plan_id=plan["id"],
                objective_values=plan["objective_values"],
                verified=plan["verified"],
            )
            for plan in plans
        )
        simulation = None
        if simulation_ids is not None:
            simulation = self._simulator.comparison(
                load_engine(), plan_ids, simulation_ids, objective
            )
        return CompareResult(
            problem_id=problem.problem_id,
            plans=rows,
            ranking=tuple(ranking),
            simulation=simulation,
        )

    # --- simulation (TF-004) -----------------------------------------------------------------

    def simulate_plan(
        self,
        plan_id: str,
        *,
        request_key: str,
        caller_id: str,
        reps: int | None = None,
        seed: int = 42,
        time_budget_seconds: float | None = None,
        baseline: bool = False,
        profile_digest: str | None = None,
        artifacts: PlanningArtifacts | None = None,
    ) -> SimulateResult:
        """Re-verify and simulate one ERP or scenario plan; stored as `simulations` records.

        A plan that does not re-verify is `blocked` (`plan_unverified` or
        `schedule_source_changed`) and nothing is stored (D-087, REQ-140).
        """
        request = SimulationRequest(reps, seed, time_budget_seconds, baseline, profile_digest)
        engine = load_engine()
        resolver = artifacts or _LOCAL_ARTIFACTS

        def compute() -> Computed[SimulateResult]:
            return self._simulator.simulate_plan(engine, plan_id, request, resolver)

        inputs = {"plan_id": plan_id, **request.inputs()}
        return self._claimed(
            "simulate_plan", caller_id, request_key, inputs, compute, SimulateResult, "result"
        )

    def simulate_portfolio(
        self,
        portfolio_id: str,
        *,
        request_key: str,
        caller_id: str,
        reps: int | None = None,
        seed: int = 42,
        time_budget_seconds: float | None = None,
        baseline: bool = True,
        profile_digest: str | None = None,
        artifacts: PlanningArtifacts | None = None,
    ) -> PortfolioSimulation:
        """Simulate a portfolio's verified candidates (D-108); one `evaluations` record."""
        request = SimulationRequest(reps, seed, time_budget_seconds, baseline, profile_digest)
        engine = load_engine()
        resolver = artifacts or _LOCAL_ARTIFACTS

        def compute() -> Computed[PortfolioSimulation]:
            return self._simulator.simulate_portfolio(engine, portfolio_id, request, resolver)

        inputs = {"portfolio_id": portfolio_id, **request.inputs()}
        return self._claimed(
            "simulate_portfolio", caller_id, request_key, inputs, compute, PortfolioSimulation
        )

    def simulation_evidence(
        self,
        simulation_id: str,
        *,
        entity_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> SimulationEvidencePage:
        """One page of a stored simulation's operation rows, then its job rows (REQ-141)."""
        return self._simulator.evidence(
            simulation_id, entity_id=entity_id, offset=offset, limit=limit
        )

    # --- internals ---------------------------------------------------------------------------

    def _claimed(
        self,
        method: str,
        caller_id: str,
        request_key: str,
        inputs: dict[str, Any],
        compute: Callable[[], Computed[ResultT]],
        model: type[ResultT],
        field: str = "summary",
    ) -> ResultT:
        """Replay the stored result of this claim, or compute, store and claim it atomically.

        The result is stored under `field` of the record the claim names. A computed result
        with no id (a blocked plan) is returned without storing or claiming anything.
        """
        key = claim_key(caller_id, request_key)
        digest = input_digest(method, inputs)
        with self.claim_lock(key):
            claimed = self._repository.find_request_claim(key)
            if claimed is None:
                result_id, rows, result = compute()
                if result_id is None:
                    return result
                claimed = self._repository.claim_request(key, digest, result_id, rows)
                if claimed == (digest, result_id):
                    log.info("planning %s stored %s", method, result_id)
                    return result
            return self._replay(claimed, digest, model, field)

    def _replay(
        self, claimed: tuple[str, str], digest: str, model: type[ResultT], field: str
    ) -> ResultT:
        stored_digest, result_id = claimed
        if stored_digest != digest:
            raise IdempotencyConflict("The request key was already used with different inputs")
        kind, _, record_id = result_id.partition("/")
        return model.model_validate(self._repository.get(kind, record_id)[field])

    def _verified(
        self, problem: StoredProblem, artifacts: PlanningArtifacts | None
    ) -> tuple[dict[str, Any], object | None]:
        """Re-verify the problem's signed inputs now; plan provenance fields and `k_lookup`.

        TF-003's `resolve_with_overrides` and `insert_rush_order` take no `k_lookup`; the
        K-table is still verified and recorded for them.
        """
        resolver = artifacts or _LOCAL_ARTIFACTS
        provenance, k_lookup = resolver.for_solve(
            problem.provenance, problem.profile, problem.request.profile
        )
        return provenance.record_fields(), k_lookup

    def _api(self) -> PortfolioApi:
        if self._portfolio is None:
            raise CapabilityUnavailable("Portfolio planning (TF-003) is not installed")
        return self._portfolio

    def _budget(self, problem: StoredProblem, requested: float | None) -> float:
        limits = [problem.request.profile.time_budget_seconds, self._max_solve_seconds]
        if requested is not None:
            limits.append(requested)
        return min(limits)

    def _problem(self, problem_id: str) -> StoredProblem:
        return StoredProblem.from_record(self._get("problems", problem_id))

    def _get(self, kind: str, record_id: str) -> dict[str, Any]:
        try:
            return self._repository.get(kind, record_id)
        except KeyError:
            raise NotFound(f"Unknown {kind} id") from None

    def _exists(self, kind: str, record_id: str) -> bool:
        try:
            self._repository.get(kind, record_id)
        except KeyError:
            return False
        return True

    def _create_once(self, kind: str, record_id: str, body: dict[str, Any]) -> None:
        """Store a content-addressed record unless it already exists (race-safe)."""
        if self._exists(kind, record_id):
            return
        try:
            self._repository.create(kind, record_id, body)
        except ValueError:
            if not self._exists(kind, record_id):
                raise  # the quota, not a concurrent identical create
