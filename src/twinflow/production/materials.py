"""Identified material lots, atomic reservations, and mass ledgers."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from threading import RLock

from .state_validation import require_same_uom, validate_positive, validate_quantity, validate_uom


class MaterialUnavailableError(ValueError):
    """The required material set cannot be reserved atomically."""


class MaterialBalanceError(ValueError):
    """A consumption or reweigh would make a material balance invalid."""


class DuplicateMaterialLotError(ValueError):
    """A material lot identifier is already present."""


class UnitConversionMissingError(ValueError):
    """A requested unit conversion has no verified revision."""


class UnitMismatchError(ValueError):
    """A material quantity uses the wrong unit."""


class ReplayConflictError(ValueError):
    """An event identifier was reused with different content."""


@dataclass(frozen=True, slots=True)
class MaterialLot:
    lot_id: str
    item_id: str
    spec: str
    available_qty: float
    uom: str
    quality_state: str = "available"
    availability_time: float = 0.0

    def __post_init__(self) -> None:
        if not self.lot_id or not self.item_id:
            raise ValueError("lot_id and item_id are required")
        object.__setattr__(
            self, "available_qty", validate_quantity(self.available_qty, field="available_qty")
        )
        object.__setattr__(self, "uom", validate_uom(self.uom))
        object.__setattr__(
            self,
            "availability_time",
            validate_quantity(self.availability_time, field="availability_time"),
        )


@dataclass(frozen=True, slots=True)
class MaterialRequirement:
    item_id: str
    qty: float
    uom: str
    basis: str = "exact"

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id is required")
        object.__setattr__(self, "qty", validate_positive(self.qty, field="requirement qty"))
        object.__setattr__(self, "uom", validate_uom(self.uom))


@dataclass(frozen=True, slots=True)
class MaterialConsumption:
    lot_id: str
    qty: float
    uom: str
    production_qty: float
    scrap_qty: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "qty", validate_positive(self.qty, field="consumption qty"))
        object.__setattr__(
            self, "production_qty", validate_quantity(self.production_qty, field="production qty")
        )
        object.__setattr__(self, "scrap_qty", validate_quantity(self.scrap_qty, field="scrap qty"))
        object.__setattr__(self, "uom", validate_uom(self.uom))
        if not math.isclose(
            self.production_qty + self.scrap_qty, self.qty, rel_tol=0.0, abs_tol=1e-9
        ):
            raise ValueError("production_qty plus scrap_qty must equal qty")


@dataclass(frozen=True, slots=True)
class MaterialAllocation:
    lot_id: str
    item_id: str
    qty: float
    uom: str


@dataclass(frozen=True, slots=True)
class MaterialReservation:
    reservation_id: str
    requirements: tuple[MaterialRequirement, ...]
    allocations: tuple[MaterialAllocation, ...]
    at: float
    status: str = "reserved"


@dataclass(frozen=True, slots=True)
class MaterialBalance:
    lot_id: str
    remaining_qty: float
    uom: str
    production_qty: float = 0.0
    scrap_qty: float = 0.0
    reweigh_qty: float = 0.0


@dataclass(frozen=True, slots=True)
class ConsumptionRecord:
    event_id: str
    reservation_id: str
    entries: tuple[MaterialConsumption, ...]


class MaterialLedger:
    """Thread-safe material reservation and consumption ledger."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._lots: dict[str, MaterialLot] = {}
        self._reserved: dict[str, float] = {}
        self._reservations: dict[str, MaterialReservation] = {}
        self._reservation_objects: dict[str, MaterialReservation] = {}
        self._events: dict[str, ConsumptionRecord] = {}
        self._reweigh_events: dict[str, tuple[str, str, float, float]] = {}
        self._balances: dict[str, MaterialBalance] = {}
        self._counter = 0

    def add_lot(self, lot: MaterialLot) -> MaterialLot:
        with self._lock:
            if lot.lot_id in self._lots:
                raise DuplicateMaterialLotError(lot.lot_id)
            self._lots[lot.lot_id] = lot
            self._balances[lot.lot_id] = MaterialBalance(lot.lot_id, lot.available_qty, lot.uom)
            self._reserved[lot.lot_id] = 0.0
            return lot

    def replace_lot(self, lot: MaterialLot) -> MaterialLot:
        """Update readiness metadata while preserving measured balance and identity."""

        with self._lock:
            current = self.lot(lot.lot_id)
            if (
                current.item_id != lot.item_id
                or current.spec != lot.spec
                or current.uom != lot.uom
                or current.available_qty != lot.available_qty
            ):
                raise MaterialBalanceError("replace_lot may only update readiness metadata")
            self._lots[lot.lot_id] = lot
            return lot

    def lot(self, lot_id: str) -> MaterialLot:
        with self._lock:
            try:
                return self._lots[lot_id]
            except KeyError as error:
                raise KeyError(lot_id) from error

    def available(self, lot_id: str) -> float:
        """Return unreserved, unconsumed quantity available for a new reservation."""

        with self._lock:
            lot = self.lot(lot_id)
            return lot.available_qty - self._reserved[lot_id]

    def earliest_ready_time(self, requirements: Sequence[MaterialRequirement]) -> float:
        """Return the earliest time at which every requirement can be reserved."""

        if not requirements:
            raise ValueError("at least one material requirement is required")
        grouped: dict[tuple[str, str], float] = {}
        for requirement in requirements:
            key = (requirement.item_id, requirement.uom)
            grouped[key] = grouped.get(key, 0.0) + requirement.qty
        with self._lock:
            ready_times: list[float] = []
            for (item_id, uom), required in grouped.items():
                total = 0.0
                ready: float | None = None
                candidates = sorted(
                    (
                        lot
                        for lot in self._lots.values()
                        if lot.item_id == item_id
                        and lot.uom == uom
                        and lot.quality_state == "available"
                    ),
                    key=lambda lot: (lot.availability_time, lot.lot_id),
                )
                for lot in candidates:
                    total += lot.available_qty - self._reserved[lot.lot_id]
                    if total + 1e-9 >= required:
                        ready = lot.availability_time
                        break
                if ready is None:
                    raise MaterialUnavailableError(f"insufficient material {item_id}")
                ready_times.append(ready)
            return max(ready_times)

    def ledger(self, lot_id: str) -> MaterialBalance:
        with self._lock:
            return self._balances[lot_id]

    def reserve(
        self,
        requirements: Sequence[MaterialRequirement],
        at: float,
        reservation_id: str | None = None,
    ) -> MaterialReservation:
        """Atomically reserve every requirement, selecting identifiable lots."""

        if not requirements:
            raise ValueError("at least one material requirement is required")
        available_at = validate_quantity(at, field="reservation time")
        with self._lock:
            reservation_key = reservation_id or self._new_id("reservation")
            if reservation_key in self._reservations:
                prior = self._reservations[reservation_key]
                candidate = tuple(requirements)
                if prior.requirements == candidate and prior.at == available_at:
                    return prior
                raise ReplayConflictError(reservation_key)
            allocations: list[MaterialAllocation] = []
            reserved_delta: dict[str, float] = {}
            for requirement in requirements:
                remaining = requirement.qty
                candidates = sorted(
                    (
                        lot
                        for lot in self._lots.values()
                        if lot.item_id == requirement.item_id
                        and lot.quality_state == "available"
                        and lot.availability_time <= available_at
                    ),
                    key=lambda lot: (lot.availability_time, lot.lot_id),
                )
                if not candidates:
                    raise MaterialUnavailableError(f"no lot for material {requirement.item_id}")
                for lot in candidates:
                    if lot.uom != requirement.uom:
                        continue
                    free = (
                        lot.available_qty
                        - self._reserved[lot.lot_id]
                        - reserved_delta.get(lot.lot_id, 0.0)
                    )
                    if free <= 0:
                        continue
                    amount = min(remaining, free)
                    allocations.append(MaterialAllocation(lot.lot_id, lot.item_id, amount, lot.uom))
                    reserved_delta[lot.lot_id] = reserved_delta.get(lot.lot_id, 0.0) + amount
                    remaining -= amount
                    if remaining <= 1e-9:
                        break
                if remaining > 1e-9:
                    differing_uom = any(
                        lot.item_id == requirement.item_id and lot.uom != requirement.uom
                        for lot in candidates
                    )
                    if differing_uom:
                        raise UnitConversionMissingError(
                            f"no verified conversion for {requirement.item_id} to {requirement.uom}"
                        )
                    raise MaterialUnavailableError(f"insufficient material {requirement.item_id}")
            for lot_id, amount in reserved_delta.items():
                self._reserved[lot_id] += amount
            reservation = MaterialReservation(
                reservation_key, tuple(requirements), tuple(allocations), available_at
            )
            self._reservations[reservation_key] = reservation
            self._reservation_objects[reservation_key] = reservation
            return reservation

    def consume(
        self,
        reservation: MaterialReservation,
        entries: Sequence[MaterialConsumption],
        event_id: str,
    ) -> ConsumptionRecord:
        """Consume explicit per-lot entries exactly once."""

        if not event_id:
            raise ValueError("event_id is required")
        candidate_entries = tuple(entries)
        with self._lock:
            current = self._reservations.get(reservation.reservation_id)
            owner = self._reservation_objects.get(reservation.reservation_id)
            if current is None or owner is not reservation:
                raise ValueError("reservation is unknown or not active")
            prior = self._events.get(event_id)
            if prior is not None:
                if (
                    prior.reservation_id == reservation.reservation_id
                    and prior.entries == candidate_entries
                ):
                    return prior
                raise ReplayConflictError(event_id)
            if current.status != "reserved":
                raise ValueError("reservation is unknown or not active")
            expected: dict[tuple[str, str], float] = {}
            for allocation in reservation.allocations:
                key = (allocation.lot_id, allocation.uom)
                expected[key] = expected.get(key, 0.0) + allocation.qty
            actual: dict[tuple[str, str], float] = {}
            for entry in candidate_entries:
                key = (entry.lot_id, entry.uom)
                actual[key] = actual.get(key, 0.0) + entry.qty
            if set(actual) != set(expected) or any(
                not math.isclose(actual[key], expected[key], rel_tol=0.0, abs_tol=1e-9)
                for key in expected
            ):
                raise MaterialBalanceError(
                    "consumption entries must exactly consume reservation allocations"
                )
            for entry in candidate_entries:
                lot = self.lot(entry.lot_id)
                require_same_uom(lot.uom, entry.uom)
                if (
                    entry.qty > lot.available_qty + 1e-9
                    or entry.qty > self._reserved[entry.lot_id] + 1e-9
                ):
                    raise MaterialBalanceError(f"consumption exceeds balance for {entry.lot_id}")
            for entry in candidate_entries:
                self._lots[entry.lot_id] = replace(
                    self._lots[entry.lot_id],
                    available_qty=self._lots[entry.lot_id].available_qty - entry.qty,
                )
                self._reserved[entry.lot_id] -= entry.qty
                old = self._balances[entry.lot_id]
                self._balances[entry.lot_id] = replace(
                    old,
                    remaining_qty=old.remaining_qty - entry.qty,
                    production_qty=old.production_qty + entry.production_qty,
                    scrap_qty=old.scrap_qty + entry.scrap_qty,
                )
            updated = replace(current, status="consumed")
            self._reservations[reservation.reservation_id] = updated
            record = ConsumptionRecord(event_id, reservation.reservation_id, candidate_entries)
            self._events[event_id] = record
            return record

    def release(self, reservation: MaterialReservation) -> MaterialReservation:
        """Release an unused reservation; repeated release is idempotent."""

        with self._lock:
            current = self._reservations.get(reservation.reservation_id)
            owner = self._reservation_objects.get(reservation.reservation_id)
            if current is None or owner is not reservation:
                raise ValueError("reservation is unknown")
            if current.status == "released":
                return current
            if current.status != "reserved":
                raise ValueError("consumed reservation cannot be released")
            for allocation in current.allocations:
                self._reserved[allocation.lot_id] -= allocation.qty
            updated = replace(current, status="released")
            self._reservations[reservation.reservation_id] = updated
            return updated

    def reweigh(
        self,
        lot_id: str,
        measured_qty: float,
        uom: str,
        at: float,
        event_id: str | None = None,
    ) -> MaterialLot:
        """Apply an explicit measured remaining weight to an identified lot."""

        measured = validate_quantity(measured_qty, field="measured_qty")
        timestamp = validate_quantity(at, field="measurement time")
        with self._lock:
            lot = self.lot(lot_id)
            require_same_uom(lot.uom, uom)
            if event_id is not None:
                payload = (lot_id, uom, measured, timestamp)
                previous_event = self._reweigh_events.get(event_id)
                if previous_event is not None:
                    if previous_event == payload:
                        return lot
                    raise ReplayConflictError(event_id)
                self._reweigh_events[event_id] = payload
            elif measured > lot.available_qty + 1e-9:
                raise MaterialBalanceError("unidentified reweigh cannot increase measured balance")
            if measured + 1e-9 < self._reserved[lot_id]:
                raise MaterialBalanceError("reweigh is below outstanding reservation")
            updated = replace(lot, available_qty=measured)
            self._lots[lot_id] = updated
            balance = self._balances[lot_id]
            self._balances[lot_id] = replace(balance, remaining_qty=measured, reweigh_qty=measured)
            return updated

    def _new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"
