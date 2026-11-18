"""Rush order to TF-002 `Job` from the item's standard routing (T-206, COMP-116, COMP-117).

Requirements: REQ-154, REQ-163 (insert_rush_order). SDD External Integrations:
"`InsertRushInput.rush` becomes the `rush_job: Job` through the builder's job rules applied
to the item's standard `Routing` from the plan's source snapshot (`routing_id` picks one when
an item has several; none gives `blocked` with `no_routing`)."

Contract pinned here (the implementation follows these tests):

    from twinflow.erp.build import RushOrder, build_rush_job

    build_rush_job(snapshot, profile, rush) -> Job | tuple[ErpIssue, ...]

* `RushOrder{order_id, item_id, quantity > 0, due: AwareDatetime, customer_id?, routing_id?}`
  (SDD Key input models), frozen pydantic, unknown fields rejected.
* Job id = `rush.order_id`; `weight_class = profile.erp.rush_class` (rush wins over the
  customer's class map); `due` = seconds from `snapshot.as_of` to `rush.due` (real elapsed
  seconds, any offset); `release` 0; status "open".
* Operations: one per in-plant routing operation in `sequence` order, id
  `f"{order_id}:{op_id}"`, `quantity = rush.quantity`, options per the builder rules: a
  machine-level option gives one `ProcessingOption`; a work-center-level option gives one per
  ACTIVE machine of the center; `unit_seconds = run_seconds_per_unit / efficiency`,
  `fixed_seconds = (fixed_run_seconds + setup_seconds) / efficiency`, `preference_cost =
  preference * alternate_penalty_seconds`. Precedence: each op links to the previous in-plant
  op with `min_lag = predecessor.move_seconds + successor.queue_seconds + summed lead_seconds
  of outside ops between`; the first op's `queue_seconds` is its `ready_time`.
* Routing choice (PINNED): `routing_id` given -> that routing, which must belong to the item;
  absent -> the item's routing with the smallest id. No routing for the item, an unknown
  `routing_id`, or a `routing_id` of another item -> `(ErpIssue(code="no_routing",
  severity="hard", ...),)`.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from twinflow.erp.build import RushOrder, build_problem_from_erp, build_rush_job
from twinflow.erp.issues import ErpIssue
from twinflow.erp.profile import ErpPolicy, PlanningProfile
from twinflow.erp.schema import (
    Customer,
    ErpSnapshot,
    Item,
    Lineage,
    Machine,
    OperationOption,
    OrderOperation,
    OutsideProcess,
    ProductionOrder,
    Routing,
    RoutingOperation,
    SourceInfo,
    Vendor,
    WorkCenter,
)
from twinflow.scheduling import Job, ProcessingOption, SchedulingProfile, validate

AS_OF = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC)
PENALTY = 500.0
RUSH_CLASS = 2
CUSTOMER_CLASS = 4


def _lin(table: str, *key: str) -> Lineage:
    return Lineage(table=table, key=key)


def _option(
    machine_id: str | None,
    *,
    setup: float,
    run: float,
    fixed: float = 0.0,
    preference: int = 0,
) -> OperationOption:
    return OperationOption(
        work_center_id="WC1",
        machine_id=machine_id,
        setup_seconds=setup,
        run_seconds_per_unit=run,
        fixed_run_seconds=fixed,
        preference=preference,
        lineage=_lin("options", machine_id or "WC1", str(preference)),
    )


def _step(
    routing_id: str,
    op_id: str,
    sequence: int,
    options: tuple[OperationOption, ...],
    *,
    queue: float = 0.0,
    move: float = 0.0,
    outside: OutsideProcess | None = None,
) -> RoutingOperation:
    return RoutingOperation(
        op_id=op_id,
        sequence=sequence,
        work_center_id="WC1",
        options=options,
        setup_params={},
        queue_seconds=queue,
        move_seconds=move,
        outside=outside,
        lineage=_lin("routing_ops", routing_id, op_id),
    )


def _routing(routing_id: str, item_id: str, *steps: RoutingOperation) -> Routing:
    return Routing(
        id=routing_id, item_id=item_id, operations=steps, lineage=_lin("routings", routing_id)
    )


# R-A (WIDGET) is listed AFTER R-B, so "smallest id" and "first listed" differ.
R_B = _routing("R-B", "WIDGET", _step("R-B", "B10", 10, (_option("M1", setup=5.0, run=1.0),)))
R_A = _routing(
    "R-A",
    "WIDGET",
    # listed out of sequence on purpose: the job must follow `sequence`
    _step(
        "R-A",
        "20",
        20,
        (
            _option("M1", setup=0.0, run=1.0, preference=0),
            _option("M2", setup=0.0, run=1.0, preference=1),
        ),
        queue=45.0,
        move=0.0,
    ),
    _step(
        "R-A",
        "10",
        10,
        (_option(None, setup=60.0, run=2.0, fixed=10.0),),
        queue=30.0,
        move=15.0,
    ),
)
R_SPRING = _routing(
    "R-SPRING",
    "SPRING",
    _step("R-SPRING", "10", 10, (_option("M1", setup=0.0, run=1.0),), queue=5.0, move=7.0),
    _step("R-SPRING", "20", 20, (), outside=OutsideProcess(vendor_id="V1", lead_seconds=3_600.0)),
    _step("R-SPRING", "30", 30, (_option("M1", setup=0.0, run=1.0),), queue=11.0),
)


def _order() -> ProductionOrder:
    op = OrderOperation(
        op_id="10",
        sequence=10,
        work_center_id="WC1",
        options=(_option("M1", setup=0.0, run=1.0),),
        setup_params={},
        queue_seconds=0.0,
        move_seconds=0.0,
        lineage=_lin("order_ops", "PO-1", "10"),
        status="pending",
        qty_to_produce=4.0,
        qty_complete=0.0,
        qty_scrapped=0.0,
        qty_remaining=4.0,
    )
    return ProductionOrder(
        id="PO-1",
        order_type="MO",
        item_id="WIDGET",
        quantity=4.0,
        status="released",
        rush=False,
        hold=False,
        due_date=dt.date(2026, 10, 9),
        sales_links=(),
        operations=(op,),
        materials=(),
        lineage=_lin("orders", "PO-1"),
    )


def _shop() -> ErpSnapshot:
    items = tuple(
        Item(id=item_id, uom="ea", attributes={}, lineage=_lin("items", item_id))
        for item_id in ("WIDGET", "SPRING", "GADGET")
    )
    machines = (
        Machine(id="M1", work_center_id="WC1", active=True, lineage=_lin("m", "M1")),
        Machine(
            id="M2", work_center_id="WC1", efficiency=0.5, active=True, lineage=_lin("m", "M2")
        ),
        Machine(id="M3", work_center_id="WC1", active=False, lineage=_lin("m", "M3")),
    )
    return ErpSnapshot(
        schema_version="twinflow.erp/1",
        as_of=AS_OF,
        plant_timezone="America/Chicago",
        source=SourceInfo(
            system="test", instance="t1", extractor="hand", extractor_version="1", generation=None
        ),
        source_issues=(),
        items=items,
        boms=(),
        routings=(R_B, R_A, R_SPRING),
        work_centers=(WorkCenter(id="WC1", attributes={}, lineage=_lin("wc", "WC1")),),
        machines=machines,
        calendars=(),
        labor_pools=(),
        production_orders=(_order(),),
        sales_order_lines=(),
        customers=(Customer(id="CUST-A", priority_class="A", lineage=_lin("customers", "CUST-A")),),
        vendors=(Vendor(id="V1", lineage=_lin("vendors", "V1")),),
        purchase_lines=(),
        inventory=(),
    )


def _profile() -> PlanningProfile:
    erp = ErpPolicy.model_validate(
        {
            "rush_class": RUSH_CLASS,
            "default_class": 1,
            "alternate_penalty_seconds": PENALTY,
            "class_weights": {"A": CUSTOMER_CLASS},
        }
    )
    return PlanningProfile(scheduling=SchedulingProfile(), erp=erp)


def _rush(**changes: object) -> RushOrder:
    data: dict[str, object] = {
        "order_id": "RUSH-1",
        "item_id": "WIDGET",
        "quantity": 3.0,
        "due": AS_OF + dt.timedelta(days=1),
        "customer_id": "CUST-A",
        "routing_id": "R-A",
    }
    data.update(changes)
    return RushOrder.model_validate(data)


def _job(rush: RushOrder) -> Job:
    result = build_rush_job(_shop(), _profile(), rush)
    assert isinstance(result, Job), result
    return result


def _codes(result: object) -> list[tuple[str, str]]:
    assert isinstance(result, tuple), result
    assert all(isinstance(found, ErpIssue) for found in result)
    return [(found.code, found.severity) for found in result]


def _options(job: Job, op_id: str) -> set[ProcessingOption]:
    op = next(o for o in job.operations if o.id == f"RUSH-1:{op_id}")
    return set(op.options)


# --- RushOrder model ------------------------------------------------------------------------------


def test_rush_order_rejects_naive_due() -> None:
    with pytest.raises(ValidationError):
        _rush(due=dt.datetime(2026, 10, 6, 12, 0))  # noqa: DTZ001 - naive on purpose


def test_rush_order_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValidationError):
        _rush(quantity=0.0)


def test_rush_order_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        _rush(priority="urgent")


def test_rush_order_is_frozen() -> None:
    rush = _rush()

    with pytest.raises(ValidationError):
        rush.quantity = 9.0  # type: ignore[misc]  # frozen model: assignment must raise


# --- job fields ---------------------------------------------------------------------------------


def test_build_rush_job_id_is_the_rush_order_id() -> None:
    assert _job(_rush()).id == "RUSH-1"


def test_build_rush_job_weight_class_is_rush_class_even_for_a_high_class_customer() -> None:
    job = _job(_rush())

    assert job.weight_class == RUSH_CLASS
    assert job.weight_override is None


def test_build_rush_job_due_is_seconds_from_as_of_for_any_offset() -> None:
    due = dt.datetime(2026, 10, 6, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5)))

    job = _job(_rush(due=due))

    assert job.due == pytest.approx((due - AS_OF).total_seconds())
    assert job.due == pytest.approx(86_400.0 + 2 * 3_600.0)


def test_build_rush_job_past_due_is_negative_seconds_not_clamped() -> None:
    job = _job(_rush(due=AS_OF - dt.timedelta(hours=1)))

    assert job.due == pytest.approx(-3_600.0)


def test_build_rush_job_is_open_and_released_now() -> None:
    job = _job(_rush())

    assert job.status == "open"
    assert job.release == 0.0


def test_build_rush_job_without_customer_still_takes_rush_class() -> None:
    assert _job(_rush(customer_id=None)).weight_class == RUSH_CLASS


# --- operations -------------------------------------------------------------------------------


def test_build_rush_job_operations_follow_routing_sequence_with_order_colon_op_ids() -> None:
    job = _job(_rush())

    assert [op.id for op in job.operations] == ["RUSH-1:10", "RUSH-1:20"]
    assert all(op.work_center_id == "WC1" for op in job.operations)


def test_build_rush_job_quantity_is_the_rush_quantity() -> None:
    assert {op.quantity for op in _job(_rush(quantity=7.5)).operations} == {7.5}


def test_build_rush_job_work_center_option_expands_to_active_machines_with_efficiency() -> None:
    options = _options(_job(_rush()), "10")

    assert options == {
        ProcessingOption(
            machine_id="M1", fixed_seconds=70.0, unit_seconds=2.0, preference_cost=0.0
        ),
        ProcessingOption(
            machine_id="M2", fixed_seconds=140.0, unit_seconds=4.0, preference_cost=0.0
        ),
    }


def test_build_rush_job_machine_options_keep_machine_and_preference_penalty() -> None:
    options = _options(_job(_rush()), "20")

    assert options == {
        ProcessingOption(machine_id="M1", fixed_seconds=0.0, unit_seconds=1.0, preference_cost=0.0),
        ProcessingOption(
            machine_id="M2", fixed_seconds=0.0, unit_seconds=2.0, preference_cost=PENALTY
        ),
    }


def test_build_rush_job_never_offers_an_inactive_machine() -> None:
    job = _job(_rush())

    assert "M3" not in {option.machine_id for op in job.operations for option in op.options}


def test_build_rush_job_precedence_lag_is_move_plus_queue() -> None:
    job = _job(_rush())
    first, second = job.operations

    assert first.predecessors == ()
    assert first.ready_time == pytest.approx(30.0)
    assert len(second.predecessors) == 1
    assert second.predecessors[0].operation_id == "RUSH-1:10"
    assert second.predecessors[0].min_lag == pytest.approx(15.0 + 45.0)


def test_build_rush_job_outside_op_in_the_middle_is_a_lag_not_an_operation() -> None:
    job = _job(_rush(item_id="SPRING", routing_id=None))

    assert [op.id for op in job.operations] == ["RUSH-1:10", "RUSH-1:30"]
    link = job.operations[1].predecessors[0]
    assert link.operation_id == "RUSH-1:10"
    assert link.min_lag == pytest.approx(7.0 + 3_600.0 + 11.0)


# --- routing choice -----------------------------------------------------------------------------


def test_build_rush_job_routing_id_picks_that_routing() -> None:
    assert [op.id for op in _job(_rush(routing_id="R-B")).operations] == ["RUSH-1:B10"]


def test_build_rush_job_several_routings_without_routing_id_picks_smallest_id() -> None:
    job = _job(_rush(routing_id=None))

    assert [op.id for op in job.operations] == ["RUSH-1:10", "RUSH-1:20"]


def test_build_rush_job_item_without_routing_gives_no_routing() -> None:
    result = build_rush_job(_shop(), _profile(), _rush(item_id="GADGET", routing_id=None))

    assert _codes(result) == [("no_routing", "hard")]


def test_build_rush_job_unknown_routing_id_gives_no_routing() -> None:
    result = build_rush_job(_shop(), _profile(), _rush(routing_id="R-NOPE"))

    assert _codes(result) == [("no_routing", "hard")]


def test_build_rush_job_routing_of_another_item_gives_no_routing() -> None:
    result = build_rush_job(_shop(), _profile(), _rush(routing_id="R-SPRING"))

    assert _codes(result) == [("no_routing", "hard")]


def test_build_rush_job_unknown_item_gives_no_routing() -> None:
    result = build_rush_job(_shop(), _profile(), _rush(item_id="NOPE", routing_id=None))

    assert _codes(result) == [("no_routing", "hard")]


# --- fits the built problem ----------------------------------------------------------------------


def test_build_rush_job_appended_to_the_built_request_passes_tf002_validation() -> None:
    built = build_problem_from_erp(_shop(), _profile())
    assert built.request is not None, [i.code for i in built.issues]
    request = built.request
    job = _job(_rush())

    problem = request.problem.model_copy(update={"jobs": (*request.problem.jobs, job)})

    assert validate(request.model_copy(update={"problem": problem})) == []
