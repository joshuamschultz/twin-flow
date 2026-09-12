"""Attributed WIP lots and order quantity accounting."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import RLock
from types import MappingProxyType
from typing import Literal

from .state_validation import require_same_uom, validate_positive, validate_quantity, validate_uom

LotState = Literal["queued", "wip", "active", "accepted", "scrap", "sampling"]


class DuplicateLotError(ValueError):
    """A lot identifier is already present in a ledger."""


class LotNotFoundError(KeyError):
    """A requested lot does not exist."""


@dataclass(frozen=True, slots=True)
class FlowLot:
    """Immutable, identified production material at one operation."""

    id: str
    order_id: str
    operation_id: str
    qty: float
    uom: str
    state: LotState
    remaining_time: float = 0.0
    machine_id: str | None = None
    location: str | None = None
    setup_state: str | None = None
    as_of: str | None = None
    source: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.order_id or not self.operation_id:
            raise ValueError("lot id, order id, and operation id are required")
        object.__setattr__(self, "qty", validate_quantity(self.qty, field="lot qty"))
        object.__setattr__(self, "uom", validate_uom(self.uom))
        object.__setattr__(
            self, "remaining_time", validate_quantity(self.remaining_time, field="remaining_time")
        )
        if self.state not in {"queued", "wip", "active", "accepted", "scrap", "sampling"}:
            raise ValueError(f"unsupported lot state {self.state!r}")
        object.__setattr__(self, "source", MappingProxyType(dict(self.source or {})))


@dataclass(frozen=True, slots=True)
class OrderDemand:
    order_id: str
    target_qty: float
    uom: str
    completed_good: float = 0.0
    unreleased: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_qty", validate_quantity(self.target_qty, field="target_qty")
        )
        object.__setattr__(self, "uom", validate_uom(self.uom))
        object.__setattr__(
            self, "completed_good", validate_quantity(self.completed_good, field="completed_good")
        )
        object.__setattr__(
            self, "unreleased", validate_quantity(self.unreleased, field="unreleased")
        )
        if self.completed_good + self.unreleased > self.target_qty:
            raise ValueError("completed_good plus unreleased exceeds target_qty")


@dataclass(frozen=True, slots=True)
class LotBalance:
    order_id: str
    target_qty: float
    uom: str
    accepted_qty: float
    wip_qty: float
    scrap_qty: float
    unreleased_qty: float
    raw_release_qty: float


@dataclass(frozen=True, slots=True)
class ReconciliationIssue:
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    order_id: str
    unresolved: bool
    issues: tuple[ReconciliationIssue, ...] = ()


class LotLedger:
    """Thread-safe in-memory lot ledger with explicit state transitions."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._lots: dict[str, FlowLot] = {}
        self._demands: dict[str, OrderDemand] = {}
        self._accepted_events: dict[str, float] = {}
        self._scrap_events: dict[str, float] = {}

    @property
    def lots(self) -> Mapping[str, FlowLot]:
        with self._lock:
            return MappingProxyType(dict(self._lots))

    def demand(
        self,
        order_id: str,
        target_qty: float,
        uom: str,
        completed_good: float = 0.0,
        unreleased: float = 0.0,
    ) -> OrderDemand:
        """Record an order demand exactly once, accepting an identical replay."""

        candidate = OrderDemand(order_id, target_qty, uom, completed_good, unreleased)
        with self._lock:
            prior = self._demands.get(order_id)
            if prior is not None and prior != candidate:
                raise ValueError(f"conflicting demand for {order_id}")
            self._demands[order_id] = candidate
        return candidate

    def seed(self, lot: FlowLot) -> FlowLot:
        """Seed one existing lot without creating a duplicate release."""

        with self._lock:
            if lot.id in self._lots:
                raise DuplicateLotError(lot.id)
            demand = self._demands.get(lot.order_id)
            if demand is not None:
                require_same_uom(demand.uom, lot.uom)
            self._lots[lot.id] = lot
        return lot

    def get(self, lot_id: str) -> FlowLot:
        with self._lock:
            try:
                return self._lots[lot_id]
            except KeyError as error:
                raise LotNotFoundError(lot_id) from error

    def lots_for_order(self, order_id: str) -> tuple[FlowLot, ...]:
        with self._lock:
            return tuple(lot for lot in self._lots.values() if lot.order_id == order_id)

    def split(self, lot_id: str, qty: float, new_lot_id: str) -> tuple[FlowLot, FlowLot]:
        """Split a lot into retained and newly identified child lots."""

        split_qty = validate_positive(qty, field="split qty")
        if not new_lot_id:
            raise ValueError("new_lot_id is required")
        with self._lock:
            source = self.get(lot_id)
            if new_lot_id in self._lots:
                raise DuplicateLotError(new_lot_id)
            if split_qty >= source.qty:
                raise ValueError("split qty must be less than source lot qty")
            retained = replace(source, qty=source.qty - split_qty)
            child = replace(source, id=new_lot_id, qty=split_qty)
            self._lots[lot_id] = retained
            self._lots[new_lot_id] = child
            return retained, child

    def transfer(
        self,
        lot_id: str,
        operation_id: str,
        state: LotState,
        location: str | None = None,
    ) -> FlowLot:
        """Move a lot to a routed operation while retaining its identity."""

        if not operation_id:
            raise ValueError("operation_id is required")
        with self._lock:
            source = self.get(lot_id)
            if source.state in {"accepted", "scrap"} and state != source.state:
                raise ValueError(f"terminal lot {lot_id} cannot transition from {source.state}")
            updated = replace(source, operation_id=operation_id, state=state, location=location)
            self._lots[lot_id] = updated
            return updated

    def accept(self, lot_id: str, qty: float, uom: str | None = None) -> FlowLot:
        """Attribute accepted good quantity to its original lot."""

        with self._lock:
            source = self.get(lot_id)
            if source.state in {"accepted", "scrap"}:
                raise ValueError(f"terminal lot {lot_id} cannot be accepted")
            if uom is not None:
                require_same_uom(source.uom, uom)
            amount = validate_positive(qty, field="accepted qty")
            if amount > source.qty:
                raise ValueError("accepted qty exceeds lot qty")
            updated = replace(
                source,
                qty=source.qty - amount,
                state="accepted" if amount == source.qty else source.state,
            )
            self._lots[lot_id] = updated
            self._accepted_events[source.order_id] = (
                self._accepted_events.get(source.order_id, 0.0) + amount
            )
            return updated

    def scrap(self, lot_id: str, qty: float, reason: str) -> FlowLot:
        """Move an explicit quantity to scrap while retaining lot attribution."""

        if not reason.strip():
            raise ValueError("scrap reason is required")
        with self._lock:
            source = self.get(lot_id)
            if source.state in {"accepted", "scrap"}:
                raise ValueError(f"terminal lot {lot_id} cannot be scrapped")
            amount = validate_positive(qty, field="scrap qty")
            if amount > source.qty:
                raise ValueError("scrap qty exceeds lot qty")
            updated = replace(
                source,
                qty=source.qty - amount,
                state="scrap" if amount == source.qty else source.state,
            )
            self._lots[lot_id] = updated
            self._scrap_events[source.order_id] = (
                self._scrap_events.get(source.order_id, 0.0) + amount
            )
            return updated

    def resume_time(self, lot_id: str) -> float:
        """Return active remaining processing time without restarting completed work."""

        lot = self.get(lot_id)
        return lot.remaining_time

    def balance(self, order_id: str) -> LotBalance:
        """Return attributed accepted, WIP, scrap, and unreleased quantities."""

        with self._lock:
            demand = self._demands.get(order_id)
            lots = self.lots_for_order(order_id)
            if demand is None:
                if not lots:
                    raise KeyError(order_id)
                demand = OrderDemand(order_id, sum(lot.qty for lot in lots), lots[0].uom)
            for lot in lots:
                require_same_uom(demand.uom, lot.uom)
            accepted = (
                demand.completed_good
                + self._accepted_events.get(order_id, 0.0)
                + sum(lot.qty for lot in lots if lot.state == "accepted")
            )
            wip = sum(
                lot.qty for lot in lots if lot.state in {"queued", "wip", "active", "sampling"}
            )
            scrap = self._scrap_events.get(order_id, 0.0) + sum(
                lot.qty for lot in lots if lot.state == "scrap"
            )
            raw_release = max(0.0, demand.target_qty - accepted - wip - demand.unreleased)
            return LotBalance(
                order_id,
                demand.target_qty,
                demand.uom,
                accepted,
                wip,
                scrap,
                demand.unreleased,
                raw_release,
            )

    def reconcile(
        self,
        order_id: str,
        target_qty: float,
        completed_good: float,
        operation_remaining: Mapping[str, float],
        source_remaining: Mapping[str, float],
    ) -> ReconciliationReport:
        """Compare supplied snapshot quantities and report contradictions without rewriting them."""

        validate_quantity(target_qty, field="target_qty")
        validate_quantity(completed_good, field="completed_good")
        issues: list[ReconciliationIssue] = []
        operation_total = 0.0
        for operation_id in sorted(set(operation_remaining) | set(source_remaining)):
            actual = operation_remaining.get(operation_id)
            supplied = source_remaining.get(operation_id)
            if actual is None or supplied is None:
                issues.append(
                    ReconciliationIssue(
                        operation_id, "operation is present in only one snapshot source"
                    )
                )
                continue
            validate_quantity(float(actual), field=f"operation {operation_id} remaining")
            validate_quantity(float(supplied), field=f"source {operation_id} remaining")
            operation_total += float(actual)
            if float(actual) != float(supplied):
                issues.append(
                    ReconciliationIssue(
                        operation_id, f"conflicting remaining values {actual} and {supplied}"
                    )
                )
        if completed_good + operation_total > target_qty:
            issues.append(
                ReconciliationIssue(
                    order_id,
                    f"completed good {completed_good} plus remaining {operation_total} "
                    f"exceeds target {target_qty}",
                )
            )
        return ReconciliationReport(order_id, bool(issues), tuple(issues))
