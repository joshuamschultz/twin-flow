"""Plan request reconstruction and re-verification (T-186, COMP-139).

Requirements: REQ-127 (the shop is built from the compiled problem the plan was verified
against), REQ-140 (re-verify against the reconstructed request immediately before
simulating; `blocked` / `plan_unverified` otherwise), REQ-143 (a plan whose assignments no
longer verify is refused). Decisions: D-087 (feasibility only, `primary_value` stripped),
TF-003 COMP-029 (request rules mirrored).

Contract pinned here (the implementation follows these tests):

    from twinflow.application.plan_source import (
        MAX_REVERIFY_ISSUES, PlanCheck, ReconstructedPlan, check_plan, reconstruct, reverify,
    )

* `reconstruct(repository, plan_id) -> ReconstructedPlan`. The plan is looked up by id in
  the `plans` kind (ERP), then in `schedule_plans` (scenario); the kind it is stored under
  decides how it is rebuilt, never a field inside the record. Unknown id -> `NotFound`.
  - ERP `solve_portfolio` plan: `request` = the stored problem's request.
  - ERP `resolve_with_overrides` plan: the problem's request with `previous_plan` = the
    `PreviousPlan` of the origin's `previous_plan_id` record and the origin `overrides`
    appended to the request's overrides.
  - ERP `insert_rush_order` plan: the problem's request with `previous_plan` set as above
    and the rush job rebuilt with `erp.build.build_rush_job(snapshot, problem profile,
    origin rush_order)` appended to the problem's jobs.
  - Scenario plan: `schedule_problem.build_request(scenario, schedule snapshot).request`;
    when its digest differs from the record's `problem_digest`, a `PlanningError` with
    code `schedule_source_changed`.
* `ReconstructedPlan` (frozen): `plan_id`, `source_kind` ("erp" | "scenario"),
  `problem_id` (the request's problem id), `problem_digest`, `request` (SolveRequest),
  `compiled` (= `scheduling.apply_overrides(request)`), `schedule` (the stored schedule with
  its assignments and `primary_value = None`).
* `reverify(plan) -> tuple[ScheduleIssue, ...]`: `scheduling.verify(compiled,
  request.overrides, schedule, request.previous_plan)`; `()` when valid, else the first
  `MAX_REVERIFY_ISSUES` (50) verifier issues.
* `check_plan(repository, plan_id) -> PlanCheck{status: "verified" | "blocked", plan:
  ReconstructedPlan | None, issues: tuple[ScheduleIssue, ...]}` is the one gate the
  simulate tools call. Verified: `plan` set, no issues. Source changed: blocked with the
  single issue `schedule_source_changed`. Any other failure (verifier issues, a record that
  cannot be rebuilt: unknown origin method, missing problem, edited origin) is blocked with
  `plan_unverified` first, then the verifier issues (at most 50). Unknown plan id still
  raises `NotFound`. Pure read: writes no record.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _fake_portfolio import FakePortfolioApi, tiny_profile, tiny_shop
from _simulation_world import (
    erp_profile,
    real_portfolio_api,
    scenario_plan,
    small_shop,
    small_shop_hold,
    small_shop_rush,
)

from twinflow.application.planning import NotFound, PlanningError, PlanningService
from twinflow.application.portfolio_port import PortfolioApi
from twinflow.application.schedule_problem import build_request
from twinflow.application.workspace import Workspace
from twinflow.erp.build import build_problem_from_erp, build_rush_job
from twinflow.scheduling import (
    PreviousAssignment,
    PreviousPlan,
    Schedule,
    ScheduleIssue,
    SolveRequest,
    apply_overrides,
    verify,
)

CALLER = "arc"
RECORD_KINDS = ("problems", "plans", "portfolios", "schedule_plans", "simulations", "evaluations")


def _plan_source() -> Any:
    """The module under test, imported lazily so each test fails RED on its own."""
    from twinflow.application import plan_source

    return plan_source


# --- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    ws = Workspace(tmp_path)
    yield ws
    ws.close()


@pytest.fixture
def fake() -> FakePortfolioApi:
    return FakePortfolioApi()


@pytest.fixture
def service(workspace: Workspace, fake: FakePortfolioApi) -> Iterator[PlanningService]:
    planning = PlanningService(workspace, portfolio=fake)
    yield planning
    planning.close()


@pytest.fixture
def built(service: PlanningService) -> Any:
    record = service.build_problem(small_shop(), erp_profile())
    assert record.status == "ready", record.issues
    return record


@pytest.fixture
def solved_plan(service: PlanningService, built: Any) -> str:
    """The recommended (verified) `solve_portfolio` plan of the small shop."""
    portfolio = service.solve_portfolio(built.problem_id, request_key="solve", caller_id=CALLER)
    assert portfolio.recommended_plan_id is not None
    return str(portfolio.recommended_plan_id)


@pytest.fixture
def resolved_plan(service: PlanningService, solved_plan: str) -> str:
    revised = service.resolve_with_overrides(
        solved_plan, (small_shop_hold(),), request_key="resolve", caller_id=CALLER
    )
    assert revised.recommended_plan_id is not None
    return str(revised.recommended_plan_id)


@pytest.fixture
def rush_plan(service: PlanningService, solved_plan: str) -> str:
    rush = service.insert_rush_order(
        solved_plan, small_shop_rush(), request_key="rush", caller_id=CALLER
    )
    assert rush.verified is True
    return str(rush.plan_id)


@pytest.fixture
def scenario_plan_id(workspace: Workspace) -> str:
    return scenario_plan(workspace)


ORIGINS = ("solved_plan", "resolved_plan", "rush_plan", "scenario_plan_id")
ERP_ORIGINS = ORIGINS[:3]


def _plan_of(request: pytest.FixtureRequest, origin: str) -> str:
    return str(request.getfixturevalue(origin))


def _stored_schedule(record: dict[str, Any]) -> Schedule:
    """The schedule as stored on an ERP plan record (assignments carry an extra `job_id`)."""
    assignments = [{k: v for k, v in a.items() if k != "job_id"} for a in record["assignments"]]
    return Schedule.model_validate({**record["schedule"], "assignments": assignments})


def _previous_plan(record: dict[str, Any]) -> PreviousPlan:
    return PreviousPlan(
        plan_id=str(record["id"]),
        problem_digest=str(record["problem_digest"]),
        assignments=tuple(
            PreviousAssignment(
                operation_id=a["operation_id"], machine_id=a["machine_id"], start=a["start"]
            )
            for a in record["assignments"]
        ),
    )


def _counts(workspace: Workspace) -> dict[str, int]:
    return {kind: workspace.repository.count(kind) for kind in RECORD_KINDS}


# --- reconstruct: one request per origin --------------------------------------------------------


def test_reconstruct_solve_portfolio_plan_is_the_stored_problem_request(
    workspace: Workspace, solved_plan: str, built: Any
) -> None:
    plan = _plan_source().reconstruct(workspace.repository, solved_plan)

    expected = build_problem_from_erp(small_shop(), erp_profile()).request
    assert plan.request == expected
    assert plan.source_kind == "erp"
    assert plan.plan_id == solved_plan
    assert plan.problem_id == built.problem_id == expected.problem.id
    assert plan.problem_digest == built.problem_digest


def test_reconstruct_resolve_plan_is_the_request_tf003_verified_against(
    workspace: Workspace, resolved_plan: str, solved_plan: str, fake: FakePortfolioApi
) -> None:
    plan = _plan_source().reconstruct(workspace.repository, resolved_plan)

    assert fake.request is not None and fake.overrides is not None
    previous = _previous_plan(workspace.repository.get("plans", solved_plan))
    assert fake.previous_plan == previous
    expected = fake.request.model_copy(
        update={"previous_plan": previous, "overrides": fake.request.overrides + fake.overrides}
    )
    assert plan.request == expected
    assert plan.request.overrides[-1] == small_shop_hold()


def test_reconstruct_rush_plan_rebuilds_the_rush_job_from_the_snapshot(
    workspace: Workspace, rush_plan: str, solved_plan: str, fake: FakePortfolioApi
) -> None:
    plan = _plan_source().reconstruct(workspace.repository, rush_plan)

    rush_job = build_rush_job(small_shop(), erp_profile(), small_shop_rush())
    assert not isinstance(rush_job, tuple), rush_job
    assert fake.request is not None and fake.rush_job == rush_job
    previous = _previous_plan(workspace.repository.get("plans", solved_plan))
    problem = fake.request.problem.model_copy(
        update={"jobs": (*fake.request.problem.jobs, rush_job)}
    )
    expected = fake.request.model_copy(update={"problem": problem, "previous_plan": previous})
    assert plan.request == expected
    assert plan.request.problem.jobs[-1] == rush_job


def test_reconstruct_scenario_plan_is_the_digest_checked_build_request(
    workspace: Workspace, scenario_plan_id: str
) -> None:
    record = workspace.repository.get("schedule_plans", scenario_plan_id)
    scenario = workspace.repository.get("scenarios", record["scenario_id"])
    snapshot = workspace.repository.get("schedule_snapshots", record["schedule_snapshot_id"])
    translation = build_request(scenario, snapshot)
    assert translation.request is not None

    plan = _plan_source().reconstruct(workspace.repository, scenario_plan_id)

    assert plan.source_kind == "scenario"
    assert plan.request == translation.request
    assert plan.problem_digest == record["problem_digest"] == translation.problem_digest
    assert plan.problem_id == translation.request.problem.id
    stored = Schedule.model_validate(record["result"]["schedule"])
    assert plan.schedule.assignments == stored.assignments


@pytest.mark.parametrize("origin", ERP_ORIGINS)
def test_reconstructed_erp_schedule_is_the_stored_schedule_without_primary_value(
    workspace: Workspace, request: pytest.FixtureRequest, origin: str
) -> None:
    plan_id = _plan_of(request, origin)
    stored = _stored_schedule(workspace.repository.get("plans", plan_id))

    plan = _plan_source().reconstruct(workspace.repository, plan_id)

    assert plan.schedule.assignments == stored.assignments
    assert plan.schedule.primary_value is None
    assert plan.schedule == stored.model_copy(update={"primary_value": None})


@pytest.mark.parametrize("origin", ORIGINS)
def test_reconstructed_compiled_problem_is_apply_overrides_of_the_request(
    workspace: Workspace, request: pytest.FixtureRequest, origin: str
) -> None:
    plan = _plan_source().reconstruct(workspace.repository, _plan_of(request, origin))

    assert isinstance(plan.request, SolveRequest)
    assert plan.compiled == apply_overrides(plan.request)


def test_reconstruct_unknown_plan_id_is_not_found(workspace: Workspace) -> None:
    with pytest.raises(NotFound):
        _plan_source().reconstruct(workspace.repository, "nope")


# --- reverify / check_plan on honest records ----------------------------------------------------


@pytest.mark.parametrize("origin", ORIGINS)
def test_every_origin_reverifies_with_primary_value_stripped(
    workspace: Workspace, request: pytest.FixtureRequest, origin: str
) -> None:
    source = _plan_source()
    plan_id = _plan_of(request, origin)

    plan = source.reconstruct(workspace.repository, plan_id)
    checked = source.check_plan(workspace.repository, plan_id)

    assert source.reverify(plan) == ()
    assert checked.status == "verified"
    assert checked.issues == ()
    assert checked.plan == plan


def test_reverify_ignores_a_primary_value_another_objective_would_disagree_with(
    workspace: Workspace, solved_plan: str
) -> None:
    """D-087: feasibility only; a stored primary value from TF-003's objective is not checked."""
    record = workspace.repository.get("plans", solved_plan)
    record["schedule"]["primary_value"] = 123_456_789.0
    workspace.repository.update("plans", solved_plan, record)

    checked = _plan_source().check_plan(workspace.repository, solved_plan)

    assert checked.status == "verified", checked.issues
    assert checked.plan.schedule.primary_value is None


def test_check_plan_unknown_plan_id_is_not_found(workspace: Workspace) -> None:
    with pytest.raises(NotFound):
        _plan_source().check_plan(workspace.repository, "nope")


def test_check_plan_writes_no_record(
    workspace: Workspace, solved_plan: str, scenario_plan_id: str
) -> None:
    before = _counts(workspace)

    _plan_source().check_plan(workspace.repository, solved_plan)
    _plan_source().check_plan(workspace.repository, scenario_plan_id)

    assert _counts(workspace) == before


# --- tampered records ---------------------------------------------------------------------------


def _options(workspace: Workspace, record: dict[str, Any]) -> dict[str, set[str]]:
    request = SolveRequest.model_validate(
        workspace.repository.get("problems", record["problem_id"])["request"]
    )
    return {
        op.id: {option.machine_id for option in op.options}
        for job in request.problem.jobs
        for op in job.operations
    }


def _swap_machine(workspace: Workspace, record: dict[str, Any]) -> None:
    options = _options(workspace, record)
    machines = {m for found in options.values() for m in found}
    target = record["assignments"][0]
    target["machine_id"] = sorted(machines - options[target["operation_id"]])[0]


def _move_into_another_op(workspace: Workspace, record: dict[str, Any]) -> None:
    by_machine: dict[str, list[dict[str, Any]]] = {}
    for assignment in record["assignments"]:
        by_machine.setdefault(assignment["machine_id"], []).append(assignment)
    busiest = max(by_machine.values(), key=len)
    first, second = sorted(busiest, key=lambda a: a["start"])[:2]
    shift = first["start"] - second["start"] + 1.0
    for field in ("start", "setup_end", "end"):
        second[field] += shift


def _delete_op(workspace: Workspace, record: dict[str, Any]) -> None:
    del record["assignments"][len(record["assignments"]) // 2]


def _point_at_other_problem(workspace: Workspace, record: dict[str, Any]) -> None:
    service = PlanningService(workspace, portfolio=None)
    try:
        other = service.build_problem(tiny_shop(), tiny_profile())
    finally:
        service.close()
    assert other.problem_id is not None and other.problem_id != record["problem_id"]
    record["problem_id"] = other.problem_id


def _point_at_missing_problem(workspace: Workspace, record: dict[str, Any]) -> None:
    record["problem_id"] = "no-such-problem"


def _unknown_method(workspace: Workspace, record: dict[str, Any]) -> None:
    record["method"] = "hand_edited"


TAMPERS = {
    "machine_swapped": _swap_machine,
    "start_moved_into_another_op": _move_into_another_op,
    "op_deleted": _delete_op,
    "problem_id_points_at_another_problem": _point_at_other_problem,
    "problem_id_points_at_no_problem": _point_at_missing_problem,
    "origin_method_unknown": _unknown_method,
}
VERIFIER_TAMPERS = ("machine_swapped", "start_moved_into_another_op", "op_deleted")


def _tamper(workspace: Workspace, plan_id: str, name: str) -> None:
    record = workspace.repository.get("plans", plan_id)
    TAMPERS[name](workspace, record)
    workspace.repository.update("plans", plan_id, record)


def _assert_unverified(checked: Any) -> None:
    assert checked.status == "blocked"
    assert checked.plan is None
    assert checked.issues, "a blocked check must name why"
    assert all(isinstance(issue, ScheduleIssue) for issue in checked.issues)
    assert checked.issues[0].code == "plan_unverified"
    assert len(checked.issues) <= 51


@pytest.mark.parametrize("name", sorted(TAMPERS))
def test_tampered_record_is_blocked_plan_unverified(
    workspace: Workspace, solved_plan: str, name: str
) -> None:
    _tamper(workspace, solved_plan, name)

    _assert_unverified(_plan_source().check_plan(workspace.repository, solved_plan))


@pytest.mark.parametrize("name", VERIFIER_TAMPERS)
def test_tampered_assignments_carry_the_verifier_issues(
    workspace: Workspace, solved_plan: str, name: str
) -> None:
    source = _plan_source()
    _tamper(workspace, solved_plan, name)

    found = source.reverify(source.reconstruct(workspace.repository, solved_plan))
    checked = source.check_plan(workspace.repository, solved_plan)

    assert found, "the verifier must reject a tampered schedule"
    assert checked.issues[1:] == found


@pytest.mark.parametrize("origin", ["resolved_plan", "rush_plan"])
def test_tampered_record_of_every_erp_origin_is_blocked(
    workspace: Workspace, request: pytest.FixtureRequest, origin: str
) -> None:
    plan_id = _plan_of(request, origin)
    _tamper(workspace, plan_id, "op_deleted")

    _assert_unverified(_plan_source().check_plan(workspace.repository, plan_id))


def test_resolve_plan_with_its_overrides_edited_out_is_blocked(
    workspace: Workspace, resolved_plan: str
) -> None:
    """The hold removed MO-0003; without it the reconstructed request needs MO-0003 placed."""
    record = workspace.repository.get("plans", resolved_plan)
    record["overrides"] = []
    workspace.repository.update("plans", resolved_plan, record)

    _assert_unverified(_plan_source().check_plan(workspace.repository, resolved_plan))


def test_rush_plan_with_its_rush_order_edited_is_blocked(
    workspace: Workspace, rush_plan: str
) -> None:
    record = workspace.repository.get("plans", rush_plan)
    record["rush_order"] = {**record["rush_order"], "item_id": "SPR-004"}
    workspace.repository.update("plans", rush_plan, record)

    _assert_unverified(_plan_source().check_plan(workspace.repository, rush_plan))


def test_unverified_issues_are_the_first_50_verifier_issues(
    workspace: Workspace, solved_plan: str
) -> None:
    source = _plan_source()
    record = workspace.repository.get("plans", solved_plan)
    record["assignments"] = []
    workspace.repository.update("plans", solved_plan, record)
    plan = source.reconstruct(workspace.repository, solved_plan)
    everything = verify(
        plan.compiled, plan.request.overrides, plan.schedule, plan.request.previous_plan
    ).issues
    assert len(everything) > 50, "precondition: more issues than the cap"

    found = source.reverify(plan)
    checked = source.check_plan(workspace.repository, solved_plan)

    assert source.MAX_REVERIFY_ISSUES == 50
    assert found == tuple(everything[:50])
    assert len(checked.issues) == 51
    assert checked.issues[1:] == found
    assert Counter(issue.code for issue in found) == Counter(
        issue.code for issue in everything[:50]
    )


# --- scenario plans whose source changed ----------------------------------------------------------


def _edit_scenario_snapshot(workspace: Workspace, plan_id: str) -> None:
    record = workspace.repository.get("schedule_plans", plan_id)
    snapshot = workspace.repository.get("schedule_snapshots", record["schedule_snapshot_id"])
    snapshot["orders"][0]["quantity"] += 1
    workspace.repository.update("schedule_snapshots", snapshot["id"], snapshot)


def test_scenario_plan_whose_source_changed_is_blocked_schedule_source_changed(
    workspace: Workspace, scenario_plan_id: str
) -> None:
    _edit_scenario_snapshot(workspace, scenario_plan_id)

    checked = _plan_source().check_plan(workspace.repository, scenario_plan_id)

    assert checked.status == "blocked"
    assert checked.plan is None
    assert [issue.code for issue in checked.issues] == ["schedule_source_changed"]


def test_reconstruct_scenario_plan_whose_source_changed_raises_schedule_source_changed(
    workspace: Workspace, scenario_plan_id: str
) -> None:
    _edit_scenario_snapshot(workspace, scenario_plan_id)

    with pytest.raises(PlanningError) as raised:
        _plan_source().reconstruct(workspace.repository, scenario_plan_id)

    assert raised.value.code == "schedule_source_changed"


def test_scenario_plan_with_tampered_assignments_is_blocked_plan_unverified(
    workspace: Workspace, scenario_plan_id: str
) -> None:
    record = workspace.repository.get("schedule_plans", scenario_plan_id)
    assignments = record["result"]["schedule"]["assignments"]
    assert assignments
    del assignments[0]
    workspace.repository.update("schedule_plans", scenario_plan_id, record)

    _assert_unverified(_plan_source().check_plan(workspace.repository, scenario_plan_id))


# --- with TF-003 installed: the real re-plans re-verify -------------------------------------------


def test_real_tf003_resolve_and_rush_plans_reverify(tmp_path: Path) -> None:
    api: PortfolioApi = real_portfolio_api()
    workspace = Workspace(tmp_path)
    service = PlanningService(workspace, portfolio=api)
    try:
        built = service.build_problem(small_shop(), erp_profile())
        assert built.problem_id is not None
        portfolio = service.solve_portfolio(
            built.problem_id, request_key="solve", caller_id=CALLER, time_budget_seconds=10.0
        )
        assert portfolio.recommended_plan_id is not None
        base = portfolio.recommended_plan_id
        revised = service.resolve_with_overrides(
            base, (small_shop_hold(),), request_key="r", caller_id=CALLER, time_budget_seconds=10.0
        )
        rush = service.insert_rush_order(
            base, small_shop_rush(), request_key="i", caller_id=CALLER, time_budget_seconds=10.0
        )
        verified = [s.plan_id for s in (*portfolio.statuses, *revised.statuses) if s.verified]
        if rush.verified:
            verified.append(rush.plan_id)
        assert any(s.plan_id in verified for s in revised.statuses), "no verified re-plan"

        for plan_id in verified:
            checked = _plan_source().check_plan(workspace.repository, plan_id)
            assert checked.status == "verified", (plan_id, checked.issues[:5])
    finally:
        service.close()
        workspace.close()
