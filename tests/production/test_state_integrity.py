"""Cross-state integrity regressions identified during the production review."""

from __future__ import annotations

from importlib import import_module

import pytest


def _qualification_api():
    try:
        module = import_module("twinflow.production.qualification")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(f"MISSING FEATURE twinflow.production.qualification: {error}", pytrace=False)
    required = (
        "QualificationPlan",
        "QualificationState",
        "QualificationStateError",
        "QualificationLimitError",
        "UnknownTrialError",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(
            f"MISSING FEATURE twinflow.production.qualification public API: {', '.join(missing)}",
            pytrace=False,
        )
    return module


def _lots_api():
    try:
        module = import_module("twinflow.production.lots")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(f"MISSING FEATURE twinflow.production.lots: {error}", pytrace=False)
    required = ("FlowLot", "LotLedger")
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(
            f"MISSING FEATURE twinflow.production.lots public API: {', '.join(missing)}",
            pytrace=False,
        )
    return module


def _external_api():
    try:
        module = import_module("twinflow.production.external")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(f"MISSING FEATURE twinflow.production.external: {error}", pytrace=False)
    required = ("ExternalOperation", "ExternalLedger")
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(
            f"MISSING FEATURE twinflow.production.external public API: {', '.join(missing)}",
            pytrace=False,
        )
    return module


def _plan(api, *, revision: str = "rev-1", acceptance: object | None = None, **kwargs):
    values = {
        "part_id": "PART-1",
        "machine_id": "529",
        "recipe_revision": revision,
        "sample_qty": 1,
        "test_route": ("inspection",),
        "acceptance_spec": {"diameter_mm": (1.0, 1.1)} if acceptance is None else acceptance,
        "max_iterations": 2,
    }
    values.update(kwargs)
    return api.QualificationPlan(
        **values,
    )


def test_qualification_trial_ids_include_revision_and_context() -> None:
    api = _qualification_api()
    first = api.QualificationState(_plan(api, revision="rev-1"))
    second = api.QualificationState(_plan(api, revision="rev-2"))
    trial_one = first.start_trial()
    trial_two = second.start_trial()

    assert trial_one.sample_lot_id != trial_two.sample_lot_id
    with pytest.raises(api.UnknownTrialError):
        first.record_result(
            trial_two.sample_lot_id, passed=True, measurements={"diameter_mm": 1.05}
        )
    first.record_result(trial_one.sample_lot_id, passed=True, measurements={"diameter_mm": 1.05})
    assert first.approved_revision == "rev-1"
    assert second.ready_for_production is False


def test_qualification_requires_finite_sample_and_integer_limit_and_boolean_result() -> None:
    api = _qualification_api()
    with pytest.raises((ValueError, TypeError)):
        _plan(api, sample_qty=float("nan"))
    with pytest.raises((ValueError, TypeError)):
        _plan(api, max_iterations=1.5)
    state = api.QualificationState(_plan(api))
    trial = state.start_trial()
    with pytest.raises((ValueError, TypeError)):
        state.record_result(trial.sample_lot_id, passed="false", measurements={"diameter_mm": 1.05})
    assert state.ready_for_production is False
    state.record_result(trial.sample_lot_id, passed=True, measurements={"diameter_mm": 1.05})


def test_qualification_rejects_missing_acceptance_and_invalid_result_keeps_trial_pending() -> None:
    api = _qualification_api()
    with pytest.raises((ValueError, TypeError)):
        _plan(api, acceptance={})
    state = api.QualificationState(_plan(api))
    trial = state.start_trial()
    with pytest.raises((ValueError, TypeError)):
        state.record_result(trial.sample_lot_id, passed=True, measurements=None)
    state.record_result(trial.sample_lot_id, passed=True, measurements={"diameter_mm": 1.05})
    assert state.ready_for_production is True


def test_qualification_plan_deep_freezes_nested_revision_evidence() -> None:
    api = _qualification_api()
    evidence = {"evidence_id": "sheet-1", "review": {"approved_by": "operator"}, "approved": True}
    plan = _plan(api, green_shortcut=evidence)
    evidence["review"]["approved_by"] = "tampered"
    assert plan.green_shortcut["review"]["approved_by"] == "operator"
    with pytest.raises(TypeError):
        plan.green_shortcut["review"]["approved_by"] = "tampered"


def test_qualification_plan_freezes_tuple_bounds_and_plan_reference() -> None:
    api = _qualification_api()
    source = {"diameter_mm": (1.0, 1.1)}
    plan = _plan(api, acceptance=source)
    state = api.QualificationState(plan)
    source["diameter_mm"] = (0.0, 99.0)
    assert state.plan.acceptance_spec["diameter_mm"] == (1.0, 1.1)
    with pytest.raises(AttributeError):
        state.plan = _plan(api, revision="rev-other")
    with pytest.raises(ValueError):
        _plan(api, acceptance={"diameter_mm": (float("nan"), 1.1)})
    with pytest.raises(ValueError):
        _plan(api, acceptance={"diameter_mm": (1.2, 1.1)})


def test_invalid_boolean_measurement_keeps_qualification_trial_pending() -> None:
    api = _qualification_api()
    state = api.QualificationState(_plan(api))
    trial = state.start_trial()
    with pytest.raises((TypeError, ValueError)):
        state.record_result(trial.sample_lot_id, passed=True, measurements={"diameter_mm": True})
    assert state.status == "awaiting_test"
    state.record_result(trial.sample_lot_id, passed=True, measurements={"diameter_mm": 1.05})
    assert state.ready_for_production is True


def test_reweigh_event_replay_does_not_restore_consumed_material() -> None:
    materials = import_module("twinflow.production.materials")
    ledger = materials.MaterialLedger()
    ledger.add_lot(
        materials.MaterialLot(
            lot_id="coil-reweigh",
            item_id="WIRE-REWEIGH",
            spec="round wire",
            available_qty=100,
            uom="lb",
        )
    )
    ledger.reweigh("coil-reweigh", measured_qty=90, uom="lb", at=1, event_id="weigh-1")
    reservation = ledger.reserve(
        [materials.MaterialRequirement(item_id="WIRE-REWEIGH", qty=10, uom="lb")], at=2
    )
    ledger.consume(
        reservation,
        entries=(
            materials.MaterialConsumption(
                lot_id="coil-reweigh", qty=10, uom="lb", production_qty=10, scrap_qty=0
            ),
        ),
        event_id="consume-reweigh",
    )
    ledger.reweigh("coil-reweigh", measured_qty=90, uom="lb", at=1, event_id="weigh-1")
    assert ledger.available("coil-reweigh") == 80


def test_terminal_scrap_lot_cannot_be_resurrected_and_contradictory_wip_is_unresolved() -> None:
    api = _lots_api()
    ledger = api.LotLedger()
    ledger.demand("WO-300", target_qty=100, uom="piece", completed_good=40, unreleased=0)
    ledger.seed(
        api.FlowLot(
            id="scrap-lot",
            order_id="WO-300",
            operation_id="0010",
            qty=10,
            uom="piece",
            state="scrap",
        )
    )
    with pytest.raises(ValueError):
        ledger.transfer("scrap-lot", operation_id="0020", state="active")
    ledger.seed(
        api.FlowLot(
            id="wip-lot",
            order_id="WO-300",
            operation_id="0020",
            qty=70,
            uom="piece",
            state="wip",
        )
    )
    report = ledger.reconcile(
        order_id="WO-300",
        target_qty=100,
        completed_good=40,
        operation_remaining={"0020": 70},
        source_remaining={"0020": 70},
    )
    assert report.unresolved is True


def test_external_rejects_early_receipt_and_customer_shipment() -> None:
    api = _external_api()
    ledger = api.ExternalLedger()
    ledger.register(
        api.ExternalOperation(
            operation_id="inspect-op",
            order_id="WO-400",
            vendor="Inspector",
            service="inspect",
        )
    )
    inspect = ledger.dispatch(
        "inspect-op", lot_id="lot-400", qty=100, uom="piece", at=10, event_id="dispatch-i"
    )
    with pytest.raises(ValueError):
        ledger.receive(
            "inspect-op",
            shipment_id=inspect.shipment_id,
            accepted_qty=100,
            rejected_qty=0,
            lost_qty=0,
            at=9,
            event_id="early-receipt",
        )
    with pytest.raises(ValueError):
        ledger.customer_ship(
            "WO-400", lot_id="lot-400", qty=1, uom="piece", at=29, event_id="early-customer"
        )


def test_external_current_lot_stage_does_not_double_count_or_accept_wrong_uom() -> None:
    api = _external_api()
    ledger = api.ExternalLedger()
    ledger.register(
        api.ExternalOperation(
            operation_id="inspect-op",
            order_id="WO-400",
            vendor="Inspector",
            service="inspect",
        )
    )
    ledger.register(
        api.ExternalOperation(
            operation_id="plate-op",
            order_id="WO-400",
            vendor="Plater",
            service="plate",
        )
    )
    inspect = ledger.dispatch(
        "inspect-op", lot_id="lot-400", qty=100, uom="piece", at=10, event_id="dispatch-i2"
    )
    ledger.receive(
        "inspect-op",
        shipment_id=inspect.shipment_id,
        accepted_qty=100,
        rejected_qty=0,
        lost_qty=0,
        at=20,
        event_id="receipt-i2",
    )
    plate = ledger.dispatch(
        "plate-op", lot_id="lot-400", qty=100, uom="piece", at=21, event_id="dispatch-p2"
    )
    ledger.receive(
        "plate-op",
        shipment_id=plate.shipment_id,
        accepted_qty=100,
        rejected_qty=0,
        lost_qty=0,
        at=30,
        event_id="receipt-p2",
    )
    assert ledger.customer_shipped("WO-400") == 0
    with pytest.raises(ValueError):
        ledger.customer_ship(
            "WO-400", lot_id="lot-400", qty=200, uom="piece", at=31, event_id="ship-bad"
        )
    with pytest.raises(ValueError):
        ledger.customer_ship(
            "WO-400", lot_id="lot-400", qty=1, uom="kg", at=31, event_id="ship-uom"
        )
    shipped = ledger.customer_ship(
        "WO-400", lot_id="lot-400", qty=100, uom="piece", at=31, event_id="ship-ok"
    )
    assert shipped.qty == 100


def test_external_receipt_cannot_move_back_before_lot_stage_clock() -> None:
    api = _external_api()
    ledger = api.ExternalLedger()
    ledger.register(
        api.ExternalOperation(
            operation_id="stage-a", order_id="WO-500", vendor="A", service="inspect"
        )
    )
    outbound = ledger.dispatch("stage-a", "lot-500", 10, "piece", at=10, event_id="dispatch-500")
    ledger.receive("stage-a", outbound.shipment_id, 10, 0, 0, at=20, event_id="receipt-500")
    with pytest.raises(ValueError):
        ledger.receive("stage-a", outbound.shipment_id, 0, 0, 0, at=19, event_id="receipt-500-late")
