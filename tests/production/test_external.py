"""Acceptance tests for outside processing, partial returns and shipment identity (TF-WS-009)."""

from __future__ import annotations

from importlib import import_module

import pytest


def _api():
    try:
        module = import_module("twinflow.production.external")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(f"MISSING FEATURE twinflow.production.external: {error}", pytrace=False)
    required = (
        "ExternalOperation",
        "ExternalLedger",
        "OverReceiptError",
        "OverShipmentError",
        "ReplayConflictError",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(
            f"MISSING FEATURE twinflow.production.external public API: {', '.join(missing)}",
            pytrace=False,
        )
    return module


def test_vendor_dispatch_and_partial_receipt_do_not_complete_customer_order() -> None:
    api = _api()
    ledger = api.ExternalLedger()
    ledger.register(
        api.ExternalOperation(
            operation_id="WO-200:0040",
            vendor="Plater-1",
            service="passivation",
            unknown_capacity=True,
        )
    )

    outbound = ledger.dispatch(
        "WO-200:0040", lot_id="lot-200", qty=100, uom="piece", at=20, event_id="dispatch-200"
    )
    assert (
        ledger.dispatch(
            "WO-200:0040", lot_id="lot-200", qty=100, uom="piece", at=20, event_id="dispatch-200"
        )
        == outbound
    )
    with pytest.raises(api.ReplayConflictError):
        ledger.dispatch(
            "WO-200:0040", lot_id="lot-200", qty=99, uom="piece", at=20, event_id="dispatch-200"
        )
    assert outbound.kind == "shipped_to_vendor"
    assert ledger.customer_shipped("WO-200") == 0
    receipt = ledger.receive(
        "WO-200:0040",
        shipment_id=outbound.shipment_id,
        accepted_qty=95,
        rejected_qty=0,
        lost_qty=5,
        at=80,
        event_id="receipt-200",
    )
    replay = ledger.receive(
        "WO-200:0040",
        shipment_id=outbound.shipment_id,
        accepted_qty=95,
        rejected_qty=0,
        lost_qty=5,
        at=80,
        event_id="receipt-200",
    )
    assert replay == receipt
    assert receipt.accepted_qty == 95
    assert receipt.lost_qty == 5
    assert ledger.downstream_available("WO-200:0040") == 95
    assert ledger.shortage("WO-200:0040") == 5
    assert ledger.customer_shipped("WO-200") == 0
    with pytest.raises(api.ReplayConflictError):
        ledger.receive(
            "WO-200:0040",
            shipment_id=outbound.shipment_id,
            accepted_qty=94,
            rejected_qty=1,
            lost_qty=5,
            at=80,
            event_id="receipt-200",
        )
    with pytest.raises(api.OverReceiptError):
        ledger.receive(
            "WO-200:0040",
            shipment_id=outbound.shipment_id,
            accepted_qty=1,
            rejected_qty=0,
            lost_qty=0,
            at=81,
            event_id="receipt-201",
        )


def test_partial_accepted_lots_flow_downstream_and_customer_ship_is_explicit() -> None:
    api = _api()
    ledger = api.ExternalLedger()
    ledger.register(
        api.ExternalOperation(operation_id="WO-201:0040", vendor="Plater-2", service="plate")
    )
    outbound = ledger.dispatch(
        "WO-201:0040", lot_id="lot-201", qty=100, uom="piece", at=1, event_id="dispatch-201"
    )
    ledger.receive(
        "WO-201:0040",
        shipment_id=outbound.shipment_id,
        accepted_qty=40,
        rejected_qty=2,
        lost_qty=3,
        at=2,
        event_id="receipt-201a",
    )
    ledger.receive(
        "WO-201:0040",
        shipment_id=outbound.shipment_id,
        accepted_qty=55,
        rejected_qty=0,
        lost_qty=0,
        at=3,
        event_id="receipt-201b",
    )
    assert ledger.downstream_available("WO-201:0040") == 95
    assert ledger.customer_shipped("WO-201") == 0
    customer_event = ledger.customer_ship(
        "WO-201", lot_id="lot-201", qty=95, uom="piece", at=4, event_id="customer-201"
    )
    assert customer_event.kind == "shipped_to_customer"
    assert ledger.customer_shipped("WO-201") == 95
    assert (
        ledger.customer_ship(
            "WO-201", lot_id="lot-201", qty=95, uom="piece", at=4, event_id="customer-201"
        )
        == customer_event
    )
    with pytest.raises(api.OverShipmentError):
        ledger.customer_ship(
            "WO-201", lot_id="lot-201", qty=1, uom="piece", at=5, event_id="customer-202"
        )
