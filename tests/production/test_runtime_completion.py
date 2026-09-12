"""Completion contract acceptance cases for the integrated production runtime."""

from __future__ import annotations

from importlib import import_module

import pytest

contracts = import_module("twinflow.production.contracts")
runtime = import_module("twinflow.production.runtime")
lots = import_module("twinflow.production.lots")
materials = import_module("twinflow.production.materials")
qualification = import_module("twinflow.production.qualification")
external = import_module("twinflow.production.external")


def resource(resource_id: str, windows=((0, 500),)):
    return contracts.Resource(
        resource_id,
        "machine",
        windows=tuple(contracts.TimeWindow(*window) for window in windows),
    )


def operation(operation_id: str, order_id: str, resource_id: str, duration: float, **kwargs):
    return contracts.ProductionOperation(
        operation_id,
        order_id,
        (
            contracts.RecipeAlternative(
                f"{operation_id}:recipe",
                resource_id,
                "r1",
                (contracts.Phase("run", duration),),
                (contracts.ResourceUse((resource_id,), "run", "run"),),
            ),
        ),
        **kwargs,
    )


def lot_ledger(order_id: str, lot_id: str, qty: float, operation_id: str = "main"):
    ledger = lots.LotLedger()
    ledger.demand(order_id, qty, "piece")
    ledger.seed(lots.FlowLot(lot_id, order_id, operation_id, qty, "piece", "queued"))
    return ledger


def test_foreign_lot_rejected_and_normal_route_ends_at_actual_terminal_operation():
    ledger = lot_ledger("WO-A", "lot-a", 10)
    ledger.seed(lots.FlowLot("lot-b", "WO-B", "main", 10, "piece", "queued"))
    source = operation("first", "WO-A", "m1", 2, lot_ids=("lot-a",))
    terminal = operation("second", "WO-A", "m2", 3, predecessors=("first",), lot_ids=("lot-a",))
    foreign_operation = operation("foreign", "WO-A", "m1", 1, lot_ids=("lot-b",))
    before_foreign = ledger.get("lot-b")
    with pytest.raises(runtime.ProductionRuntimeError):
        runtime.run(
            contracts.ProductionProblem((resource("m1"),), (foreign_operation,)),
            runtime.RuntimeInputs(ledger, materials.MaterialLedger(), external.ExternalLedger()),
        )
    assert ledger.get("lot-b") == before_foreign

    flow = runtime.ExternalWorkflow(
        external.ExternalOperation("outside", "vendor", "plate", order_id="WO-A"),
        "first",
        operation("finish", "WO-A", "m2", 1),
        "lot-b",
        10,
        "piece",
        (runtime.ExternalReceiptPlan(10, 0, 0, 10, "receipt-foreign"),),
    )
    before = ledger.get("lot-b")
    with pytest.raises(runtime.ProductionRuntimeError):
        runtime.run(
            contracts.ProductionProblem((resource("m1"), resource("m2")), (source, terminal)),
            runtime.RuntimeInputs(
                ledger,
                materials.MaterialLedger(),
                external.ExternalLedger(),
                external_workflows=(flow,),
            ),
        )
    assert ledger.get("lot-b") == before

    good = lot_ledger("WO-GOOD", "lot-good", 1)
    first = operation("first", "WO-GOOD", "m1", 1, lot_ids=("lot-good",))
    second = operation("second", "WO-GOOD", "m2", 1, predecessors=("first",), lot_ids=("lot-good",))
    result = runtime.run(
        contracts.ProductionProblem((resource("m1"), resource("m2")), (first, second)),
        runtime.RuntimeInputs(good, materials.MaterialLedger(), external.ExternalLedger()),
    )
    assert result.quantity_balances["WO-GOOD"].accepted_qty == 1
    assert good.get("lot-good").operation_id == "second"


def test_qualification_samples_are_explicitly_material_accounted_and_exhaustion_is_partial():
    ledger = lot_ledger("WO-Q", "lot-q", 1)
    stock = materials.MaterialLedger()
    stock.add_lot(
        materials.MaterialLot("sample-coil", "WIRE", "spec", 4, "lb", availability_time=5)
    )
    state = qualification.QualificationState(
        qualification.QualificationPlan("PART", "m1", "r1", 1, ("inspect",), {"size": (0, 1)}, 2)
    )
    main = operation("main", "WO-Q", "m1", 1, lot_ids=("lot-q",), qualification_id="q")
    workflow = runtime.QualificationWorkflow(
        "q",
        state,
        operation("sample", "WO-Q", "m1", 1),
        (operation("inspect", "WO-Q", "m1", 1),),
        operation("adjust", "WO-Q", "m1", 1),
        (
            runtime.QualificationAttempt(False, {"size": 2}),
            runtime.QualificationAttempt(True, {"size": 0.5}),
        ),
    )
    inputs = runtime.RuntimeInputs(
        ledger,
        stock,
        external.ExternalLedger(),
        qualification_workflows=(workflow,),
        material_requirements={"sample": (materials.MaterialRequirement("WIRE", 1, "lb"),)},
        material_consumptions={
            "sample": (materials.MaterialConsumption("sample-coil", 1, "lb", 1, 0),)
        },
    )
    result = runtime.run(contracts.ProductionProblem((resource("m1"),), (main,)), inputs)
    assert len(result.qualification_samples) == 2
    assert all(item.material_accounting == "quantified" for item in result.qualification_samples)
    assert {item.qty for item in result.qualification_samples} == {1}
    assert {item.uom for item in result.qualification_samples} == {"piece"}
    assert {item.disposition for item in result.qualification_samples} == {"retained"}
    assert len({item.sample_lot_id for item in result.qualification_samples}) == 2
    sample_assignments = [
        item for item in result.schedule.assignments if item.operation_id.endswith(":sample")
    ]
    assert len(sample_assignments) == 2
    assert all(item.start >= 5 for item in sample_assignments)
    assert stock.ledger("sample-coil").production_qty == 2

    exhausted_ledger = lot_ledger("WO-Q2", "lot-q2", 1)
    exhausted_stock = materials.MaterialLedger()
    exhausted_stock.add_lot(materials.MaterialLot("sample-coil-2", "WIRE", "spec", 1, "lb"))
    exhausted_state = qualification.QualificationState(
        qualification.QualificationPlan("PART", "m1", "r1", 1, ("inspect2",), {"size": (0, 1)}, 1)
    )
    exhausted = runtime.QualificationWorkflow(
        "q2",
        exhausted_state,
        operation("sample2", "WO-Q2", "m1", 1),
        (operation("inspect2", "WO-Q2", "m1", 1),),
        operation("adjust2", "WO-Q2", "m1", 1),
        (runtime.QualificationAttempt(False, {"size": 2}),),
    )
    blocked = operation("blocked", "WO-Q2", "m1", 1, lot_ids=("lot-q2",), qualification_id="q2")
    partial = runtime.run(
        contracts.ProductionProblem((resource("m1"),), (blocked,)),
        runtime.RuntimeInputs(
            exhausted_ledger,
            exhausted_stock,
            external.ExternalLedger(),
            qualification_workflows=(exhausted,),
            material_requirements={"sample2": (materials.MaterialRequirement("WIRE", 1, "lb"),)},
            material_consumptions={
                "sample2": (materials.MaterialConsumption("sample-coil-2", 1, "lb", 1, 0),)
            },
        ),
    )
    assert partial.completed is False
    assert exhausted_state.status == "unresolved"
    assigned_ids = {item.operation_id for item in partial.schedule.assignments}
    assert any(item.startswith("q2:trial:1:sample") for item in assigned_ids)
    assert any(item.startswith("q2:trial:1:test:inspect2") for item in assigned_ids)
    assert "blocked" not in assigned_ids
    assert len(partial.qualification_samples) == 1
    assert exhausted_stock.ledger("sample-coil-2").production_qty == 1
    assert partial.quantity_balances["WO-Q2"].accepted_qty == 0


def test_known_vendors_use_independent_windows_and_early_receipt_rolls_back():
    ledger = lot_ledger("WO-V", "lot-v", 2)
    source = operation("source", "WO-V", "m1", 1, lot_ids=("lot-v",))
    vendor_a = operation("vendor-a-template", "WO-V", "va", 2)
    vendor_b = operation("vendor-b-template", "WO-V2", "vb", 2)

    def workflow(operation_id, vendor, receipt_at, event_id):
        return runtime.ExternalWorkflow(
            external.ExternalOperation(
                operation_id, "vendor", "service", unknown_capacity=False, order_id="WO-V"
            ),
            "source",
            operation(f"finish-{operation_id}", "WO-V", "m1", 1),
            "lot-v",
            2,
            "piece",
            (runtime.ExternalReceiptPlan(2, 0, 0, receipt_at, event_id),),
            vendor_operation=vendor,
        )

    # Separate lots keep the two vendor flows independent.
    ledger.demand("WO-V2", 2, "piece")
    ledger.seed(lots.FlowLot("lot-v2", "WO-V2", "source2", 2, "piece", "queued"))
    source2 = operation("source2", "WO-V2", "m1", 1, lot_ids=("lot-v2",))
    flow_a = workflow("outside-a", vendor_a, 30, "receipt-a")
    flow_b = runtime.ExternalWorkflow(
        external.ExternalOperation(
            "outside-b", "vendor", "service", unknown_capacity=False, order_id="WO-V2"
        ),
        "source2",
        operation("finish-b", "WO-V2", "m1", 1),
        "lot-v2",
        2,
        "piece",
        (runtime.ExternalReceiptPlan(2, 0, 0, 30, "receipt-b"),),
        vendor_operation=vendor_b,
    )
    result = runtime.run(
        contracts.ProductionProblem(
            (
                resource("m1"),
                resource("va", ((10, 30),)),
                resource("vb", ((20, 40),)),
            ),
            (source, source2),
        ),
        runtime.RuntimeInputs(
            ledger,
            materials.MaterialLedger(),
            external.ExternalLedger(),
            external_workflows=(flow_a, flow_b),
        ),
    )
    assert result.schedule.status == "FEASIBLE"
    assert all(item.receipt.at >= 30 for item in result.external_receipts)
    vendor_a_assignment = next(
        item for item in result.schedule.assignments if item.operation_id == "outside-a:vendor"
    )
    vendor_b_assignment = next(
        item for item in result.schedule.assignments if item.operation_id == "outside-b:vendor"
    )
    assert vendor_a_assignment.start >= 10 and vendor_a_assignment.end <= 30
    assert vendor_b_assignment.start >= 20 and vendor_b_assignment.end <= 40

    bad_ledger = lot_ledger("WO-BAD-V", "lot-bad-v", 1)
    bad_source = operation("bad-source", "WO-BAD-V", "m1", 1, lot_ids=("lot-bad-v",))
    bad_flow = runtime.ExternalWorkflow(
        external.ExternalOperation(
            "outside-bad", "vendor", "service", unknown_capacity=False, order_id="WO-BAD-V"
        ),
        "bad-source",
        operation("finish-bad", "WO-BAD-V", "m1", 1),
        "lot-bad-v",
        1,
        "piece",
        (runtime.ExternalReceiptPlan(1, 0, 0, 1, "receipt-bad"),),
        vendor_operation=operation("vendor-bad", "WO-BAD-V", "va", 10),
    )
    with pytest.raises(runtime.ProductionRuntimeError):
        runtime.run(
            contracts.ProductionProblem((resource("m1"), resource("va")), (bad_source,)),
            runtime.RuntimeInputs(
                bad_ledger,
                materials.MaterialLedger(),
                external.ExternalLedger(),
                external_workflows=(bad_flow,),
            ),
        )
    assert bad_ledger.get("lot-bad-v").state == "queued"


def test_empty_and_partial_returns_keep_vendor_wip_and_only_accept_returns_downstream():
    empty_ledger = lot_ledger("WO-EMPTY", "lot-empty", 10)
    empty_external = external.ExternalLedger()
    empty_flow = runtime.ExternalWorkflow(
        external.ExternalOperation("outside-empty", "vendor", "service", order_id="WO-EMPTY"),
        "source-empty",
        operation("finish-empty", "WO-EMPTY", "m1", 1),
        "lot-empty",
        10,
        "piece",
        (),
    )
    empty_result = runtime.run(
        contracts.ProductionProblem(
            (resource("m1"),),
            (operation("source-empty", "WO-EMPTY", "m1", 1, lot_ids=("lot-empty",)),),
        ),
        runtime.RuntimeInputs(
            empty_ledger,
            materials.MaterialLedger(),
            empty_external,
            external_workflows=(empty_flow,),
        ),
    )
    assert empty_result.quantity_balances["WO-EMPTY"].accepted_qty == 0
    assert empty_result.quantity_balances["WO-EMPTY"].wip_qty == 10
    assert empty_external.shortage("outside-empty") == 10
    assert empty_result.external_receipts == ()

    ledger = lot_ledger("WO-P", "lot-p", 100)
    source = operation("source", "WO-P", "m1", 1, lot_ids=("lot-p",))
    flow = runtime.ExternalWorkflow(
        external.ExternalOperation("outside-p", "vendor", "service", order_id="WO-P"),
        "source",
        operation("finish-p", "WO-P", "m1", 1),
        "lot-p",
        100,
        "piece",
        (runtime.ExternalReceiptPlan(40, 0, 0, 10, "receipt-p"),),
    )
    result = runtime.run(
        contracts.ProductionProblem((resource("m1"),), (source,)),
        runtime.RuntimeInputs(
            ledger,
            materials.MaterialLedger(),
            external.ExternalLedger(),
            external_workflows=(flow,),
        ),
    )
    assert result.quantity_balances["WO-P"].accepted_qty == 40
    assert result.quantity_balances["WO-P"].wip_qty == 60
    assert any(item.operation_id.startswith("finish-p") for item in result.schedule.assignments)


def test_customer_promise_uses_actual_downstream_completion_and_pending_service_is_none():
    ledger = lot_ledger("WO-C", "lot-c", 10)
    source = operation("source", "WO-C", "m1", 1, lot_ids=("lot-c",))
    flow = runtime.ExternalWorkflow(
        external.ExternalOperation("outside-c", "vendor", "service", order_id="WO-C"),
        "source",
        operation("finish-c", "WO-C", "m1", 1),
        "lot-c",
        10,
        "piece",
        (runtime.ExternalReceiptPlan(10, 0, 0, 100, "receipt-c"),),
    )
    problem = contracts.ProductionProblem(
        (resource("m1"),),
        (source,),
        jobs=(
            contracts.ProductionJob(
                "WO-C", ("source",), demand_kind="customer", promised_ship_time=50
            ),
        ),
    )
    result = runtime.run(
        problem,
        runtime.RuntimeInputs(
            ledger,
            materials.MaterialLedger(),
            external.ExternalLedger(),
            external_workflows=(flow,),
        ),
    )
    assert result.schedule.job_results[0].completion_time >= 101
    assert result.schedule.job_results[0].on_time is False
    assert result.schedule.customer_on_time_fraction == 0.0


def test_partial_run_preserves_requested_cohort_and_marks_executed_subset():
    ledger = lot_ledger("WO-PENDING", "lot-pending", 10)
    source = operation("source", "WO-PENDING", "m1", 1, lot_ids=("lot-pending",))
    flow = runtime.ExternalWorkflow(
        external.ExternalOperation("outside-pending", "vendor", "service", order_id="WO-PENDING"),
        "source",
        operation("finish-pending", "WO-PENDING", "m1", 1),
        "lot-pending",
        10,
        "piece",
        (),
    )
    cohort = contracts.ScenarioCohort("requested", "snapshot", ("WO-PENDING",), 10)
    problem = contracts.ProductionProblem(
        (resource("m1"),),
        (source,),
        jobs=(
            contracts.ProductionJob(
                "WO-PENDING", ("source",), demand_kind="customer", promised_ship_time=20
            ),
        ),
        cohort=cohort,
    )
    result = runtime.run(
        problem,
        runtime.RuntimeInputs(
            ledger,
            materials.MaterialLedger(),
            external.ExternalLedger(),
            external_workflows=(flow,),
        ),
    )
    assert result.requested_problem == problem
    assert result.completed is False
    assert "WO-PENDING" in result.unresolved_job_ids
    assert result.expanded_problem.cohort != cohort
    assert result.customer_on_time_fraction is None
    assert all(item.job_id != "WO-PENDING" for item in result.schedule.job_results)
    assert result.quantity_balances["WO-PENDING"].accepted_qty == 0
    assert result.quantity_balances["WO-PENDING"].wip_qty == 10
