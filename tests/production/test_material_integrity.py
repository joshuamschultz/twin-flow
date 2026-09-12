"""Regression tests for material reservation integrity and event ownership."""

from __future__ import annotations

from dataclasses import replace
from importlib import import_module

import pytest


def _api():
    try:
        module = import_module("twinflow.production.materials")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(f"MISSING FEATURE twinflow.production.materials: {error}", pytrace=False)
    required = (
        "MaterialLot",
        "MaterialRequirement",
        "MaterialConsumption",
        "MaterialLedger",
        "MaterialUnavailableError",
        "MaterialBalanceError",
        "ReplayConflictError",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(
            f"MISSING FEATURE twinflow.production.materials public API: {', '.join(missing)}",
            pytrace=False,
        )
    return module


def test_reservation_honors_availability_time_and_quality_state() -> None:
    api = _api()
    ledger = api.MaterialLedger()
    ledger.add_lot(
        api.MaterialLot(
            lot_id="future-coil",
            item_id="WIRE-FUTURE",
            spec="round wire",
            available_qty=50,
            uom="lb",
            quality_state="available",
            availability_time=10,
        )
    )
    requirement = [api.MaterialRequirement(item_id="WIRE-FUTURE", qty=5, uom="lb")]

    with pytest.raises(api.MaterialUnavailableError):
        ledger.reserve(requirement, at=9)
    ledger.add_lot(
        api.MaterialLot(
            lot_id="held-coil",
            item_id="WIRE-HELD",
            spec="round wire",
            available_qty=50,
            uom="lb",
            quality_state="hold",
            availability_time=0,
        )
    )
    held_requirement = [api.MaterialRequirement(item_id="WIRE-HELD", qty=5, uom="lb")]
    with pytest.raises(api.MaterialUnavailableError):
        ledger.reserve(held_requirement, at=10)

    released = replace(ledger.lot("held-coil"), quality_state="available")
    ledger.replace_lot(released)
    reservation = ledger.reserve(held_requirement, at=10)
    assert reservation.allocations[0].lot_id == "held-coil"


def test_repeated_requirements_on_one_lot_aggregate_during_consumption() -> None:
    api = _api()
    ledger = api.MaterialLedger()
    ledger.add_lot(
        api.MaterialLot(
            lot_id="shared-coil",
            item_id="WIRE-SHARED",
            spec="round wire",
            available_qty=100,
            uom="lb",
        )
    )
    reservation = ledger.reserve(
        [
            api.MaterialRequirement(item_id="WIRE-SHARED", qty=60, uom="lb"),
            api.MaterialRequirement(item_id="WIRE-SHARED", qty=30, uom="lb"),
        ],
        at=0,
    )
    record = ledger.consume(
        reservation,
        entries=(
            api.MaterialConsumption(
                lot_id="shared-coil",
                qty=90,
                uom="lb",
                production_qty=80,
                scrap_qty=10,
            ),
        ),
        event_id="shared-consume",
    )

    assert record.entries[0].qty == 90
    assert ledger.available("shared-coil") == 10


def test_forged_reservation_cannot_release_or_replay_consume() -> None:
    api = _api()
    ledger = api.MaterialLedger()
    ledger.add_lot(
        api.MaterialLot(
            lot_id="owned-coil",
            item_id="WIRE-OWNED",
            spec="round wire",
            available_qty=100,
            uom="lb",
        )
    )
    reservation = ledger.reserve(
        [api.MaterialRequirement(item_id="WIRE-OWNED", qty=20, uom="lb")],
        at=0,
        reservation_id="owned-reservation",
    )
    forged = replace(reservation)
    with pytest.raises((ValueError, api.ReplayConflictError)):
        ledger.release(forged)
    entries = (
        api.MaterialConsumption(
            lot_id="owned-coil",
            qty=20,
            uom="lb",
            production_qty=20,
            scrap_qty=0,
        ),
    )
    ledger.consume(reservation, entries=entries, event_id="owned-consume")
    with pytest.raises((ValueError, api.ReplayConflictError)):
        ledger.consume(forged, entries=entries, event_id="owned-consume")
