"""Acceptance tests for attributed WIP lots and remaining-time state (TF-WS-004).

The production module is intentionally imported inside each test while the public
contract is being implemented.  A missing module is reported as a feature gap,
rather than making test collection fail with an opaque import error.
"""

from __future__ import annotations

from importlib import import_module

import pytest


def _api():
    try:
        module = import_module("twinflow.production.lots")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(f"MISSING FEATURE twinflow.production.lots: {error}", pytrace=False)
    required = ("FlowLot", "LotLedger", "DuplicateLotError")
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(
            f"MISSING FEATURE twinflow.production.lots public API: {', '.join(missing)}",
            pytrace=False,
        )
    return module


def test_split_stage_wip_preserves_order_identity_and_quantity_balance() -> None:
    api = _api()
    lot_a = api.FlowLot(
        id="lot-20",
        order_id="WO-100",
        operation_id="0020",
        qty=20,
        uom="piece",
        state="wip",
        location="queue:0020",
        as_of="2026-08-07T11:46:00-06:00",
    )
    lot_b = api.FlowLot(
        id="lot-30",
        order_id="WO-100",
        operation_id="0030",
        qty=30,
        uom="piece",
        state="wip",
        location="queue:0030",
        as_of="2026-08-07T11:46:00-06:00",
    )
    ledger = api.LotLedger()
    ledger.demand("WO-100", target_qty=50, uom="piece", completed_good=0, unreleased=0)
    ledger.seed(lot_a)
    ledger.seed(lot_b)

    balance = ledger.balance("WO-100")

    assert balance.target_qty == 50
    assert balance.wip_qty == 50
    assert balance.accepted_qty == 0
    assert balance.unreleased_qty == 0
    assert {lot.order_id for lot in ledger.lots_for_order("WO-100")} == {"WO-100"}
    assert {lot.operation_id for lot in ledger.lots_for_order("WO-100")} == {"0020", "0030"}


def test_lot_split_advance_accept_and_scrap_conserve_original_quantity() -> None:
    api = _api()
    ledger = api.LotLedger()
    ledger.demand("WO-103", target_qty=50, uom="piece", completed_good=0, unreleased=0)
    ledger.seed(
        api.FlowLot(
            id="lot-whole",
            order_id="WO-103",
            operation_id="0020",
            qty=50,
            uom="piece",
            state="wip",
        )
    )

    ledger.split("lot-whole", qty=20, new_lot_id="lot-split")
    ledger.transfer("lot-split", operation_id="0030", state="wip", location="queue:0030")
    ledger.accept("lot-split", qty=20)
    ledger.scrap("lot-whole", qty=5, reason="setup damage")

    balance = ledger.balance("WO-103")
    assert balance.accepted_qty == 20
    assert balance.wip_qty == 25
    assert balance.scrap_qty == 5
    assert balance.accepted_qty + balance.wip_qty + balance.scrap_qty == 50


def test_invalid_lot_quantities_uom_and_duplicate_ids_are_rejected() -> None:
    api = _api()
    with pytest.raises((ValueError, TypeError)):
        api.FlowLot(
            id="bad",
            order_id="WO-104",
            operation_id="0010",
            qty=-1,
            uom="piece",
            state="wip",
        )
    with pytest.raises((ValueError, TypeError)):
        api.FlowLot(
            id="nan",
            order_id="WO-104",
            operation_id="0010",
            qty=float("nan"),
            uom="piece",
            state="wip",
        )
    ledger = api.LotLedger()
    lot = api.FlowLot(
        id="lot-duplicate",
        order_id="WO-104",
        operation_id="0010",
        qty=1,
        uom="piece",
        state="wip",
    )
    ledger.seed(lot)
    with pytest.raises(api.DuplicateLotError):
        ledger.seed(lot)
    with pytest.raises(ValueError):
        ledger.accept("lot-duplicate", qty=2, uom="kg")


def test_active_lot_resumes_remaining_time_and_does_not_duplicate_raw_release() -> None:
    api = _api()
    active = api.FlowLot(
        id="lot-active",
        order_id="WO-101",
        operation_id="0010",
        qty=40,
        uom="piece",
        state="active",
        machine_id="441",
        remaining_time=10,
        setup_state="complete",
    )
    ledger = api.LotLedger()
    ledger.demand("WO-101", target_qty=100, uom="piece", completed_good=60, unreleased=0)
    ledger.seed(active)

    assert ledger.resume_time("lot-active") == 10
    balance = ledger.balance("WO-101")
    assert balance.wip_qty == 40
    assert balance.accepted_qty == 60
    assert balance.unreleased_qty == 0
    assert balance.raw_release_qty == 0

    with pytest.raises(AttributeError):
        active.qty = 41


def test_contradictory_snapshot_is_unresolved_and_never_silently_reconciled() -> None:
    api = _api()
    ledger = api.LotLedger()
    report = ledger.reconcile(
        order_id="WO-102",
        target_qty=100,
        completed_good=60,
        operation_remaining={"0010": 0, "0020": 70},
        source_remaining={"0010": 40, "0020": 40},
    )

    assert report.unresolved is True
    assert any("0010" in str(issue) for issue in report.issues)
