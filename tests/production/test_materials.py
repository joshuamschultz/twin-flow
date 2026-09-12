"""Acceptance tests for coil identity, atomic requirements and mass ledgers (TF-WS-007)."""

from __future__ import annotations

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
        "DuplicateMaterialLotError",
        "UnitConversionMissingError",
        "UnitMismatchError",
        "ReplayConflictError",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(
            f"MISSING FEATURE twinflow.production.materials public API: {', '.join(missing)}",
            pytrace=False,
        )
    return module


def test_multi_material_reservation_is_atomic_and_consumed_once() -> None:
    api = _api()
    ledger = api.MaterialLedger()
    ledger.add_lot(
        api.MaterialLot(
            lot_id="coil-A",
            item_id="WIRE-01",
            spec="0.027in stainless",
            available_qty=1000,
            uom="lb",
        )
    )
    requirements = [
        api.MaterialRequirement(item_id="WIRE-01", qty=495, uom="lb", basis="exact"),
        api.MaterialRequirement(item_id="LUBE-01", qty=2, uom="lb", basis="exact"),
    ]

    with pytest.raises(api.MaterialUnavailableError):
        ledger.reserve(requirements, at=0)
    assert ledger.available("coil-A") == 1000

    ledger.add_lot(
        api.MaterialLot(
            lot_id="lube-A",
            item_id="LUBE-01",
            spec="forming lubricant",
            available_qty=2,
            uom="lb",
        )
    )
    reservation = ledger.reserve(requirements, at=1)
    entries = (
        api.MaterialConsumption(
            lot_id="coil-A",
            qty=495,
            uom="lb",
            production_qty=455,
            scrap_qty=40,
        ),
        api.MaterialConsumption(
            lot_id="lube-A",
            qty=2,
            uom="lb",
            production_qty=2,
            scrap_qty=0,
        ),
    )
    ledger.consume(reservation, entries=entries, event_id="consume-1")
    ledger.consume(reservation, entries=entries, event_id="consume-1")

    assert ledger.available("coil-A") == 505
    assert ledger.available("lube-A") == 0
    assert ledger.ledger("coil-A").production_qty == 455
    assert ledger.ledger("coil-A").scrap_qty == 40
    with pytest.raises(api.ReplayConflictError):
        ledger.consume(
            reservation,
            entries=(
                api.MaterialConsumption(
                    lot_id="coil-A",
                    qty=495,
                    uom="lb",
                    production_qty=454,
                    scrap_qty=41,
                ),
                entries[1],
            ),
            event_id="consume-1",
        )


def test_two_jobs_cannot_double_reserve_one_coil_and_release_allows_retry() -> None:
    api = _api()
    ledger = api.MaterialLedger()
    ledger.add_lot(
        api.MaterialLot(
            lot_id="coil-shared",
            item_id="WIRE-04",
            spec="round wire",
            available_qty=100,
            uom="lb",
        )
    )
    requirement = [api.MaterialRequirement(item_id="WIRE-04", qty=70, uom="lb", basis="exact")]
    first = ledger.reserve(requirement, at=0, reservation_id="job-1")
    with pytest.raises(api.MaterialUnavailableError):
        ledger.reserve(requirement, at=0, reservation_id="job-2")
    ledger.release(first)
    second = ledger.reserve(requirement, at=0, reservation_id="job-2")
    assert second.reservation_id == "job-2"


def test_reweigh_preserves_coil_identity_and_rejects_negative_balance() -> None:
    api = _api()
    with pytest.raises((ValueError, TypeError)):
        api.MaterialLot(
            lot_id="bad-negative",
            item_id="WIRE-02",
            spec="flat wire",
            available_qty=-1,
            uom="lb",
        )
    with pytest.raises((ValueError, TypeError)):
        api.MaterialLot(
            lot_id="bad-infinite",
            item_id="WIRE-02",
            spec="flat wire",
            available_qty=float("inf"),
            uom="lb",
        )
    ledger = api.MaterialLedger()
    ledger.add_lot(
        api.MaterialLot(
            lot_id="coil-B",
            item_id="WIRE-02",
            spec="flat wire",
            available_qty=505,
            uom="lb",
        )
    )
    ledger.reweigh("coil-B", measured_qty=504.5, uom="lb", at=10)
    assert ledger.lot("coil-B").available_qty == 504.5
    assert ledger.lot("coil-B").lot_id == "coil-B"
    assert ledger.ledger("coil-B").reweigh_qty == 504.5

    with pytest.raises((ValueError, api.MaterialBalanceError)):
        ledger.reweigh("coil-B", measured_qty=-1, uom="lb", at=11)
    with pytest.raises((ValueError, api.UnitMismatchError)):
        ledger.reweigh("coil-B", measured_qty=5, uom="kg", at=12)
    with pytest.raises(api.DuplicateMaterialLotError):
        ledger.add_lot(
            api.MaterialLot(
                lot_id="coil-B",
                item_id="WIRE-02",
                spec="flat wire",
                available_qty=1,
                uom="lb",
            )
        )


def test_piece_conversion_without_measured_revision_fails_explicitly() -> None:
    api = _api()
    ledger = api.MaterialLedger()
    ledger.add_lot(
        api.MaterialLot(
            lot_id="coil-C",
            item_id="WIRE-03",
            spec="unknown wire",
            available_qty=100,
            uom="lb",
        )
    )
    requirement = api.MaterialRequirement(item_id="WIRE-03", qty=100, uom="piece", basis="piece")

    with pytest.raises(api.UnitConversionMissingError):
        ledger.reserve([requirement], at=0)
