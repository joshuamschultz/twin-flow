"""Planning service: persistence, idempotency, TF-003 wrappers, reads, compare, simulate.

Tasks T-205 and T-206 (COMP-117). Requirements: REQ-159, REQ-163, REQ-167, REQ-168, REQ-169,
REQ-171, REQ-177. Decisions: D-119 (plans by reference), D-120 (idempotency, capped budget).

Contract pinned here (the implementation follows these tests):

    from twinflow.application.planning import (
        PlanningService, PlanningError, NotFound, IdempotencyConflict, PlanningTimeout,
        Blocked, SizeLimit, CapabilityUnavailable,
    )

* `PlanningService(workspace, *, portfolio: PortfolioApi | None, max_solve_seconds=120.0,
  timeout_grace_seconds=15.0, max_workers=4)`.
* `PlanningError(Exception)` has `.code`; subclasses and codes: NotFound "not_found",
  IdempotencyConflict "idempotency_conflict", PlanningTimeout "timeout", Blocked "blocked"
  (with `.issues`, a tuple of `ErpIssue`), SizeLimit "size_limit", CapabilityUnavailable
  "capability_unavailable". `compare_plans` over two problem digests raises a plain
  `PlanningError` with code "mixed_problems".
* Records (workspace repository kinds): "erp_snapshots" id = snapshot digest (body has the
  validation report); "problems" id = problem_id (body has `problem_digest`, `request` (the
  SolveRequest JSON), `lineage_index`, the planning profile); "portfolios" id = portfolio id;
  "plans" id = f"{portfolio_id}:{candidate_id}" (one per candidate) or a new id for a rush
  plan, each with `problem_id`, `problem_digest`, `snapshot_digest`, `source` "erp",
  `validity` "provisional", `evidence_kind`, `assignments`, `objective_values`, `verified`,
  `issues`. A plan from `resolve_with_overrides` or `insert_rush_order` keeps the source
  plan's `problem_id` and `problem_digest` (the overrides/rush are inputs, not a new problem).
* Results are pydantic models with the attributes used below. Nested rows (page assignments,
  menu entries, compare rows, simulate result) are read through `_row`, so a model or a
  plain dict both satisfy the contract; the CONTENT is pinned, not the container.
* Idempotency key = caller_id + ":" + request_key, claimed with the SHA-256 of the canonical
  JSON of the method name and inputs. A caller id may not be able to forge another caller's
  key (see the ":" collision test).
* Budget passed to TF-003 = min(requested if given, the stored problem's
  `profile.time_budget_seconds`, `max_solve_seconds`); requested <= 0 or not finite ->
  ValueError. Wall-clock limit = budget + `timeout_grace_seconds` -> PlanningTimeout.
* `get_plan(plan_id, *, offset=0, limit=100, job_id=None)`: limit outside 1..1000 or
  offset < 0 -> ValueError; assignments in stored order, each with its `lineage` from the
  problem's lineage index.
* `simulate_plan(plan_id, *, request_key, caller_id, reps=1, seed=0, wait_seconds=0)`
  returns a result with `status`, `issues`, `job` (the jobs record or None), `validity`,
  `evidence_kind`. reps outside 1..10 or wait_seconds outside 0..120 -> ValueError.
"""

from __future__ import annotations

import base64
import math
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _fake_portfolio import (
    FakePortfolioApi,
    tiny_profile,
    tiny_shop,
)
from pydantic import BaseModel

from twinflow.application.linear_draft import LinearDraft
from twinflow.application.planning import (
    Blocked,
    CapabilityUnavailable,
    IdempotencyConflict,
    NotFound,
    PlanningError,
    PlanningService,
    PlanningTimeout,
    SizeLimit,
)
from twinflow.application.portfolio_port import load_portfolio_api
from twinflow.application.workspace import Workspace
from twinflow.erp.build import RushOrder, build_problem_from_erp, build_rush_job
from twinflow.erp.digest import snapshot_digest
from twinflow.erp.profile import profile_digest
from twinflow.erp.schema import ErpSnapshot
from twinflow.scheduling import HoldOverride, SolveRequest, rank

CALLER = "arc"
PLAN_KINDS = ("erp_snapshots", "problems", "portfolios", "plans")
ALL_KINDS = (*PLAN_KINDS, "jobs")
HOLD_PO2 = HoldOverride(
    id="hold-po2", fact_ref="operator", reason="customer hold", hardness="hard", job_id="PO-2"
)


# --- helpers -----------------------------------------------------------------------------------


def _row(value: Any) -> dict[str, Any]:
    """A nested result row as JSON, whether the service returns a model or a dict."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    assert isinstance(value, dict), type(value)
    return value


def _counts(workspace: Workspace) -> dict[str, int]:
    return {kind: workspace.repository.count(kind) for kind in ALL_KINDS}


def _rush(**changes: object) -> RushOrder:
    data: dict[str, object] = {
        "order_id": "RUSH-1",
        "item_id": "WIDGET",
        "quantity": 2.0,
        "due": tiny_shop().as_of.replace(hour=18),
        "customer_id": "CUST-A",
        "routing_id": None,
    }
    data.update(changes)
    return RushOrder.model_validate(data)


def _solve(service: PlanningService, problem_id: str, key: str, **options: Any) -> Any:
    return service.solve_portfolio(problem_id, request_key=key, caller_id=CALLER, **options)


def _assert_provisional(result: Any) -> None:
    body = _row(result)
    assert body["validity"] == "provisional"
    assert isinstance(body["evidence_kind"], str)
    assert body["evidence_kind"]


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
def service(workspace: Workspace, fake: FakePortfolioApi) -> PlanningService:
    return PlanningService(workspace, portfolio=fake)


@pytest.fixture
def shop() -> ErpSnapshot:
    return tiny_shop()


@pytest.fixture
def built(service: PlanningService, shop: ErpSnapshot) -> Any:
    record = service.build_problem(shop, tiny_profile())
    assert record.status == "ready", record.issues
    return record


@pytest.fixture
def portfolio(service: PlanningService, built: Any) -> Any:
    return _solve(service, built.problem_id, "solve-1")


@pytest.fixture
def plan_id(portfolio: Any) -> str:
    return str(portfolio.plan_ids[0])


# --- errors --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NotFound, "not_found"),
        (IdempotencyConflict, "idempotency_conflict"),
        (PlanningTimeout, "timeout"),
        (Blocked, "blocked"),
        (SizeLimit, "size_limit"),
        (CapabilityUnavailable, "capability_unavailable"),
    ],
)
def test_planning_errors_are_planning_errors_with_stable_codes(
    error: type[PlanningError], code: str
) -> None:
    assert issubclass(error, PlanningError)
    assert issubclass(PlanningError, Exception)
    assert error.code == code


# --- validate_snapshot ---------------------------------------------------------------------------


def test_validate_snapshot_reports_hard_issue_and_writes_no_record(
    service: PlanningService, workspace: Workspace
) -> None:
    report = service.validate_snapshot(tiny_shop(bad_work_center=True), tiny_profile())

    assert report.hard_count >= 1
    assert "unknown_work_center" in {issue.code for issue in report.issues}
    _assert_provisional(report)
    assert _counts(workspace) == dict.fromkeys(ALL_KINDS, 0)


# --- build_problem (T-205) -----------------------------------------------------------------------


def test_build_problem_ready_record_matches_the_builder(built: Any, shop: ErpSnapshot) -> None:
    expected = build_problem_from_erp(shop, tiny_profile())

    assert built.status == "ready"
    assert built.problem_id == expected.request.problem.id
    assert built.problem_id.startswith("erp-")
    assert built.problem_digest == expected.problem_digest
    assert built.snapshot_digest == snapshot_digest(shop)
    assert built.profile_digest == profile_digest(tiny_profile())
    assert built.builder_version == "erp-build-1"
    assert dict(built.counts) == dict(expected.counts)
    assert built.issue_total == len(expected.issues)
    assert built.validity == "provisional"
    assert built.evidence_kind == "erp_build"


def test_build_problem_persists_snapshot_and_problem_records(
    built: Any, workspace: Workspace, shop: ErpSnapshot
) -> None:
    expected = build_problem_from_erp(shop, tiny_profile())
    repository = workspace.repository

    assert repository.count("erp_snapshots") == 1
    assert repository.count("problems") == 1
    repository.get("erp_snapshots", snapshot_digest(shop))
    problem = repository.get("problems", built.problem_id)
    assert problem["problem_digest"] == expected.problem_digest
    assert SolveRequest.model_validate(problem["request"]) == expected.request
    assert set(problem["lineage_index"]) == set(expected.lineage_index)


def test_build_problem_repeat_returns_same_ids_and_writes_nothing(
    service: PlanningService, built: Any, workspace: Workspace, shop: ErpSnapshot
) -> None:
    before = _counts(workspace)

    again = service.build_problem(shop, tiny_profile())

    assert again.problem_id == built.problem_id
    assert again.problem_digest == built.problem_digest
    assert again.snapshot_digest == built.snapshot_digest
    assert _counts(workspace) == before


def test_build_problem_other_profile_is_a_new_problem_on_the_same_snapshot(
    service: PlanningService, built: Any, workspace: Workspace, shop: ErpSnapshot
) -> None:
    other = service.build_problem(shop, tiny_profile(time_budget_seconds=20.0))

    assert other.problem_id != built.problem_id
    assert workspace.repository.count("problems") == 2
    assert workspace.repository.count("erp_snapshots") == 1


def test_build_problem_blocked_persists_snapshot_report_but_no_problem(
    service: PlanningService, workspace: Workspace
) -> None:
    bad = tiny_shop(bad_work_center=True)

    record = service.build_problem(bad, tiny_profile())

    assert record.status == "blocked"
    assert record.problem_id is None
    assert record.problem_digest is None
    assert "unknown_work_center" in {issue.code for issue in record.issues}
    assert workspace.repository.count("erp_snapshots") == 1
    assert workspace.repository.count("problems") == 0
    stored = workspace.repository.get("erp_snapshots", snapshot_digest(bad))
    assert "unknown_work_center" in str(stored)
    _assert_provisional(record)


def test_build_problem_reports_first_500_issues_and_the_total(
    service: PlanningService, shop: ErpSnapshot
) -> None:
    template = tiny_shop(bad_work_center=True).production_orders[1]
    orders = tuple(template.model_copy(update={"id": f"BAD-{n:04d}"}) for n in range(600))
    flood = shop.model_copy(update={"production_orders": orders, "digest": None})

    record = service.build_problem(flood, tiny_profile())

    assert record.status == "blocked"
    assert record.issue_total >= 600
    assert len(record.issues) == 500


# --- solve_portfolio ----------------------------------------------------------------------------


def test_solve_portfolio_returns_plan_ids_per_candidate_and_recommended(portfolio: Any) -> None:
    pid = portfolio.portfolio_id

    assert list(portfolio.plan_ids) == [f"{pid}:00:baseline", f"{pid}:01:delayed"]
    assert portfolio.recommended_plan_id == f"{pid}:00:baseline"
    assert portfolio.menu_size == 2
    assert len(portfolio.statuses) == 2
    assert portfolio.validity == "provisional"
    assert portfolio.evidence_kind == "portfolio"


def test_solve_portfolio_resolves_problem_id_to_the_stored_solve_request(
    portfolio: Any, fake: FakePortfolioApi, shop: ErpSnapshot
) -> None:
    assert fake.calls["solve_portfolio"] == 1
    assert fake.request == build_problem_from_erp(shop, tiny_profile()).request


def test_solve_portfolio_stores_one_portfolio_and_one_plan_per_candidate(
    portfolio: Any, built: Any, workspace: Workspace
) -> None:
    repository = workspace.repository
    assert repository.count("portfolios") == 1
    assert repository.count("plans") == 2
    stored = repository.get("portfolios", portfolio.portfolio_id)
    assert stored["validity"] == "provisional"
    assert stored["evidence_kind"]
    for plan_id in portfolio.plan_ids:
        plan = repository.get("plans", plan_id)
        assert plan["problem_id"] == built.problem_id
        assert plan["problem_digest"] == built.problem_digest
        assert plan["snapshot_digest"] == built.snapshot_digest
        assert plan["source"] == "erp"
        assert plan["validity"] == "provisional"
        assert plan["evidence_kind"]
        assert {"assignments", "objective_values", "verified", "issues"} <= set(plan)


def test_solve_portfolio_plan_records_carry_the_candidate_schedule(
    portfolio: Any, fake: FakePortfolioApi, workspace: Workspace
) -> None:
    baseline = workspace.repository.get("plans", portfolio.plan_ids[0])
    delayed = workspace.repository.get("plans", portfolio.plan_ids[1])

    assert baseline["verified"] is True
    assert baseline["objective_values"]["makespan"] < delayed["objective_values"]["makespan"]
    assert [a["operation_id"] for a in baseline["assignments"]]
    assert len(baseline["assignments"]) == len(delayed["assignments"]) == 4


def test_solve_portfolio_unknown_problem_is_not_found_and_never_solves(
    service: PlanningService, fake: FakePortfolioApi
) -> None:
    with pytest.raises(NotFound):
        _solve(service, "erp-0000000000000000", "k")

    assert fake.calls["solve_portfolio"] == 0


# --- idempotency (T-205, REQ-167, D-120) ----------------------------------------------------------


@pytest.mark.parametrize("key", ["", None])
def test_solve_portfolio_requires_request_key(
    service: PlanningService, built: Any, fake: FakePortfolioApi, key: str | None
) -> None:
    with pytest.raises((ValueError, TypeError)):
        service.solve_portfolio(built.problem_id, request_key=key, caller_id=CALLER)  # type: ignore[arg-type]  # None on purpose

    assert fake.calls["solve_portfolio"] == 0


def test_solve_portfolio_same_caller_key_and_input_replays_without_solving(
    service: PlanningService,
    built: Any,
    portfolio: Any,
    fake: FakePortfolioApi,
    workspace: Workspace,
) -> None:
    before = _counts(workspace)

    again = _solve(service, built.problem_id, "solve-1")

    assert again == portfolio
    assert fake.calls["solve_portfolio"] == 1
    assert _counts(workspace) == before


def test_solve_portfolio_replay_survives_a_new_service_on_the_same_workspace(
    workspace: Workspace, built: Any, portfolio: Any
) -> None:
    fresh_fake = FakePortfolioApi()
    restarted = PlanningService(workspace, portfolio=fresh_fake)

    again = _solve(restarted, built.problem_id, "solve-1")

    assert again == portfolio
    assert fresh_fake.calls["solve_portfolio"] == 0


def test_solve_portfolio_same_key_different_input_is_conflict(
    service: PlanningService, built: Any, portfolio: Any, fake: FakePortfolioApi
) -> None:
    with pytest.raises(IdempotencyConflict) as raised:
        _solve(service, built.problem_id, "solve-1", seed=1)

    assert raised.value.code == "idempotency_conflict"
    assert fake.calls["solve_portfolio"] == 1


def test_same_key_reused_by_another_method_is_conflict(
    service: PlanningService, portfolio: Any, plan_id: str, fake: FakePortfolioApi
) -> None:
    with pytest.raises(IdempotencyConflict):
        service.resolve_with_overrides(
            plan_id, (HOLD_PO2,), request_key="solve-1", caller_id=CALLER
        )

    assert fake.calls["resolve_with_overrides"] == 0


def test_solve_portfolio_two_callers_same_key_never_share(
    service: PlanningService, built: Any, fake: FakePortfolioApi, workspace: Workspace
) -> None:
    first = service.solve_portfolio(built.problem_id, request_key="k", caller_id="alice")
    second = service.solve_portfolio(built.problem_id, request_key="k", caller_id="bob")

    assert fake.calls["solve_portfolio"] == 2
    assert first.portfolio_id != second.portfolio_id
    assert workspace.repository.count("portfolios") == 2


def test_caller_id_cannot_forge_another_callers_key_through_the_separator(
    service: PlanningService, built: Any, fake: FakePortfolioApi
) -> None:
    """`"alice:x" + ":" + "k"` == `"alice" + ":" + "x:k"`; REQ-167 says they never match."""
    first = service.solve_portfolio(built.problem_id, request_key="x:k", caller_id="alice")

    try:
        second = service.solve_portfolio(
            built.problem_id, request_key="k", caller_id="alice:x", seed=5
        )
    except ValueError:
        return  # refusing ":" in a caller id is an acceptable fix
    assert fake.calls["solve_portfolio"] == 2
    assert second.portfolio_id != first.portfolio_id


def test_solve_portfolio_concurrent_duplicates_solve_once(workspace: Workspace, built: Any) -> None:
    gate = threading.Event()
    gated = FakePortfolioApi(block=gate)
    service = PlanningService(workspace, portfolio=gated)
    barrier = threading.Barrier(4)
    results: list[Any] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def call() -> None:
        barrier.wait(5)
        try:
            outcome = _solve(service, built.problem_id, "race")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=call) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        assert gated.entered.wait(5), "no call reached the solver"
        time.sleep(0.3)  # the three duplicates arrive while the first is still solving
    finally:
        gate.set()
        for thread in threads:
            thread.join(15)

    assert errors == []
    assert len(results) == 4
    assert gated.calls["solve_portfolio"] == 1
    assert all(result == results[0] for result in results)
    assert workspace.repository.count("portfolios") == 1


# --- budget and timeout (REQ-169) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile_budget", "max_solve", "requested", "expected"),
    [
        (10.0, 120.0, None, 10.0),
        (10.0, 120.0, 5.0, 5.0),
        (10.0, 120.0, 50.0, 10.0),
        (10.0, 3.0, None, 3.0),
        (10.0, 3.0, 2.0, 2.0),
        (0.5, 120.0, 60.0, 0.5),
    ],
)
def test_solve_portfolio_budget_is_min_of_requested_profile_and_server_cap(
    workspace: Workspace,
    shop: ErpSnapshot,
    profile_budget: float,
    max_solve: float,
    requested: float | None,
    expected: float,
) -> None:
    spy = FakePortfolioApi()
    service = PlanningService(workspace, portfolio=spy, max_solve_seconds=max_solve)
    record = service.build_problem(shop, tiny_profile(time_budget_seconds=profile_budget))

    _solve(service, record.problem_id, "budget", time_budget_seconds=requested)

    assert spy.last_budget_seconds == pytest.approx(expected)


@pytest.mark.parametrize("requested", [0.0, -1.0, math.nan, math.inf])
def test_solve_portfolio_rejects_non_positive_or_non_finite_budget(
    service: PlanningService, built: Any, fake: FakePortfolioApi, requested: float
) -> None:
    with pytest.raises(ValueError):
        _solve(service, built.problem_id, "bad-budget", time_budget_seconds=requested)

    assert fake.calls["solve_portfolio"] == 0


def test_resolve_and_rush_pass_the_capped_budget(workspace: Workspace, shop: ErpSnapshot) -> None:
    spy = FakePortfolioApi()
    service = PlanningService(workspace, portfolio=spy, max_solve_seconds=3.0)
    record = service.build_problem(shop, tiny_profile(time_budget_seconds=10.0))
    plan_id = _solve(service, record.problem_id, "s").plan_ids[0]

    service.resolve_with_overrides(
        plan_id, (HOLD_PO2,), request_key="r", caller_id=CALLER, time_budget_seconds=60.0
    )
    resolve_budget = spy.last_budget_seconds
    service.insert_rush_order(
        plan_id, _rush(), request_key="i", caller_id=CALLER, time_budget_seconds=1.5
    )

    assert resolve_budget == pytest.approx(3.0)
    assert spy.last_budget_seconds == pytest.approx(1.5)


def test_solver_past_budget_and_grace_times_out_and_frees_the_worker(
    workspace: Workspace, shop: ErpSnapshot
) -> None:
    gate = threading.Event()
    slow = FakePortfolioApi(block=gate)
    service = PlanningService(
        workspace,
        portfolio=slow,
        max_solve_seconds=0.2,
        timeout_grace_seconds=0.1,
        max_workers=1,
    )
    record = service.build_problem(shop, tiny_profile())
    try:
        started = time.monotonic()
        with pytest.raises(PlanningTimeout) as raised:
            _solve(service, record.problem_id, "slow")
        elapsed = time.monotonic() - started
    finally:
        gate.set()

    assert raised.value.code == "timeout"
    assert elapsed < 1.5
    slow.block = None
    follow_up = _solve(service, record.problem_id, "after-timeout")
    assert len(follow_up.plan_ids) == 2


# --- TF-003 absent (capability_unavailable) ------------------------------------------------------


def test_tf003_absent_load_gives_none_and_solve_tools_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace, shop: ErpSnapshot
) -> None:
    monkeypatch.setitem(sys.modules, "twinflow.scheduling.portfolio.api", None)
    monkeypatch.setitem(sys.modules, "twinflow.scheduling.portfolio", None)
    service = PlanningService(workspace, portfolio=load_portfolio_api())
    record = service.build_problem(shop, tiny_profile())

    with pytest.raises(CapabilityUnavailable) as raised:
        _solve(service, record.problem_id, "k")

    assert raised.value.code == "capability_unavailable"
    assert record.status == "ready"
    assert workspace.repository.count("portfolios") == 0


def test_tf003_absent_every_wrapper_is_unavailable_but_reads_still_work(
    workspace: Workspace, built: Any, portfolio: Any, plan_id: str
) -> None:
    offline = PlanningService(workspace, portfolio=None)

    with pytest.raises(CapabilityUnavailable):
        offline.resolve_with_overrides(plan_id, (HOLD_PO2,), request_key="r", caller_id=CALLER)
    with pytest.raises(CapabilityUnavailable):
        offline.insert_rush_order(plan_id, _rush(), request_key="i", caller_id=CALLER)
    with pytest.raises(CapabilityUnavailable):
        offline.get_pareto_menu(portfolio.portfolio_id)
    page = offline.get_plan(plan_id)
    assert page.total == 4


def test_explain_plan_is_unavailable_until_b6_even_with_tf003(
    service: PlanningService, plan_id: str
) -> None:
    with pytest.raises(CapabilityUnavailable):
        service.explain_plan(plan_id)
    with pytest.raises(CapabilityUnavailable):
        service.explain_plan(plan_id, job_ids=("PO-1",))


# --- get_plan (REQ-168) ---------------------------------------------------------------------------


def test_get_plan_pages_with_offset_limit_and_next_offset(
    service: PlanningService, plan_id: str, workspace: Workspace
) -> None:
    stored = [a["operation_id"] for a in workspace.repository.get("plans", plan_id)["assignments"]]

    first = service.get_plan(plan_id, offset=0, limit=3)
    second = service.get_plan(plan_id, offset=first.next_offset, limit=3)

    assert first.total == second.total == 4
    assert (first.offset, first.limit, first.next_offset) == (0, 3, 3)
    assert (second.offset, second.next_offset) == (3, None)
    pages = [_row(a)["operation_id"] for a in (*first.assignments, *second.assignments)]
    assert pages == stored


def test_get_plan_page_carries_problem_id_and_provenance(
    service: PlanningService, plan_id: str, built: Any
) -> None:
    page = service.get_plan(plan_id)

    assert page.plan_id == plan_id
    assert page.problem_id == built.problem_id
    assert page.next_offset is None
    assert page.validity == "provisional"
    assert page.evidence_kind


def test_get_plan_assignments_carry_lineage_from_the_problem(
    service: PlanningService, plan_id: str, shop: ErpSnapshot
) -> None:
    lineage_index = build_problem_from_erp(shop, tiny_profile()).lineage_index

    page = service.get_plan(plan_id)

    for assignment in page.assignments:
        row = _row(assignment)
        assert row["lineage"] == lineage_index[row["operation_id"]].model_dump(mode="json")


def test_get_plan_filters_by_job(service: PlanningService, plan_id: str) -> None:
    page = service.get_plan(plan_id, job_id="PO-2")

    ops = sorted(_row(a)["operation_id"] for a in page.assignments)
    assert ops == ["PO-2:10", "PO-2:20"]
    assert page.total == 2


def test_get_plan_unknown_job_gives_an_empty_page(service: PlanningService, plan_id: str) -> None:
    page = service.get_plan(plan_id, job_id="NOPE")

    assert page.total == 0
    assert list(page.assignments) == []
    assert page.next_offset is None


def test_get_plan_accepts_limit_1000(service: PlanningService, plan_id: str) -> None:
    assert service.get_plan(plan_id, limit=1000).total == 4


@pytest.mark.parametrize(("offset", "limit"), [(0, 0), (0, 1001), (-1, 10)])
def test_get_plan_rejects_limit_outside_1_to_1000_or_negative_offset(
    service: PlanningService, plan_id: str, offset: int, limit: int
) -> None:
    with pytest.raises(ValueError):
        service.get_plan(plan_id, offset=offset, limit=limit)


def test_get_plan_unknown_plan_is_not_found(service: PlanningService) -> None:
    with pytest.raises(NotFound):
        service.get_plan("nope:00:baseline")


# --- get_pareto_menu ------------------------------------------------------------------------------


def test_get_pareto_menu_maps_menu_entries_to_plan_ids(
    service: PlanningService, portfolio: Any
) -> None:
    menu = service.get_pareto_menu(portfolio.portfolio_id)

    entries = {_row(e)["plan_id"]: _row(e) for e in menu.entries}
    assert set(entries) == set(portfolio.plan_ids)
    assert all(entry["supported"] is True for entry in entries.values())
    baseline = entries[portfolio.recommended_plan_id]["values"]
    assert set(baseline) == {"makespan", "weighted_tardiness"}
    assert menu.portfolio_id == portfolio.portfolio_id
    _assert_provisional(menu)


def test_get_pareto_menu_unknown_portfolio_is_not_found(service: PlanningService) -> None:
    with pytest.raises(NotFound):
        service.get_pareto_menu("nope")


# --- resolve_with_overrides -----------------------------------------------------------------------


def test_resolve_with_overrides_builds_previous_plan_from_the_stored_plan(
    service: PlanningService,
    plan_id: str,
    built: Any,
    fake: FakePortfolioApi,
    workspace: Workspace,
    shop: ErpSnapshot,
) -> None:
    stored = workspace.repository.get("plans", plan_id)

    service.resolve_with_overrides(plan_id, (HOLD_PO2,), request_key="r1", caller_id=CALLER)

    assert fake.request == build_problem_from_erp(shop, tiny_profile()).request
    assert fake.overrides == (HOLD_PO2,)
    previous = fake.previous_plan
    assert previous is not None
    assert previous.plan_id == plan_id
    assert previous.problem_digest == built.problem_digest
    assert [(p.operation_id, p.machine_id, p.start) for p in previous.assignments] == [
        (a["operation_id"], a["machine_id"], a["start"]) for a in stored["assignments"]
    ]


def test_resolve_with_overrides_stores_a_new_portfolio_on_the_same_problem(
    service: PlanningService, plan_id: str, portfolio: Any, built: Any, workspace: Workspace
) -> None:
    revised = service.resolve_with_overrides(
        plan_id, (HOLD_PO2,), request_key="r1", caller_id=CALLER
    )

    assert revised.portfolio_id != portfolio.portfolio_id
    assert len(revised.plan_ids) == 2
    assert workspace.repository.count("portfolios") == 2
    assert workspace.repository.count("plans") == 4
    for new_plan in revised.plan_ids:
        record = workspace.repository.get("plans", new_plan)
        assert record["problem_id"] == built.problem_id
        assert record["problem_digest"] == built.problem_digest
        assert "PO-2:10" not in {a["operation_id"] for a in record["assignments"]}
    _assert_provisional(revised)


def test_resolve_with_overrides_replays_without_solving(
    service: PlanningService, plan_id: str, fake: FakePortfolioApi
) -> None:
    first = service.resolve_with_overrides(plan_id, (HOLD_PO2,), request_key="r1", caller_id=CALLER)
    again = service.resolve_with_overrides(plan_id, (HOLD_PO2,), request_key="r1", caller_id=CALLER)

    assert again == first
    assert fake.calls["resolve_with_overrides"] == 1


def test_resolve_with_overrides_unknown_plan_is_not_found(
    service: PlanningService, fake: FakePortfolioApi
) -> None:
    with pytest.raises(NotFound):
        service.resolve_with_overrides("nope", (HOLD_PO2,), request_key="r", caller_id=CALLER)

    assert fake.calls["resolve_with_overrides"] == 0


# --- insert_rush_order ----------------------------------------------------------------------------


def test_insert_rush_order_turns_the_rush_into_a_job_from_the_standard_routing(
    service: PlanningService, plan_id: str, fake: FakePortfolioApi, shop: ErpSnapshot
) -> None:
    service.insert_rush_order(plan_id, _rush(), request_key="i1", caller_id=CALLER)

    assert fake.rush_job == build_rush_job(shop, tiny_profile(), _rush())
    assert fake.rush_job is not None
    assert fake.rush_job.weight_class == 2  # tiny_profile rush_class, read from the problem
    assert fake.previous_plan is not None
    assert fake.previous_plan.plan_id == plan_id


def test_insert_rush_order_stores_one_plan_and_surfaces_relaxations(
    service: PlanningService, plan_id: str, built: Any, workspace: Workspace
) -> None:
    before = workspace.repository.count("plans")

    summary = service.insert_rush_order(plan_id, _rush(), request_key="i1", caller_id=CALLER)

    assert workspace.repository.count("plans") == before + 1
    assert tuple(summary.relaxations) == ()
    record = workspace.repository.get("plans", summary.plan_id)
    assert record["problem_id"] == built.problem_id
    assert record["source"] == "erp"
    placed = {a["operation_id"] for a in record["assignments"]}
    assert {"RUSH-1:10", "RUSH-1:20"} <= placed
    assert service.get_plan(summary.plan_id).total == 6
    _assert_provisional(summary)


def test_insert_rush_order_item_without_routing_is_blocked_no_routing(
    service: PlanningService, plan_id: str, fake: FakePortfolioApi, workspace: Workspace
) -> None:
    before = _counts(workspace)

    with pytest.raises(Blocked) as raised:
        service.insert_rush_order(
            plan_id, _rush(item_id="GADGET"), request_key="i2", caller_id=CALLER
        )

    assert raised.value.code == "blocked"
    assert "no_routing" in {issue.code for issue in raised.value.issues}
    assert fake.calls["insert_rush_order"] == 0
    assert _counts(workspace) == before


def test_insert_rush_order_unknown_routing_id_is_blocked(
    service: PlanningService, plan_id: str, fake: FakePortfolioApi
) -> None:
    with pytest.raises(Blocked):
        service.insert_rush_order(
            plan_id, _rush(routing_id="R-NOPE"), request_key="i3", caller_id=CALLER
        )

    assert fake.calls["insert_rush_order"] == 0


def test_insert_rush_order_replays_without_solving(
    service: PlanningService, plan_id: str, fake: FakePortfolioApi
) -> None:
    first = service.insert_rush_order(plan_id, _rush(), request_key="i1", caller_id=CALLER)
    again = service.insert_rush_order(plan_id, _rush(), request_key="i1", caller_id=CALLER)

    assert again == first
    assert fake.calls["insert_rush_order"] == 1


# --- compare_plans --------------------------------------------------------------------------------


def test_compare_plans_shows_values_side_by_side_and_ranks_by_profile_objective(
    service: PlanningService, portfolio: Any, workspace: Workspace
) -> None:
    plan_ids = list(portfolio.plan_ids)
    stored = {pid: workspace.repository.get("plans", pid)["objective_values"] for pid in plan_ids}

    result = _row(service.compare_plans(tuple(reversed(plan_ids))))

    rows = {row["plan_id"]: row for row in result["plans"]}
    assert set(rows) == set(plan_ids)
    for pid in plan_ids:
        assert rows[pid]["objective_values"] == stored[pid]
    expected = rank(stored, tiny_profile().scheduling.objective)
    assert result["ranking"] == expected
    assert result["ranking"][0] == portfolio.recommended_plan_id
    assert result["validity"] == "provisional"
    assert result["evidence_kind"]


def test_compare_plans_refuses_plans_of_different_problems(
    service: PlanningService, portfolio: Any, shop: ErpSnapshot
) -> None:
    other = service.build_problem(shop, tiny_profile(time_budget_seconds=20.0))
    other_plan = _solve(service, other.problem_id, "other").plan_ids[0]

    with pytest.raises(PlanningError) as raised:
        service.compare_plans((portfolio.plan_ids[0], other_plan))

    assert raised.value.code == "mixed_problems"


@pytest.mark.parametrize("size", [0, 1, 11])
def test_compare_plans_needs_two_to_ten_plans(service: PlanningService, size: int) -> None:
    with pytest.raises(ValueError):
        service.compare_plans(tuple(f"p{n}" for n in range(size)))


def test_compare_plans_unknown_plan_is_not_found(service: PlanningService, plan_id: str) -> None:
    with pytest.raises(NotFound):
        service.compare_plans((plan_id, "nope"))


# --- read-only methods write nothing (SDD tool table) ---------------------------------------------


def test_read_methods_leave_every_record_count_unchanged(
    service: PlanningService, portfolio: Any, plan_id: str, workspace: Workspace
) -> None:
    before = _counts(workspace)

    service.validate_snapshot(tiny_shop(), tiny_profile())
    service.get_plan(plan_id)
    service.get_pareto_menu(portfolio.portfolio_id)
    service.compare_plans(tuple(portfolio.plan_ids))
    with pytest.raises(CapabilityUnavailable):
        service.explain_plan(plan_id)

    assert _counts(workspace) == before


# --- simulate_plan (REQ-171) ----------------------------------------------------------------------


def test_simulate_plan_on_erp_plan_is_blocked_simulation_link_pending(
    service: PlanningService, plan_id: str, workspace: Workspace
) -> None:
    result = _row(
        service.simulate_plan(plan_id, request_key="sim", caller_id=CALLER, reps=1, seed=0)
    )

    assert result["status"] == "blocked"
    assert "simulation_link_pending" in {issue["code"] for issue in result["issues"]}
    assert result["job"] is None
    assert result["validity"] == "provisional"
    assert result["evidence_kind"]
    assert workspace.repository.count("jobs") == 0


def test_simulate_plan_requires_request_key(service: PlanningService, plan_id: str) -> None:
    with pytest.raises(ValueError):
        service.simulate_plan(plan_id, request_key="", caller_id=CALLER)


@pytest.mark.parametrize(("reps", "wait"), [(0, 0), (11, 0), (1, -1), (1, 121)])
def test_simulate_plan_rejects_reps_or_wait_out_of_range(
    service: PlanningService, plan_id: str, reps: int, wait: float
) -> None:
    with pytest.raises(ValueError):
        service.simulate_plan(
            plan_id, request_key="sim", caller_id=CALLER, reps=reps, wait_seconds=wait
        )


def test_simulate_plan_unknown_plan_is_not_found(service: PlanningService) -> None:
    with pytest.raises(NotFound):
        service.simulate_plan("nope", request_key="sim", caller_id=CALLER)


def _fact(value: Any, unit: str | None = None) -> dict[str, Any]:
    return {"status": "supplied", "value": value, "unit": unit, "source": "operator"}


def _scenario_plan(workspace: Workspace) -> str:
    """A verified scenario schedule plan, built the way today's REST flow builds one."""
    draft = LinearDraft.model_validate(
        {
            "product": _fact("finished"),
            "input_part": _fact("blank"),
            "as_of": _fact("2026-10-08T00:00:00Z"),
            "operators": _fact(1, "person"),
            "simple_flow_confirmed": _fact(True),
            "stages": [
                {
                    "id": "cut",
                    "seconds_per_piece": _fact(4.0, "second/piece"),
                    "machine_capacity": _fact(1, "machine"),
                }
            ],
            "orders": [
                {
                    "id": "o1",
                    "quantity": _fact(1, "piece"),
                    "release_second": _fact(0.0, "second"),
                    "due_second": _fact(100.0, "second"),
                }
            ],
        }
    )
    scenario = workspace.import_model_draft(workspace.save_model_draft("demo", draft)["id"])
    content = base64.b64encode(b"Order,Part,Qty,Release,Due\nnew-1,finished,2,0,1000\n").decode()
    mapping = {
        "order_id": "Order",
        "product": "Part",
        "quantity": "Qty",
        "release_second": "Release",
        "due_second": "Due",
    }
    preview = workspace.preview_schedule_upload(scenario["id"], "orders.csv", content, mapping)
    snapshot = workspace.commit_schedule_upload(
        scenario["id"], "orders.csv", content, preview["original_file_digest"], mapping
    )
    plan = workspace.solve_schedule_snapshot(snapshot["id"], "baseline", 5)
    assert plan["simulation"]["status"] == "available", plan["simulation"]
    return str(plan["id"])


def test_simulate_plan_on_scenario_plan_waits_for_the_finished_job(
    service: PlanningService, workspace: Workspace
) -> None:
    scenario_plan = _scenario_plan(workspace)

    result = _row(
        service.simulate_plan(
            scenario_plan, request_key="sim", caller_id=CALLER, reps=1, seed=42, wait_seconds=60
        )
    )

    assert result["status"] == "completed"
    assert result["job"]["status"] == "completed"
    assert result["job"]["result"]["schedule_plan"]["id"] == scenario_plan
    assert result["validity"] == "provisional"
    assert result["evidence_kind"]


def test_simulate_plan_on_scenario_plan_is_todays_submit_schedule_plan_claim(
    service: PlanningService, workspace: Workspace
) -> None:
    scenario_plan = _scenario_plan(workspace)

    result = _row(
        service.simulate_plan(scenario_plan, request_key="sim", caller_id=CALLER, reps=1, seed=42)
    )
    today = workspace.submit_schedule_plan(scenario_plan, 1, 42, f"{CALLER}:sim")

    assert result["job"]["id"] == today["id"]
    assert result["job"]["schedule_plan_id"] == scenario_plan
    assert workspace.repository.count("jobs") == 1


def test_simulate_plan_without_wait_returns_the_submitted_job_at_once(
    service: PlanningService, workspace: Workspace
) -> None:
    scenario_plan = _scenario_plan(workspace)

    result = _row(
        service.simulate_plan(scenario_plan, request_key="sim", caller_id=CALLER, wait_seconds=0)
    )

    assert result["job"]["status"] in {"queued", "running", "completed"}
    assert (
        workspace.repository.get("jobs", result["job"]["id"])["schedule_plan_id"] == scenario_plan
    )


# --- every stored record is provisional evidence (REQ-177) ----------------------------------------


def test_every_planning_record_carries_validity_and_evidence_kind(
    service: PlanningService, plan_id: str, workspace: Workspace
) -> None:
    service.resolve_with_overrides(plan_id, (HOLD_PO2,), request_key="r", caller_id=CALLER)
    service.insert_rush_order(plan_id, _rush(), request_key="i", caller_id=CALLER)
    service.build_problem(tiny_shop(bad_work_center=True), tiny_profile())

    for kind in PLAN_KINDS:
        records = workspace.repository.list(kind, limit=1000)
        assert records, kind
        for record in records:
            assert record["validity"] == "provisional", (kind, record.get("id"))
            assert isinstance(record["evidence_kind"], str), (kind, record.get("id"))
            assert record["evidence_kind"], (kind, record.get("id"))


def test_scenario_plan_time_limit_applied_comes_from_the_solver_result(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The baseline now stops at its budget too; the flag is what the solver reported."""
    baseline_plan = workspace.repository.get("schedule_plans", _scenario_plan(workspace))
    assert baseline_plan["backend"] == "baseline"
    assert baseline_plan["time_limit_applied"] is True
    assert baseline_plan["result"]["schedule"]["budget_enforced"] is True

    from twinflow.application import workspace as workspace_module
    from twinflow.scheduling import BaselineSolver

    class IgnoresBudget:
        """A registered solver that does not claim to honor the budget."""

        name = "ignores-budget"
        capabilities = BaselineSolver().capabilities

        def solve(self, problem: Any, *, objective: Any, budget_seconds: float) -> Any:
            schedule = BaselineSolver().solve(problem, objective=objective, budget_seconds=60.0)
            return schedule.model_copy(update={"budget_enforced": False})

    monkeypatch.setattr(workspace_module, "build_solver", lambda name: IgnoresBudget())
    other = workspace.repository.get("schedule_plans", _scenario_plan(workspace))

    assert other["time_limit_applied"] is False
