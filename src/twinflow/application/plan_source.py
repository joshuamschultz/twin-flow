"""Plan request reconstruction and re-verification (COMP-139; REQ-127, REQ-140, REQ-143).

A stored plan record can be edited, so the simulator never trusts its `verified` flag
(D-087). `reconstruct` rebuilds the exact `SolveRequest` the plan was verified against,
mirroring TF-003 COMP-029 from the plan's stored origin, and `check_plan` re-verifies the
stored assignments against it, feasibility only (`primary_value` stripped: TF-003's
effective objective may differ from the profile's).

* The kind the plan is stored under decides how it is rebuilt: `plans` (ERP) by its origin
  `method`, `schedule_plans` (scenario) by the digest-checked `build_request`.
* Any record that cannot be rebuilt (unknown method, missing problem or snapshot, edited
  origin) is `plan_unverified`, like a schedule the verifier rejects. A scenario whose
  source no longer translates to the same problem is `schedule_source_changed`.

Pure reads: nothing here writes a record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import TypeAdapter

from twinflow.application._planning_models import NotFound, PlanningError
from twinflow.application._planning_records import StoredProblem, previous_plan
from twinflow.application.repository import WorkspaceRepository
from twinflow.application.schedule_problem import build_request
from twinflow.erp.build import RushOrder, build_rush_job
from twinflow.erp.schema import ErpSnapshot
from twinflow.scheduling import (
    CompiledProblem,
    Job,
    Override,
    Schedule,
    ScheduleIssue,
    SolveRequest,
    apply_overrides,
    verify,
)

__all__ = [
    "MAX_REVERIFY_ISSUES",
    "PlanCheck",
    "ReconstructedPlan",
    "check_plan",
    "reconstruct",
    "reverify",
]

MAX_REVERIFY_ISSUES = 50
SourceKind = Literal["erp", "scenario"]

_OVERRIDES = TypeAdapter(tuple[Override, ...])


@dataclass(frozen=True)
class ReconstructedPlan:
    """A stored plan with the request it was verified against and its compiled problem."""

    plan_id: str
    source_kind: SourceKind
    problem_id: str
    problem_digest: str
    request: SolveRequest
    compiled: CompiledProblem
    schedule: Schedule


@dataclass(frozen=True)
class PlanCheck:
    """`verified` with the rebuilt plan, or `blocked` with the issues that say why."""

    status: Literal["verified", "blocked"]
    plan: ReconstructedPlan | None
    issues: tuple[ScheduleIssue, ...]


class _Unrebuildable(PlanningError):
    """The stored record cannot be turned back into the request it was verified against."""

    code = "plan_unverified"


def reconstruct(repository: WorkspaceRepository, plan_id: str) -> ReconstructedPlan:
    """The plan `plan_id` with its reconstructed request; `NotFound` for an unknown id.

    Raises `PlanningError` `schedule_source_changed` for a scenario plan whose source
    changed, and `plan_unverified` for a record that cannot be rebuilt.
    """
    kind, record = _plan_record(repository, plan_id)
    return _rebuild(repository, kind, record)


def reverify(plan: ReconstructedPlan) -> tuple[ScheduleIssue, ...]:
    """`()` when the schedule verifies against the request, else the first 50 issues."""
    request = plan.request
    verdict = verify(plan.compiled, request.overrides, plan.schedule, request.previous_plan)
    return () if verdict.valid else tuple(verdict.issues[:MAX_REVERIFY_ISSUES])


def check_plan(repository: WorkspaceRepository, plan_id: str) -> PlanCheck:
    """The one gate the simulate tools call before simulating (D-087); writes nothing."""
    kind, record = _plan_record(repository, plan_id)
    try:
        plan = _rebuild(repository, kind, record)
    except _Unrebuildable:
        return _blocked(_unverified(plan_id))
    except PlanningError as exc:
        if exc.code != "schedule_source_changed":
            raise
        return _blocked(_issue(exc.code, str(exc)))
    issues = reverify(plan)
    if issues:
        return _blocked(_unverified(plan_id), *issues)
    return PlanCheck(status="verified", plan=plan, issues=())


# --- lookup ------------------------------------------------------------------------------------


def _plan_record(repository: WorkspaceRepository, plan_id: str) -> tuple[str, dict[str, Any]]:
    """`(kind, record)`: the ERP `plans` kind first, then scenario `schedule_plans`."""
    for kind in ("plans", "schedule_plans"):
        try:
            return kind, repository.get(kind, plan_id)
        except KeyError:
            continue
    raise NotFound("Unknown plan id")


def _rebuild(
    repository: WorkspaceRepository, kind: str, record: dict[str, Any]
) -> ReconstructedPlan:
    """Rebuild by stored kind; malformed or dangling records become `_Unrebuildable`."""
    try:
        if kind == "plans":
            return _erp_plan(repository, record)
        return _scenario_plan(repository, record)
    except (KeyError, ValueError, TypeError) as exc:  # pydantic errors are ValueErrors
        raise _Unrebuildable("The plan record cannot be rebuilt") from exc


# --- ERP plans -----------------------------------------------------------------------------------


def _erp_plan(repository: WorkspaceRepository, record: dict[str, Any]) -> ReconstructedPlan:
    problem = StoredProblem.from_record(repository.get("problems", record["problem_id"]))
    if record["problem_digest"] != problem.problem_digest:
        raise _Unrebuildable("The plan names another revision of its problem")
    request = _erp_request(repository, record, problem)
    return ReconstructedPlan(
        plan_id=str(record["id"]),
        source_kind="erp",
        problem_id=request.problem.id,
        problem_digest=problem.problem_digest,
        request=request,
        compiled=apply_overrides(request),
        schedule=_erp_schedule(record),
    )


def _erp_request(
    repository: WorkspaceRepository, record: Mapping[str, Any], problem: StoredProblem
) -> SolveRequest:
    """The request TF-003 verified the plan against, by its origin method (COMP-029)."""
    method = record["method"]
    request = problem.request
    if method == "solve_portfolio":
        return request
    previous = previous_plan(repository.get("plans", record["previous_plan_id"]))
    if method == "resolve_with_overrides":
        overrides = _OVERRIDES.validate_python(record["overrides"])
        return request.model_copy(
            update={"previous_plan": previous, "overrides": request.overrides + overrides}
        )
    if method == "insert_rush_order":
        rush_problem = request.problem.model_copy(
            update={"jobs": (*request.problem.jobs, _rush_job(repository, record, problem))}
        )
        return request.model_copy(update={"problem": rush_problem, "previous_plan": previous})
    raise _Unrebuildable("The plan's origin method is unknown")


def _rush_job(
    repository: WorkspaceRepository, record: Mapping[str, Any], problem: StoredProblem
) -> Job:
    """The rush job rebuilt from the problem's snapshot and the origin rush order."""
    snapshot = ErpSnapshot.model_validate(
        repository.get("erp_snapshots", problem.snapshot_digest)["snapshot"]
    )
    rush_order = RushOrder.model_validate(record["rush_order"])
    job = build_rush_job(snapshot, problem.profile, rush_order)
    if isinstance(job, tuple):
        raise _Unrebuildable("The rush order no longer becomes a job")
    return job


def _erp_schedule(record: Mapping[str, Any]) -> Schedule:
    """The stored schedule; assignments drop their stored `job_id`, `primary_value` None."""
    assignments = [{k: v for k, v in row.items() if k != "job_id"} for row in record["assignments"]]
    stored = {**record["schedule"], "assignments": assignments, "primary_value": None}
    return Schedule.model_validate(stored)


# --- scenario plans ------------------------------------------------------------------------------


def _scenario_plan(repository: WorkspaceRepository, record: dict[str, Any]) -> ReconstructedPlan:
    scenario = repository.get("scenarios", record["scenario_id"])
    snapshot = repository.get("schedule_snapshots", record["schedule_snapshot_id"])
    translation = build_request(scenario, snapshot)
    request, digest = translation.request, translation.problem_digest
    if request is None or digest is None or digest != record["problem_digest"]:
        raise PlanningError(
            "Schedule source changed since the plan was solved", code="schedule_source_changed"
        )
    stored = {**record["result"]["schedule"], "primary_value": None}
    return ReconstructedPlan(
        plan_id=str(record["id"]),
        source_kind="scenario",
        problem_id=request.problem.id,
        problem_digest=digest,
        request=request,
        compiled=apply_overrides(request),
        schedule=Schedule.model_validate(stored),
    )


# --- issues --------------------------------------------------------------------------------------


def _issue(code: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path="$.plan_id", message=message)


def _unverified(plan_id: str) -> ScheduleIssue:
    return _issue(
        "plan_unverified", f"plan {plan_id} does not verify against its reconstructed request"
    )


def _blocked(*issues: ScheduleIssue) -> PlanCheck:
    return PlanCheck(status="blocked", plan=None, issues=issues)
