"""Typed test doubles and tiny inputs for the planning service tests (T-205, T-206).

`FakePortfolioApi` implements the `twinflow.application.portfolio_port.PortfolioApi`
contract (the four TF-003 functions, signatures per TF-005 SDD External Integrations) with
real TF-002 solving underneath: each solve runs the baseline solver and returns two
candidates, the baseline schedule and the same schedule delayed by `DELAY_SECONDS`, so the
two candidates have different objective values and a deterministic rank. The result models
use the TF-003 field names (`PortfolioResult`, `Candidate`, `MenuEntry`, TF-003 SDD Data
Model) and are pydantic, so `model_dump(mode="json")` stores them.

Spy surface: `calls` (per function name), `budgets` (every `budget_seconds` received, in
order), `last_budget_seconds`, and the last `request`, `previous_plan`, `overrides` and
`rush_job` seen. `block`: when set to an `Event`, every function first waits on it (at most
`BLOCK_LIMIT_SECONDS`), simulating a solver that runs past its budget; `entered` is set as
soon as any call is inside the fake, so a test can tell the call is in flight.

Also here: `tiny_shop()` (a hand-built `twinflow.erp/1` snapshot with no hard issue),
`tiny_profile()` and `tiny_request()` (a TF-002 request independent of the ERP builder).
"""

from __future__ import annotations

import datetime as dt
import threading
from collections import Counter

from pydantic import BaseModel, ConfigDict

from twinflow.application.portfolio_port import PortfolioApi
from twinflow.erp.profile import ErpPolicy, PlanningProfile
from twinflow.erp.schema import (
    Customer,
    ErpSnapshot,
    Item,
    Lineage,
    Machine,
    OperationOption,
    OrderOperation,
    ProductionOrder,
    Routing,
    RoutingOperation,
    SourceInfo,
    WorkCenter,
)
from twinflow.scheduling import (
    Assignment,
    Job,
    ObjectiveKind,
    Operation,
    Override,
    Precedence,
    PreviousPlan,
    Problem,
    ProcessingOption,
    Schedule,
    ScheduleIssue,
    SchedulingProfile,
    SolveRequest,
    SolveResult,
    apply_overrides,
    evaluate,
    solve,
    verify,
)
from twinflow.scheduling import Machine as ProblemMachine
from twinflow.scheduling import WorkCenter as ProblemWorkCenter

DELAY_SECONDS = 3_600.0
BLOCK_LIMIT_SECONDS = 30.0
MENU_AXES = (ObjectiveKind.MAKESPAN, ObjectiveKind.WEIGHTED_TARDINESS)
AS_OF = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC)


# --- TF-003 result shapes (field names per TF-003 SDD Data Model) ---------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class FakeMenuEntry(_Frozen):
    candidate_id: str
    supported: bool
    values: dict[ObjectiveKind, float]


class FakeCandidate(_Frozen):
    id: str
    pipeline: tuple[str, ...]
    schedule: Schedule
    verified: bool
    objective_values: dict[ObjectiveKind, float]
    issues: tuple[ScheduleIssue, ...] = ()


class FakePortfolioResult(_Frozen):
    candidates: tuple[FakeCandidate, ...]
    menu: tuple[FakeMenuEntry, ...]
    recommended: str | None
    issues: tuple[ScheduleIssue, ...] = ()
    elapsed_seconds: float = 0.0


# --- the fake --------------------------------------------------------------------------------


class FakePortfolioApi:
    """PortfolioApi with real baseline solving, call spies and an optional blocking gate."""

    def __init__(self, *, block: threading.Event | None = None) -> None:
        self.block = block
        self.entered = threading.Event()
        self.calls: Counter[str] = Counter()
        self.budgets: list[float | None] = []
        self.request: SolveRequest | None = None
        self.previous_plan: PreviousPlan | None = None
        self.overrides: tuple[Override, ...] | None = None
        self.rush_job: Job | None = None
        self._lock = threading.Lock()

    @property
    def last_budget_seconds(self) -> float | None:
        return self.budgets[-1] if self.budgets else None

    def _enter(self, name: str, request: SolveRequest, budget_seconds: float | None) -> None:
        with self._lock:
            self.calls[name] += 1
            self.budgets.append(budget_seconds)
            self.request = request
        self.entered.set()
        if self.block is not None:
            self.block.wait(BLOCK_LIMIT_SECONDS)

    def solve_portfolio(
        self,
        request: SolveRequest,
        *,
        budget_seconds: float | None = None,
        seed: int = 0,
        pipelines: tuple[str, ...] | None = None,
        k_lookup: object | None = None,
    ) -> FakePortfolioResult:
        self._enter("solve_portfolio", request, budget_seconds)
        return _portfolio(request)

    def get_pareto_menu(
        self, result: FakePortfolioResult, axes: tuple[ObjectiveKind, ...] | None = None
    ) -> tuple[FakeMenuEntry, ...]:
        with self._lock:
            self.calls["get_pareto_menu"] += 1
        if axes is None:
            return result.menu
        return tuple(_menu_entry(candidate, axes) for candidate in result.candidates)

    def resolve_with_overrides(
        self,
        request: SolveRequest,
        previous_plan: PreviousPlan,
        overrides: tuple[Override, ...],
        *,
        budget_seconds: float | None = None,
        seed: int = 0,
    ) -> FakePortfolioResult:
        self._enter("resolve_with_overrides", request, budget_seconds)
        with self._lock:
            self.previous_plan = previous_plan
            self.overrides = overrides
        revised = request.model_copy(
            update={"previous_plan": previous_plan, "overrides": request.overrides + overrides}
        )
        return _portfolio(revised)

    def insert_rush_order(
        self,
        request: SolveRequest,
        previous_plan: PreviousPlan,
        rush_job: Job,
        *,
        budget_seconds: float | None = None,
        seed: int = 0,
    ) -> SolveResult:
        self._enter("insert_rush_order", request, budget_seconds)
        with self._lock:
            self.previous_plan = previous_plan
            self.rush_job = rush_job
        problem = request.problem.model_copy(update={"jobs": (*request.problem.jobs, rush_job)})
        revised = request.model_copy(update={"problem": problem, "previous_plan": previous_plan})
        return solve(revised)


def _portfolio(request: SolveRequest) -> FakePortfolioResult:
    """Two candidates: the baseline schedule and the same schedule delayed (still feasible)."""
    result = solve(request)
    compiled = apply_overrides(request)
    delayed = result.schedule.model_copy(
        update={
            "assignments": tuple(_delayed(a) for a in result.schedule.assignments),
            "primary_value": None,
        }
    )
    delayed_ok = verify(compiled, request.overrides, delayed, request.previous_plan).valid
    candidates = (
        FakeCandidate(
            id="00:baseline",
            pipeline=("baseline",),
            schedule=result.schedule,
            verified=result.verified,
            objective_values=dict(result.objective_values),
        ),
        FakeCandidate(
            id="01:delayed",
            pipeline=("baseline", "delay"),
            schedule=delayed,
            verified=delayed_ok,
            objective_values=evaluate(compiled, delayed, request.previous_plan),
        ),
    )
    menu = tuple(_menu_entry(candidate, MENU_AXES) for candidate in candidates)
    return FakePortfolioResult(candidates=candidates, menu=menu, recommended="00:baseline")


def _delayed(assignment: Assignment) -> Assignment:
    return assignment.model_copy(
        update={
            "start": assignment.start + DELAY_SECONDS,
            "setup_end": assignment.setup_end + DELAY_SECONDS,
            "end": assignment.end + DELAY_SECONDS,
        }
    )


def _menu_entry(candidate: FakeCandidate, axes: tuple[ObjectiveKind, ...]) -> FakeMenuEntry:
    values = {axis: candidate.objective_values[axis] for axis in axes}
    return FakeMenuEntry(candidate_id=candidate.id, supported=True, values=values)


def as_portfolio_api(fake: FakePortfolioApi) -> PortfolioApi:
    """Typed view of the fake; mypy checks it satisfies the port Protocol structurally."""
    return fake


# --- tiny inputs -------------------------------------------------------------------------------


def _lineage(table: str, *key: str) -> Lineage:
    return Lineage(table=table, key=key)


def _option(
    machine_id: str | None = None,
    *,
    setup: float = 60.0,
    run: float = 2.0,
    fixed: float = 10.0,
    preference: int = 0,
) -> OperationOption:
    return OperationOption(
        work_center_id="WC1",
        machine_id=machine_id,
        setup_seconds=setup,
        run_seconds_per_unit=run,
        fixed_run_seconds=fixed,
        preference=preference,
        lineage=_lineage("options", machine_id or "WC1", str(preference)),
    )


def _order_op(order_id: str, op_id: str, sequence: int, option: OperationOption) -> OrderOperation:
    return OrderOperation(
        op_id=op_id,
        sequence=sequence,
        work_center_id="WC1",
        options=(option,),
        setup_params={},
        queue_seconds=30.0,
        move_seconds=15.0,
        lineage=_lineage("order_ops", order_id, op_id),
        status="pending",
        qty_to_produce=10.0,
        qty_complete=0.0,
        qty_scrapped=0.0,
        qty_remaining=10.0,
    )


def _order(order_id: str, due: dt.date) -> ProductionOrder:
    return ProductionOrder(
        id=order_id,
        order_type="MO",
        item_id="WIDGET",
        quantity=10.0,
        status="released",
        rush=False,
        hold=False,
        due_date=due,
        sales_links=(),
        operations=(
            _order_op(order_id, "10", 10, _option()),
            _order_op(order_id, "20", 20, _option("M1", setup=0.0, run=1.0, fixed=0.0)),
        ),
        materials=(),
        lineage=_lineage("orders", order_id),
    )


def _routing(routing_id: str) -> Routing:
    steps = (
        ("10", 10, _option()),
        ("20", 20, _option("M1", setup=0.0, run=1.0, fixed=0.0)),
    )
    return Routing(
        id=routing_id,
        item_id="WIDGET",
        operations=tuple(
            RoutingOperation(
                op_id=op_id,
                sequence=sequence,
                work_center_id="WC1",
                options=(option,),
                setup_params={},
                queue_seconds=30.0,
                move_seconds=15.0,
                lineage=_lineage("routing_ops", routing_id, op_id),
            )
            for op_id, sequence, option in steps
        ),
        lineage=_lineage("routings", routing_id),
    )


def tiny_shop(*, bad_work_center: bool = False) -> ErpSnapshot:
    """Two open orders of two ops each on work center WC1 (machines M1, M2); no hard issue.

    Item WIDGET has one standard routing (`R-WIDGET`); item GADGET has none. With
    `bad_work_center` the second order points at an unknown work center (hard issue).
    """
    orders = (_order("PO-1", dt.date(2026, 10, 9)), _order("PO-2", dt.date(2026, 10, 7)))
    if bad_work_center:
        broken = orders[1].operations[0].model_copy(update={"work_center_id": "NOPE"})
        orders = (orders[0], orders[1].model_copy(update={"operations": (broken,)}))
    return ErpSnapshot(
        schema_version="twinflow.erp/1",
        as_of=AS_OF,
        plant_timezone="UTC",
        source=SourceInfo(
            system="test", instance="t1", extractor="hand", extractor_version="1", generation=None
        ),
        source_issues=(),
        items=(
            Item(id="WIDGET", uom="ea", attributes={}, lineage=_lineage("items", "WIDGET")),
            Item(id="GADGET", uom="ea", attributes={}, lineage=_lineage("items", "GADGET")),
        ),
        boms=(),
        routings=(_routing("R-WIDGET"),),
        work_centers=(WorkCenter(id="WC1", attributes={}, lineage=_lineage("wc", "WC1")),),
        machines=(
            Machine(id="M1", work_center_id="WC1", active=True, lineage=_lineage("m", "M1")),
            Machine(
                id="M2",
                work_center_id="WC1",
                efficiency=0.5,
                active=True,
                lineage=_lineage("m", "M2"),
            ),
        ),
        calendars=(),
        labor_pools=(),
        production_orders=orders,
        sales_order_lines=(),
        customers=(
            Customer(id="CUST-A", priority_class="A", lineage=_lineage("customers", "CUST-A")),
        ),
        vendors=(),
        purchase_lines=(),
        inventory=(),
    )


def tiny_profile(*, time_budget_seconds: float = 10.0, rush_class: int = 2) -> PlanningProfile:
    """Default scheduling half with a chosen budget; rush class 2 so it is distinguishable."""
    return PlanningProfile(
        scheduling=SchedulingProfile(time_budget_seconds=time_budget_seconds),
        erp=ErpPolicy.model_validate({"rush_class": rush_class, "class_weights": {"A": 4}}),
    )


def tiny_request() -> SolveRequest:
    """A TF-002 request built by hand: jobs J1 (two ops) and J2 (one op) on M1/M2."""

    def op(op_id: str, predecessors: tuple[Precedence, ...] = ()) -> Operation:
        options = tuple(
            ProcessingOption(
                machine_id=m, fixed_seconds=10.0, unit_seconds=2.0, preference_cost=0.0
            )
            for m in ("M1", "M2")
        )
        return Operation(
            id=op_id,
            work_center_id="WC1",
            quantity=5.0,
            options=options,
            predecessors=predecessors,
        )

    jobs = (
        Job(
            id="J1",
            due=500.0,
            operations=(op("J1:10"), op("J1:20", (Precedence(operation_id="J1:10"),))),
        ),
        Job(id="J2", due=200.0, operations=(op("J2:10"),)),
    )
    problem = Problem(
        id="tiny",
        jobs=jobs,
        machines=(
            ProblemMachine(id="M1", work_center_id="WC1"),
            ProblemMachine(id="M2", work_center_id="WC1"),
        ),
        work_centers=(ProblemWorkCenter(id="WC1"),),
    )
    return SolveRequest(schema_version="2.0", problem=problem)


def rush_job_for(request: SolveRequest, job_id: str = "RUSH-1") -> Job:
    """A one-op job that fits `tiny_request`'s machines."""
    option = ProcessingOption(
        machine_id="M1", fixed_seconds=5.0, unit_seconds=1.0, preference_cost=0.0
    )
    operation = Operation(id=f"{job_id}:10", work_center_id="WC1", quantity=2.0, options=(option,))
    return Job(id=job_id, weight_class=4, due=100.0, operations=(operation,))
