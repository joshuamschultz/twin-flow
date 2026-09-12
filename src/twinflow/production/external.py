"""Outside-processing and shipment identity ledger."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from threading import RLock

from .state_validation import validate_positive, validate_quantity, validate_uom


class OverReceiptError(ValueError):
    """A vendor receipt exceeds the dispatched quantity."""


class OverShipmentError(ValueError):
    """A customer shipment exceeds accepted downstream quantity."""


class ReplayConflictError(ValueError):
    """An event identifier was reused with different content."""


@dataclass(frozen=True, slots=True)
class ExternalOperation:
    operation_id: str
    vendor: str
    service: str
    unknown_capacity: bool = True
    order_id: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_id or not self.vendor or not self.service:
            raise ValueError("operation_id, vendor, and service are required")


@dataclass(frozen=True, slots=True)
class VendorShipment:
    shipment_id: str
    operation_id: str
    lot_id: str
    qty: float
    uom: str
    at: float
    kind: str = "shipped_to_vendor"


@dataclass(frozen=True, slots=True)
class VendorReceipt:
    shipment_id: str
    operation_id: str
    accepted_qty: float
    rejected_qty: float
    lost_qty: float
    at: float
    kind: str = "received_from_vendor"


@dataclass(frozen=True, slots=True)
class CustomerShipment:
    shipment_id: str
    order_id: str
    lot_id: str
    qty: float
    uom: str
    at: float
    kind: str = "shipped_to_customer"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class ExternalLedger:
    """Thread-safe ledger separating supplier movement from customer shipment."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._operations: dict[str, ExternalOperation] = {}
        self._dispatches: dict[str, VendorShipment] = {}
        self._receipts: dict[str, VendorReceipt] = {}
        self._customer_shipments: dict[str, CustomerShipment] = {}
        self._event_digests: dict[str, str] = {}
        self._received_totals: dict[str, float] = {}
        self._downstream: dict[str, float] = {}
        self._downstream_uom: dict[str, str] = {}
        self._lot_order: dict[str, str] = {}
        self._lot_uom: dict[str, str] = {}
        self._lot_available: dict[str, float] = {}
        self._lot_last_time: dict[str, float] = {}
        self._customer_totals: dict[str, float] = {}

    def register(self, operation: ExternalOperation) -> ExternalOperation:
        with self._lock:
            if (
                operation.operation_id in self._operations
                and self._operations[operation.operation_id] != operation
            ):
                raise ValueError(f"conflicting operation {operation.operation_id}")
            self._operations[operation.operation_id] = operation
            return operation

    def dispatch(
        self,
        operation_id: str,
        lot_id: str,
        qty: float,
        uom: str,
        at: float,
        event_id: str | None = None,
    ) -> VendorShipment:
        amount = validate_positive(qty, field="dispatch qty")
        unit = validate_uom(uom)
        timestamp = validate_quantity(at, field="dispatch time")
        with self._lock:
            self._require_operation(operation_id)
            key = event_id or f"dispatch:{operation_id}:{len(self._dispatches) + 1}"
            payload = (operation_id, lot_id, amount, unit, timestamp)
            prior = self._check_replay(key, payload)
            if prior is not None:
                return self._dispatches[f"vendor:{prior}"]
            operation = self._operations[operation_id]
            order_id = operation.order_id or operation_id.split(":", 1)[0]
            previous_order = self._lot_order.get(lot_id)
            if previous_order is not None and previous_order != order_id:
                raise ValueError("lot belongs to another order")
            previous_uom = self._lot_uom.get(lot_id)
            if previous_uom is not None and previous_uom != unit:
                raise ValueError("lot unit differs from prior stage")
            previous_time = self._lot_last_time.get(lot_id)
            if previous_time is not None and timestamp < previous_time:
                raise ValueError("dispatch precedes the lot's current stage")
            available = self._lot_available.get(lot_id)
            if available is not None and amount > available + 1e-9:
                raise OverShipmentError("dispatch exceeds accepted quantity at current lot stage")
            self._lot_order[lot_id] = order_id
            self._lot_uom[lot_id] = unit
            if available is not None:
                self._lot_available[lot_id] = available - amount
            else:
                self._lot_available[lot_id] = 0.0
            self._lot_last_time[lot_id] = timestamp
            shipment = VendorShipment(
                f"vendor:{key}", operation_id, lot_id, amount, unit, timestamp
            )
            self._dispatches[shipment.shipment_id] = shipment
            self._event_digests[key] = _digest(payload)
            return shipment

    def receive(
        self,
        operation_id: str,
        shipment_id: str,
        accepted_qty: float,
        rejected_qty: float,
        lost_qty: float,
        at: float,
        event_id: str | None = None,
    ) -> VendorReceipt:
        accepted = validate_quantity(accepted_qty, field="accepted receipt qty")
        rejected = validate_quantity(rejected_qty, field="rejected receipt qty")
        lost = validate_quantity(lost_qty, field="lost receipt qty")
        timestamp = validate_quantity(at, field="receipt time")
        with self._lock:
            self._require_operation(operation_id)
            try:
                dispatch = self._dispatches[shipment_id]
            except KeyError as error:
                raise KeyError(shipment_id) from error
            if dispatch.operation_id != operation_id:
                raise ValueError("shipment belongs to another operation")
            key = event_id or f"receipt:{shipment_id}:{len(self._receipts) + 1}"
            payload = (operation_id, shipment_id, accepted, rejected, lost, timestamp)
            prior = self._check_replay(key, payload)
            if prior is not None:
                return self._receipts[prior]
            if timestamp < dispatch.at:
                raise ValueError("receipt cannot precede supplier dispatch")
            if timestamp < self._lot_last_time.get(dispatch.lot_id, dispatch.at):
                raise ValueError("receipt precedes the lot's current stage")
            total = self._received_totals.get(shipment_id, 0.0) + accepted + rejected + lost
            if total > dispatch.qty + 1e-9:
                raise OverReceiptError(f"receipt exceeds dispatch {shipment_id}")
            prior_uom = self._downstream_uom.get(operation_id)
            if prior_uom is not None and prior_uom != dispatch.uom:
                raise ValueError("receipt unit differs from prior receipt")
            receipt = VendorReceipt(shipment_id, operation_id, accepted, rejected, lost, timestamp)
            self._receipts[key] = receipt
            self._event_digests[key] = _digest(payload)
            self._received_totals[shipment_id] = total
            self._downstream[operation_id] = self._downstream.get(operation_id, 0.0) + accepted
            self._downstream_uom[operation_id] = dispatch.uom
            self._lot_available[dispatch.lot_id] = (
                self._lot_available.get(dispatch.lot_id, 0.0) + accepted
            )
            self._lot_last_time[dispatch.lot_id] = max(
                timestamp, self._lot_last_time.get(dispatch.lot_id, timestamp)
            )
            return receipt

    def customer_ship(
        self,
        order_id: str,
        lot_id: str,
        qty: float,
        uom: str,
        at: float,
        event_id: str | None = None,
    ) -> CustomerShipment:
        amount = validate_positive(qty, field="customer shipment qty")
        unit = validate_uom(uom)
        timestamp = validate_quantity(at, field="customer shipment time")
        with self._lock:
            key = event_id or f"customer:{order_id}:{len(self._customer_shipments) + 1}"
            payload = (order_id, lot_id, amount, unit, timestamp)
            prior = self._check_replay(key, payload)
            if prior is not None:
                return self._customer_shipments[prior]
            if lot_id not in self._lot_order:
                raise ValueError("customer shipment references an unknown lot")
            if self._lot_order[lot_id] != order_id:
                raise ValueError("customer shipment lot belongs to another order")
            require_uom = self._lot_uom[lot_id]
            if require_uom != unit:
                raise ValueError("customer shipment unit differs from accepted quantity")
            if timestamp < self._lot_last_time.get(lot_id, timestamp):
                raise ValueError("customer shipment precedes latest lot receipt")
            available = self._lot_available.get(lot_id, 0.0)
            if amount > available + 1e-9:
                raise OverShipmentError(
                    f"customer shipment exceeds accepted quantity for {order_id}"
                )
            shipment = CustomerShipment(
                f"customer:{key}", order_id, lot_id, amount, unit, timestamp
            )
            self._customer_shipments[key] = shipment
            self._event_digests[key] = _digest(payload)
            self._lot_available[lot_id] = available - amount
            self._lot_last_time[lot_id] = timestamp
            self._customer_totals[order_id] = self._customer_totals.get(order_id, 0.0) + amount
            return shipment

    def downstream_available(self, operation_id: str) -> float:
        with self._lock:
            return self._downstream.get(operation_id, 0.0)

    def shortage(self, operation_id: str) -> float:
        with self._lock:
            dispatch_total = sum(
                s.qty for s in self._dispatches.values() if s.operation_id == operation_id
            )
            accepted = sum(
                r.accepted_qty for r in self._receipts.values() if r.operation_id == operation_id
            )
            return max(0.0, dispatch_total - accepted)

    def customer_shipped(self, order_id: str) -> float:
        with self._lock:
            return self._customer_totals.get(order_id, 0.0)

    def _require_operation(self, operation_id: str) -> None:
        if operation_id not in self._operations:
            raise KeyError(operation_id)

    def _order_downstream(self, order_id: str) -> float:
        total = 0.0
        for operation_id, qty in self._downstream.items():
            operation = self._operations[operation_id]
            if operation.order_id == order_id or (
                operation.order_id is None and operation_id.startswith(f"{order_id}:")
            ):
                total += qty
        return total

    def _operation_belongs_to_order(self, operation_id: str, order_id: str) -> bool:
        operation = self._operations[operation_id]
        return operation.order_id == order_id or (
            operation.order_id is None and operation_id.startswith(f"{order_id}:")
        )

    def _check_replay(self, event_id: str, payload: object) -> str | None:
        digest = _digest(payload)
        prior = self._event_digests.get(event_id)
        if prior is None:
            return None
        if prior != digest:
            raise ReplayConflictError(event_id)
        return event_id
