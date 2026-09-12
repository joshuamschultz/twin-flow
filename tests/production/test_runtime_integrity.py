"""Integrated runtime regressions for state atomicity and attribution."""

from __future__ import annotations

from importlib import import_module

import pytest


def _api():
    return tuple(
        import_module(name)
        for name in (
            "twinflow.production.contracts",
            "twinflow.production.runtime",
            "twinflow.production.lots",
            "twinflow.production.materials",
            "twinflow.production.qualification",
            "twinflow.production.external",
        )
    )


def _resource(api, resource_id: str, end: float = 100.0):
    return api.Resource(
        resource_id,
        "machine",
        1,
        (api.TimeWindow(0, end),),
    )


def _operation(api, operation_id: str, order_id: str, resource_id: str, duration: float, **kwargs):
    return api.ProductionOperation(
        operation_id,
        order_id,
        (
            api.RecipeAlternative(
                f"{operation_id}-recipe",
                resource_id,
                "r1",
                (api.Phase("run", duration),),
                (api.ResourceUse((resource_id,), "run", "run"),),
            ),
        ),
        **kwargs,
    )


def test_infeasible_schedule_rolls_back_qualification_and_material_state() -> None:
    api, runtime, lots, materials, qualification, external = _api()
    lot_ledger = lots.LotLedger()
    lot_ledger.demand("WO-ATOMIC", 1, "piece")
    lot_ledger.seed(lots.FlowLot("lot-atomic", "WO-ATOMIC", "main", 1, "piece", "queued"))
    material_ledger = materials.MaterialLedger()
    material_ledger.add_lot(materials.MaterialLot("coil-atomic", "WIRE", "spec", 1, "lb"))
    q_state = qualification.QualificationState(
        qualification.QualificationPlan("PART", "m1", "r1", 1, ("test",), {"size": (0.0, 1.0)}, 1)
    )
    main = _operation(
        api,
        "main",
        "WO-ATOMIC",
        "m1",
        10,
        lot_ids=("lot-atomic",),
        qualification_id="q",
    )
    workflow = runtime.QualificationWorkflow(
        "q",
        q_state,
        _operation(api, "sample", "WO-ATOMIC", "m1", 2),
        (_operation(api, "test", "WO-ATOMIC", "m1", 1),),
        _operation(api, "adjust", "WO-ATOMIC", "m1", 1),
        (runtime.QualificationAttempt(True, {"size": 0.5}),),
    )
    problem = api.ProductionProblem((_resource(api, "m1", 1),), (main,))
    inputs = runtime.RuntimeInputs(
        lot_ledger,
        material_ledger,
        external.ExternalLedger(),
        qualification_workflows=(workflow,),
        material_requirements={"main": (materials.MaterialRequirement("WIRE", 1, "lb"),)},
        material_consumptions={
            "main": (materials.MaterialConsumption("coil-atomic", 1, "lb", 1, 0),)
        },
    )

    result = runtime.run(problem, inputs)

    assert result.schedule.status in {"INFEASIBLE", "UNKNOWN"}
    assert q_state.status == "unqualified"
    assert material_ledger.available("coil-atomic") == 1
    assert lot_ledger.get("lot-atomic").state == "queued"


def test_external_partial_return_uses_arbitrary_terminal_operation_and_preserves_loss() -> None:
    api, runtime, lots, materials, qualification, external = _api()
    lot_ledger = lots.LotLedger()
    lot_ledger.demand("WO-EXT", 100, "piece")
    lot_ledger.seed(lots.FlowLot("lot-ext", "WO-EXT", "main", 100, "piece", "queued"))
    operation = _operation(api, "main", "WO-EXT", "m1", 1, lot_ids=("lot-ext",))
    flow = runtime.ExternalWorkflow(
        external.ExternalOperation("outside", "Vendor", "plate", order_id="WO-EXT"),
        "main",
        _operation(api, "finish", "WO-EXT", "m2", 1),
        "lot-ext",
        100,
        "piece",
        (runtime.ExternalReceiptPlan(95, 0, 5, 10, "receipt-ext"),),
    )
    result = runtime.run(
        api.ProductionProblem(
            (_resource(api, "m1"), _resource(api, "m2")),
            (operation,),
            jobs=(
                api.ProductionJob(
                    "WO-EXT", ("main",), demand_kind="customer", promised_ship_time=50
                ),
            ),
        ),
        runtime.RuntimeInputs(
            lot_ledger,
            materials.MaterialLedger(),
            external.ExternalLedger(),
            external_workflows=(flow,),
        ),
    )

    assert result.verification.valid is True
    assert result.external_receipts[0].downstream_operation_id.startswith("finish")
    assert not any(item.operation_id == "pack" for item in result.schedule.assignments)
    balance = result.quantity_balances["WO-EXT"]
    assert balance.accepted_qty == 95
    assert balance.scrap_qty == 5
    assert balance.wip_qty == 0
    assert result.schedule.job_results == ()
    assert result.completed is False
    assert result.unresolved_job_ids == ("WO-EXT",)
    assert result.customer_on_time_fraction is None


def test_zero_accepted_external_receipt_creates_no_downstream_assignment() -> None:
    api, runtime, lots, materials, qualification, external = _api()
    ledger = lots.LotLedger()
    ledger.demand("WO-ZERO", 10, "piece")
    ledger.seed(lots.FlowLot("lot-zero", "WO-ZERO", "main", 10, "piece", "queued"))
    source = _operation(api, "main", "WO-ZERO", "m1", 1, lot_ids=("lot-zero",))
    flow = runtime.ExternalWorkflow(
        external.ExternalOperation("outside-zero", "Vendor", "plate", order_id="WO-ZERO"),
        "main",
        _operation(api, "finish", "WO-ZERO", "m2", 1),
        "lot-zero",
        10,
        "piece",
        (runtime.ExternalReceiptPlan(0, 0, 10, 10, "receipt-zero"),),
    )
    result = runtime.run(
        api.ProductionProblem((_resource(api, "m1"), _resource(api, "m2")), (source,)),
        runtime.RuntimeInputs(
            ledger,
            materials.MaterialLedger(),
            external.ExternalLedger(),
            external_workflows=(flow,),
        ),
    )

    assert result.verification.valid is True
    assert result.external_receipts[0].ready_lot_id is None
    assert not any(item.operation_id.startswith("finish") for item in result.schedule.assignments)
    assert result.quantity_balances["WO-ZERO"].accepted_qty == 0
    assert result.quantity_balances["WO-ZERO"].scrap_qty == 10


def test_external_dispatch_without_receipt_remains_vendor_wip() -> None:
    api, runtime, lots, materials, qualification, external = _api()
    ledger = lots.LotLedger()
    ledger.demand("WO-PENDING", 10, "piece")
    ledger.seed(lots.FlowLot("lot-pending", "WO-PENDING", "main", 10, "piece", "queued"))
    source = _operation(api, "main", "WO-PENDING", "m1", 1, lot_ids=("lot-pending",))
    flow = runtime.ExternalWorkflow(
        external.ExternalOperation("outside-pending", "Vendor", "plate", order_id="WO-PENDING"),
        "main",
        _operation(api, "finish", "WO-PENDING", "m2", 1),
        "lot-pending",
        10,
        "piece",
        (),
    )
    result = runtime.run(
        api.ProductionProblem((_resource(api, "m1"), _resource(api, "m2")), (source,)),
        runtime.RuntimeInputs(
            ledger,
            materials.MaterialLedger(),
            external.ExternalLedger(),
            external_workflows=(flow,),
        ),
    )
    assert result.completed is False
    assert result.unresolved_obligations == ("external:outside-pending:vendor_quantity_pending",)
    assert ledger.get("lot-pending").state == "wip"
    assert ledger.balance("WO-PENDING").wip_qty == 10


def test_active_wip_drops_completed_setup_phase() -> None:
    api, runtime, lots, materials, qualification, external = _api()
    ledger = lots.LotLedger()
    ledger.demand("WO-WIP", 1, "piece")
    ledger.seed(
        lots.FlowLot(
            "lot-wip-runtime",
            "WO-WIP",
            "resume",
            1,
            "piece",
            "active",
            remaining_time=3,
            machine_id="m1",
            setup_state="r1",
        )
    )
    operation = api.ProductionOperation(
        "resume",
        "WO-WIP",
        (
            api.RecipeAlternative(
                "resume-r1",
                "m1",
                "r1",
                (api.Phase("setup", 9), api.Phase("run", 20)),
                (api.ResourceUse(("m1",), "setup", "run"),),
            ),
        ),
        lot_ids=("lot-wip-runtime",),
    )
    result = runtime.run(
        api.ProductionProblem((_resource(api, "m1"),), (operation,)),
        runtime.RuntimeInputs(ledger, materials.MaterialLedger(), external.ExternalLedger()),
    )

    assignment = result.schedule.assignments[0]
    assert [segment.phase_id for segment in assignment.segments] == ["run"]
    assert assignment.end - assignment.start == 3
    assert result.quantity_balances["WO-WIP"].accepted_qty == 1
    assert result.quantity_balances["WO-WIP"].wip_qty == 0


def test_future_material_readiness_delays_operation_and_wrong_qualification_is_atomic() -> None:
    api, runtime, lots, materials, qualification, external = _api()
    lot_ledger = lots.LotLedger()
    lot_ledger.demand("WO-GATE", 1, "piece")
    lot_ledger.seed(lots.FlowLot("lot-gate", "WO-GATE", "main", 1, "piece", "queued"))
    material_ledger = materials.MaterialLedger()
    material_ledger.add_lot(
        materials.MaterialLot("coil-future", "WIRE", "spec", 1, "lb", availability_time=5)
    )
    main = _operation(api, "main", "WO-GATE", "m1", 1, lot_ids=("lot-gate",))
    inputs = runtime.RuntimeInputs(
        lot_ledger,
        material_ledger,
        external.ExternalLedger(),
        material_requirements={"main": (materials.MaterialRequirement("WIRE", 1, "lb"),)},
        material_consumptions={
            "main": (materials.MaterialConsumption("coil-future", 1, "lb", 1, 0),)
        },
    )
    result = runtime.run(api.ProductionProblem((_resource(api, "m1"),), (main,)), inputs)
    assert result.schedule.assignments[0].start == 5
    assert material_ledger.available("coil-future") == 0
    assert result.quantity_balances["WO-GATE"].accepted_qty == 1

    bad_lot_ledger = lots.LotLedger()
    bad_lot_ledger.demand("WO-BAD", 1, "piece")
    bad_lot_ledger.seed(lots.FlowLot("lot-bad", "WO-BAD", "main", 1, "piece", "queued"))
    bad_main = _operation(
        api, "main", "WO-BAD", "m1", 1, lot_ids=("lot-bad",), qualification_id="missing"
    )
    with pytest.raises(runtime.ProductionRuntimeError):
        runtime.run(
            api.ProductionProblem((_resource(api, "m1"),), (bad_main,)),
            runtime.RuntimeInputs(
                bad_lot_ledger,
                materials.MaterialLedger(),
                external.ExternalLedger(),
            ),
        )
    assert bad_lot_ledger.get("lot-bad").state == "queued"
