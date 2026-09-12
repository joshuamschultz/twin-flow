"""Acceptance tests for calendars and the integrated production runtime."""

from __future__ import annotations

from importlib import import_module

import pytest


def _api():
    try:
        contracts = import_module("twinflow.production.contracts")
        scheduling = import_module("twinflow.production.scheduling")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(
            f"MISSING FEATURE twinflow.production runtime contracts: {error}", pytrace=False
        )
    return contracts, scheduling


def _runtime_api():
    try:
        contracts = import_module("twinflow.production.contracts")
        runtime = import_module("twinflow.production.runtime")
        lots = import_module("twinflow.production.lots")
        materials = import_module("twinflow.production.materials")
        qualification = import_module("twinflow.production.qualification")
        external = import_module("twinflow.production.external")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(
            f"MISSING FEATURE twinflow.production runtime integration: {error}", pytrace=False
        )
    required = (
        "RuntimeInputs",
        "QualificationAttempt",
        "QualificationWorkflow",
        "ExternalReceiptPlan",
        "ExternalWorkflow",
        "run",
    )
    missing = [name for name in required if not hasattr(runtime, name)]
    if missing:
        pytest.fail(
            f"MISSING FEATURE twinflow.production.runtime public API: {', '.join(missing)}",
            pytrace=False,
        )
    return contracts, runtime, lots, materials, qualification, external


def _resource(api, resource_id: str, kind: str, windows, qualifications=frozenset()):
    return api.Resource(
        id=resource_id,
        kind=kind,
        capacity=1,
        windows=tuple(api.TimeWindow(start, end) for start, end in windows),
        qualifications=qualifications,
    )


def _operation(
    api,
    operation_id: str,
    order_id: str,
    resource_id: str,
    duration: float,
    *,
    revision: str = "r1",
    **kwargs,
):
    return api.ProductionOperation(
        id=operation_id,
        order_id=order_id,
        alternatives=(
            api.RecipeAlternative(
                id=f"{operation_id}-recipe",
                primary_resource_id=resource_id,
                revision=revision,
                phases=(api.Phase("run", duration),),
                uses=(api.ResourceUse((resource_id,), "run", "run"),),
            ),
        ),
        **kwargs,
    )


def test_public_runtime_schedules_dynamic_gates_and_moves_attributed_quantity() -> None:
    """TF-004/007/008/009 compose through one real run, not isolated ledger helpers."""

    api, runtime, lot_api, material_api, qualification_api, external_api = _runtime_api()
    lot_ledger = lot_api.LotLedger()
    lot_ledger.demand("WO-RUN", 10, "piece")
    lot_ledger.seed(lot_api.FlowLot("lot-run", "WO-RUN", "main", 10, "piece", "queued"))
    lot_ledger.demand("WO-WIP", 5, "piece")
    lot_ledger.seed(
        lot_api.FlowLot(
            "lot-wip",
            "WO-WIP",
            "resume",
            5,
            "piece",
            "active",
            remaining_time=20,
            machine_id="coiler-2",
            setup_state="rev-wip",
        )
    )
    assert lot_ledger.balance("WO-WIP").raw_release_qty == 0

    material_ledger = material_api.MaterialLedger()
    material_ledger.add_lot(material_api.MaterialLot("coil-1", "WIRE", "synthetic wire", 10, "lb"))
    qualification_state = qualification_api.QualificationState(
        qualification_api.QualificationPlan(
            part_id="PART-RUN",
            machine_id="coiler-1",
            recipe_revision="rev-main",
            sample_qty=1,
            test_route=("stress-relief", "inspection"),
            acceptance_spec={"diameter_mm": (1.0, 1.1)},
            max_iterations=2,
        )
    )
    external_ledger = external_api.ExternalLedger()
    outside = external_api.ExternalOperation(
        operation_id="WO-RUN:outside",
        order_id="WO-RUN",
        vendor="synthetic-vendor",
        service="plate",
        unknown_capacity=True,
    )

    main = _operation(
        api,
        "main",
        "WO-RUN",
        "coiler-1",
        30,
        revision="rev-main",
        lot_ids=("lot-run",),
        qualification_id="qual-main",
    )
    resume = _operation(
        api,
        "resume",
        "WO-WIP",
        "coiler-2",
        50,
        revision="rev-wip",
        lot_ids=("lot-wip",),
    )
    problem = api.ProductionProblem(
        resources=tuple(
            _resource(api, resource_id, kind, ((0, 300),))
            for resource_id, kind in (
                ("coiler-1", "machine"),
                ("coiler-2", "machine"),
                ("oven", "machine"),
                ("inspection", "labor"),
                ("packer", "labor"),
            )
        ),
        operations=(main, resume),
    )
    workflow = runtime.QualificationWorkflow(
        id="qual-main",
        state=qualification_state,
        sample_operation=_operation(api, "sample", "WO-RUN", "coiler-1", 5),
        test_operations=(
            _operation(api, "stress-relief", "WO-RUN", "oven", 7),
            _operation(api, "inspection", "WO-RUN", "inspection", 3),
        ),
        adjustment_operation=_operation(api, "adjustment", "WO-RUN", "coiler-1", 4),
        attempts=(
            runtime.QualificationAttempt(False, {"diameter_mm": 1.2}),
            runtime.QualificationAttempt(True, {"diameter_mm": 1.05}),
        ),
    )
    external_flow = runtime.ExternalWorkflow(
        operation=outside,
        source_operation_id="main",
        downstream_operation=_operation(api, "pack", "WO-RUN", "packer", 6),
        lot_id="lot-run",
        qty=10,
        uom="piece",
        receipts=(
            runtime.ExternalReceiptPlan(6, 0, 0, at=100, event_id="receipt-1"),
            runtime.ExternalReceiptPlan(4, 0, 0, at=130, event_id="receipt-2"),
        ),
    )
    inputs = runtime.RuntimeInputs(
        lot_ledger=lot_ledger,
        material_ledger=material_ledger,
        external_ledger=external_ledger,
        qualification_workflows=(workflow,),
        external_workflows=(external_flow,),
        material_requirements={
            "main": (material_api.MaterialRequirement("WIRE", 10, "lb", "exact"),)
        },
        material_consumptions={
            "main": (material_api.MaterialConsumption("coil-1", 10, "lb", 9, 1),)
        },
    )

    result = runtime.run(problem, inputs, solver="baseline")
    assert result.schedule.status == "FEASIBLE"
    assert result.verification.valid is True
    assignments = {item.operation_id: item for item in result.schedule.assignments}
    qualification_runs = result.qualification_runs
    assert len(qualification_runs) == 2
    assert qualification_runs[0].trial.sample_lot_id != qualification_runs[1].trial.sample_lot_id
    assert qualification_runs[0].result.kind == "adjustment"
    assert qualification_runs[0].adjustment_operation_id in assignments
    assert qualification_runs[1].result.kind == "approved"
    assert qualification_runs[1].adjustment_operation_id is None
    for run in qualification_runs:
        assert run.sample_operation_id in assignments
        assert all(operation_id in assignments for operation_id in run.test_operation_ids)
    second_test_end = max(
        assignments[item].end for item in qualification_runs[1].test_operation_ids
    )
    assert assignments["main"].start >= second_test_end
    assert qualification_state.ready_for_production is True

    resumed_work = sum(
        segment.end - segment.start
        for segment in assignments["resume"].segments
        if segment.kind == "work"
    )
    assert resumed_work == 20
    assert result.quantity_balances["WO-WIP"].raw_release_qty == 0
    assert material_ledger.ledger("coil-1").remaining_qty == 0

    receipts = result.external_receipts
    assert [item.receipt.accepted_qty for item in receipts] == [6, 4]
    assert [lot_ledger.get(item.ready_lot_id).qty for item in receipts] == [6, 4]
    assert [lot_ledger.get(item.ready_lot_id).operation_id for item in receipts] == [
        "pack",
        "pack",
    ]
    first_pack = assignments[receipts[0].downstream_operation_id]
    second_pack = assignments[receipts[1].downstream_operation_id]
    assert first_pack.start >= 100
    assert first_pack.end <= second_pack.start
    assert second_pack.start >= 130
    assert external_ledger.downstream_available("WO-RUN:outside") == 10
    assert external_ledger.customer_shipped("WO-RUN") == 0
    final_balance = result.quantity_balances["WO-RUN"]
    assert final_balance.accepted_qty == 10
    assert final_balance.wip_qty == 0


def test_one_setter_serializes_setups_while_unattended_machine_runs_overlap() -> None:
    api, scheduling = _api()
    resources = (
        _resource(api, "m1", "machine", ((0, 200),)),
        _resource(api, "m2", "machine", ((0, 200),)),
        _resource(api, "setter", "labor", ((0, 200),), frozenset({"setup"})),
    )

    def operation(operation_id: str, machine_id: str):
        return api.ProductionOperation(
            id=operation_id,
            order_id=operation_id,
            alternatives=(
                api.RecipeAlternative(
                    id=machine_id,
                    primary_resource_id=machine_id,
                    revision="r1",
                    phases=(api.Phase("setup", 10), api.Phase("run", 60)),
                    uses=(
                        api.ResourceUse((machine_id,), "setup", "run"),
                        api.ResourceUse(
                            ("setter",),
                            "setup",
                            "setup",
                            qualifications=frozenset({"setup"}),
                        ),
                    ),
                ),
            ),
        )

    problem = api.ProductionProblem(
        resources=resources,
        operations=(operation("op-1", "m1"), operation("op-2", "m2")),
    )
    schedule = scheduling.solve(problem, solver="baseline")
    setter = sorted(
        (
            occupation
            for assignment in schedule.assignments
            for occupation in assignment.occupations
            if occupation.resource_id == "setter"
        ),
        key=lambda item: item.start,
    )
    run_segments = sorted(
        (
            segment
            for assignment in schedule.assignments
            for segment in assignment.segments
            if segment.phase_id == "run"
        ),
        key=lambda item: item.start,
    )

    assert setter[0].end <= setter[1].start
    assert run_segments[0].start < run_segments[1].end
    assert run_segments[1].start < run_segments[0].end
    assert scheduling.verify(problem, schedule).valid is True


def test_interruptible_work_resumes_remaining_time_and_charges_one_restart() -> None:
    api, scheduling = _api()
    machine = _resource(api, "m1", "machine", ((0, 60), (120, 200)))
    setter = _resource(api, "setter", "labor", ((0, 60), (120, 200)), frozenset({"setup"}))
    restart = api.RestartRule(
        idle_threshold=30,
        duration=10,
        resource_ids=("setter",),
        qualifications=frozenset({"setup"}),
    )
    operation = api.ProductionOperation(
        id="paused",
        order_id="WO-PAUSE",
        alternatives=(
            api.RecipeAlternative(
                id="paused-r1",
                primary_resource_id="m1",
                revision="r1",
                phases=(api.Phase("run", 120, interruptible=True, restart_rule=restart),),
                uses=(api.ResourceUse(("m1",), "run", "run", hold_during_pause=True),),
            ),
        ),
    )
    problem = api.ProductionProblem(resources=(machine, setter), operations=(operation,))
    schedule = scheduling.solve(problem, solver="baseline")
    assignment = schedule.assignments[0]
    work = [segment for segment in assignment.segments if segment.kind == "work"]
    restarts = [segment for segment in assignment.segments if segment.kind == "restart"]

    assert [(item.start, item.end) for item in work] == [(0, 60), (130, 190)]
    assert [(item.start, item.end) for item in restarts] == [(120, 130)]
    assert sum(item.end - item.start for item in work) == 120
    restart_uses = [item for item in assignment.occupations if item.kind == "restart"]
    assert [(item.resource_id, item.start, item.end) for item in restart_uses] == [
        ("setter", 120, 130)
    ]
    machine_hold = next(
        item for item in assignment.occupations if item.resource_id == "m1" and item.kind == "hold"
    )
    assert (machine_hold.start, machine_hold.end) == (0, 190)
    assert scheduling.verify(problem, schedule).valid is True


def test_unattended_run_finishes_off_shift_but_unload_waits_for_labor() -> None:
    api, scheduling = _api()
    operation = api.ProductionOperation(
        id="unattended",
        order_id="WO-U",
        alternatives=(
            api.RecipeAlternative(
                id="unattended-r1",
                primary_resource_id="m1",
                revision="r1",
                phases=(api.Phase("run", 60), api.Phase("unload", 5)),
                uses=(
                    api.ResourceUse(("m1",), "run", "unload", hold_during_pause=True),
                    api.ResourceUse(
                        ("operator",),
                        "unload",
                        "unload",
                        qualifications=frozenset({"unload"}),
                    ),
                ),
            ),
        ),
    )
    problem = api.ProductionProblem(
        resources=(
            _resource(api, "m1", "machine", ((0, 200),)),
            _resource(
                api,
                "operator",
                "labor",
                ((0, 30), (100, 200)),
                frozenset({"unload"}),
            ),
        ),
        operations=(operation,),
    )
    schedule = scheduling.solve(problem, solver="baseline")
    assignment = schedule.assignments[0]
    segments = {segment.phase_id: segment for segment in assignment.segments}

    assert (segments["run"].start, segments["run"].end) == (0, 60)
    assert (segments["unload"].start, segments["unload"].end) == (100, 105)
    assert assignment.end == 105
    assert scheduling.verify(problem, schedule).valid is True


def test_skill_mismatch_is_rejected_even_when_machine_is_available() -> None:
    api, scheduling = _api()
    operation = api.ProductionOperation(
        id="inspect",
        order_id="WO-I",
        alternatives=(
            api.RecipeAlternative(
                id="inspect-r1",
                primary_resource_id="m1",
                revision="r1",
                phases=(api.Phase("inspect", 10),),
                uses=(
                    api.ResourceUse(("m1",), "inspect", "inspect"),
                    api.ResourceUse(
                        ("worker",),
                        "inspect",
                        "inspect",
                        qualifications=frozenset({"inspector"}),
                    ),
                ),
            ),
        ),
    )
    problem = api.ProductionProblem(
        resources=(
            _resource(api, "m1", "machine", ((0, 100),)),
            _resource(api, "worker", "labor", ((0, 100),), frozenset({"setter"})),
        ),
        operations=(operation,),
    )
    issues = scheduling.validate(problem)
    assert any(issue.code == "qualification_mismatch" for issue in issues)
    assert scheduling.solve(problem, solver="baseline").status == "INFEASIBLE"
