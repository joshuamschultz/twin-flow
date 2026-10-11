"""Shared pieces of the snapshot validator (COMP-112): issue table, lookups, reference checks.

`validate.py` owns the report and the semantic rules; this module owns what every rule
needs (the `RULES` table of severity and message per code, the `Index` lookups built once
per snapshot, the open-ness predicates) and the referential-integrity family: every
`unknown_*` code and `machine_outside_work_center`. Messages name the rule; they never
repeat an input value.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Container, Iterator
from dataclasses import dataclass

from twinflow.erp.issues import ErpIssue
from twinflow.erp.schema import (
    ErpSnapshot,
    Lineage,
    Machine,
    OperationOption,
    OrderOperation,
    ProductionOrder,
    RoutingOperation,
    SalesOrderLine,
    Severity,
)

OPEN_ORDER_STATUSES = frozenset({"planned", "released", "in_process", "on_hold"})
DONE_OPERATION_STATUSES = frozenset({"complete", "cancelled"})

RULES: dict[str, tuple[Severity, str]] = {
    "digest_mismatch": (
        "hard",
        "supplied digest does not match the SHA-256 of the canonical snapshot JSON",
    ),
    "duplicate_key": ("hard", "record repeats the key of an earlier record in this collection"),
    "unknown_work_center": ("hard", "work center id is not in the snapshot"),
    "unknown_machine": ("hard", "machine id is not in the snapshot"),
    "unknown_calendar": ("hard", "calendar id is not in the snapshot"),
    "unknown_labor_pool": ("hard", "labor pool id is not in the snapshot"),
    "unknown_item": ("hard", "item id is not in the snapshot"),
    "machine_outside_work_center": ("hard", "option machine belongs to another work center"),
    "no_option": ("hard", "open in-plant operation has no machine to run on"),
    "no_active_option": ("hard", "every machine of this open operation is inactive"),
    "qty_remaining_exceeds_order": ("hard", "qty_remaining exceeds the order quantity"),
    "batch_capacity_conflict": (
        "hard",
        "batch capacity differs from the first batch capacity on this work center",
    ),
    "unknown_customer": ("soft", "customer id is not in the snapshot"),
    "unknown_vendor": ("soft", "vendor id is not in the snapshot"),
    "unknown_priority_class": ("soft", "priority class is not in the profile class map"),
    "sales_link_missing": ("soft", "linked sales order line is not in the snapshot"),
    "no_due_date": ("soft", "open order has no promised, requested or due date"),
    "past_due_no_promise": ("soft", "open order is past due and has no promised date"),
    "calendar_no_working_time": ("soft", "calendar has no working time in the horizon"),
    "po_past_due": ("soft", "open purchase line was promised before the as_of date"),
    "qty_inconsistent": ("soft", "qty_complete plus qty_scrapped exceeds qty_to_produce"),
    "inactive_machine_option": ("soft", "some options of this open operation are inactive"),
    # Builder-only codes (COMP-116); the validator never raises them.
    "in_process_machine_inferred": (
        "soft",
        "in-process operation names no machine; its most preferred active machine is assumed",
    ),
    "in_progress_machine_unknown": (
        "soft",
        "in-process operation has no free machine to be running on; it is planned from now",
    ),
    "release_after_horizon": (
        "soft",
        "order releases, or its material arrives, at or after the horizon end; the job is held",
    ),
    "outside_process_last": (
        "soft",
        "outside operation ends the route; its lead time is taken off the due date",
    ),
    "batch_lot_exceeds_capacity": (
        "soft",
        "batch lot is larger than the batch capacity; it runs alone instead of batched",
    ),
    "no_routing": ("hard", "no standard routing of this item matches the request"),
    "builder_invariant": ("hard", "the built request failed the TF-002 self-check"),
}

LineKey = tuple[str, str, str]


@dataclass(frozen=True)
class Index:
    """Lookups built once per snapshot; the first record wins when a key repeats."""

    work_centers: frozenset[str]
    machines: dict[str, Machine]
    machines_by_center: dict[str, tuple[Machine, ...]]
    calendars: frozenset[str]
    labor_pools: frozenset[str]
    items: frozenset[str]
    customers: frozenset[str]
    vendors: frozenset[str]
    sales_lines: dict[LineKey, SalesOrderLine]


def issue(code: str, path: str, lineage: Lineage | None) -> ErpIssue:
    """An issue whose severity and message come from the `RULES` table."""
    severity, message = RULES[code]
    return ErpIssue(code=code, severity=severity, path=path, lineage=lineage, message=message)


def line_key(order_type: str, order_nbr: str, line_nbr: object) -> LineKey:
    """Sales lines number lines as int, links as text: compare both through `str`."""
    return (order_type, order_nbr, str(line_nbr))


def is_open(order: ProductionOrder, op: OrderOperation) -> bool:
    """Open order status and an operation not complete or cancelled."""
    return order.status in OPEN_ORDER_STATUSES and op.status not in DONE_OPERATION_STATUSES


def order_operations(
    snapshot: ErpSnapshot,
) -> Iterator[tuple[str, ProductionOrder, OrderOperation]]:
    """Yield `(json path, order, operation)` for every order operation in document order."""
    for i, order in enumerate(snapshot.production_orders):
        for j, op in enumerate(order.operations):
            yield f"$.production_orders[{i}].operations[{j}]", order, op


def build_index(snapshot: ErpSnapshot) -> Index:
    """Build every id lookup in one pass per collection."""
    machines: dict[str, Machine] = {}
    by_center: defaultdict[str, list[Machine]] = defaultdict(list)
    for machine in snapshot.machines:
        machines.setdefault(machine.id, machine)
        by_center[machine.work_center_id].append(machine)
    sales_lines: dict[LineKey, SalesOrderLine] = {}
    for line in snapshot.sales_order_lines:
        sales_lines.setdefault(line_key(line.order_type, line.order_nbr, line.line_nbr), line)
    return Index(
        work_centers=frozenset(wc.id for wc in snapshot.work_centers),
        machines=machines,
        machines_by_center={center: tuple(ms) for center, ms in by_center.items()},
        calendars=frozenset(cal.id for cal in snapshot.calendars),
        labor_pools=frozenset(pool.id for pool in snapshot.labor_pools),
        items=frozenset(item.id for item in snapshot.items),
        customers=frozenset(customer.id for customer in snapshot.customers),
        vendors=frozenset(vendor.id for vendor in snapshot.vendors),
        sales_lines=sales_lines,
    )


# --- reference checks --------------------------------------------------------------------------


def reference_issues(snapshot: ErpSnapshot, index: Index) -> Iterator[ErpIssue]:
    """Every id a record holds names a record of the snapshot (hard, or soft for partners)."""
    yield from _resource_refs(snapshot, index)
    yield from _routing_refs(snapshot, index)
    yield from _order_refs(snapshot, index)
    yield from _trade_refs(snapshot, index)


def _ref(
    code: str, known: Container[str], value: str | None, path: str, lineage: Lineage
) -> Iterator[ErpIssue]:
    """Yield `code` when a present reference `value` is not among the `known` ids."""
    if value is not None and value not in known:
        yield issue(code, path, lineage)


def _resource_refs(snapshot: ErpSnapshot, index: Index) -> Iterator[ErpIssue]:
    """Work centers, machines and labor pools point at known centers and calendars."""
    cals = index.calendars
    for k, wc in enumerate(snapshot.work_centers):
        path = f"$.work_centers[{k}]"
        yield from _ref("unknown_calendar", cals, wc.calendar_id, f"{path}.calendar_id", wc.lineage)
    for k, m in enumerate(snapshot.machines):
        path = f"$.machines[{k}]"
        center, lineage = m.work_center_id, m.lineage
        yield from _ref(
            "unknown_work_center", index.work_centers, center, f"{path}.work_center_id", lineage
        )
        yield from _ref("unknown_calendar", cals, m.calendar_id, f"{path}.calendar_id", lineage)
    for k, pool in enumerate(snapshot.labor_pools):
        path = f"$.labor_pools[{k}]"
        yield from _ref(
            "unknown_calendar", cals, pool.calendar_id, f"{path}.calendar_id", pool.lineage
        )


def _routing_refs(snapshot: ErpSnapshot, index: Index) -> Iterator[ErpIssue]:
    """Routings and BOMs point at known items; routing steps at known resources."""
    items = index.items
    for k, routing in enumerate(snapshot.routings):
        path = f"$.routings[{k}]"
        yield from _ref("unknown_item", items, routing.item_id, f"{path}.item_id", routing.lineage)
        for j, op in enumerate(routing.operations):
            yield from _operation_refs(op, f"{path}.operations[{j}]", index)
    for k, bom in enumerate(snapshot.boms):
        path = f"$.boms[{k}]"
        yield from _ref("unknown_item", items, bom.item_id, f"{path}.item_id", bom.lineage)
        for n, line in enumerate(bom.lines):
            line_path = f"{path}.lines[{n}].item_id"
            yield from _ref("unknown_item", items, line.item_id, line_path, bom.lineage)


def _order_refs(snapshot: ErpSnapshot, index: Index) -> Iterator[ErpIssue]:
    """Orders, their materials and their operations point at known records."""
    items = index.items
    for i, order in enumerate(snapshot.production_orders):
        path = f"$.production_orders[{i}]"
        yield from _ref("unknown_item", items, order.item_id, f"{path}.item_id", order.lineage)
        for k, need in enumerate(order.materials):
            need_path = f"{path}.materials[{k}].item_id"
            yield from _ref("unknown_item", items, need.item_id, need_path, need.lineage)
    for path, _order, op in order_operations(snapshot):
        yield from _operation_refs(op, path, index)
        machine_path = f"{path}.current_machine_id"
        machine_id = op.current_machine_id
        yield from _ref("unknown_machine", index.machines, machine_id, machine_path, op.lineage)


def _operation_refs(op: RoutingOperation, path: str, index: Index) -> Iterator[ErpIssue]:
    """An operation's own references; its options carry their own lineage."""
    centers, lineage = index.work_centers, op.lineage
    wc_path = f"{path}.work_center_id"
    yield from _ref("unknown_work_center", centers, op.work_center_id, wc_path, lineage)
    for k, option in enumerate(op.options):
        yield from _option_refs(option, f"{path}.options[{k}]", index)
    if op.crew is not None:
        pool_path = f"{path}.crew.labor_pool_id"
        pool_id = op.crew.labor_pool_id
        yield from _ref("unknown_labor_pool", index.labor_pools, pool_id, pool_path, lineage)
    if op.outside is not None:
        vendor_path = f"{path}.outside.vendor_id"
        vendor_id = op.outside.vendor_id
        yield from _ref("unknown_vendor", index.vendors, vendor_id, vendor_path, lineage)


def _option_refs(option: OperationOption, path: str, index: Index) -> Iterator[ErpIssue]:
    """An option's center is known; its machine is known and inside that center."""
    center = option.work_center_id
    wc_path = f"{path}.work_center_id"
    yield from _ref("unknown_work_center", index.work_centers, center, wc_path, option.lineage)
    if option.machine_id is None:
        return
    machine = index.machines.get(option.machine_id)
    if machine is None:
        yield issue("unknown_machine", f"{path}.machine_id", option.lineage)
    elif machine.work_center_id != center:
        yield issue("machine_outside_work_center", f"{path}.machine_id", option.lineage)


def _trade_refs(snapshot: ErpSnapshot, index: Index) -> Iterator[ErpIssue]:
    """Sales lines, purchase lines and stock rows name known items, customers and vendors."""
    items = index.items
    for k, sale in enumerate(snapshot.sales_order_lines):
        path, lineage = f"$.sales_order_lines[{k}]", sale.lineage
        customer = sale.customer_id
        yield from _ref(
            "unknown_customer", index.customers, customer, f"{path}.customer_id", lineage
        )
        yield from _ref("unknown_item", items, sale.item_id, f"{path}.item_id", lineage)
    for k, buy in enumerate(snapshot.purchase_lines):
        path, lineage = f"$.purchase_lines[{k}]", buy.lineage
        yield from _ref(
            "unknown_vendor", index.vendors, buy.vendor_id, f"{path}.vendor_id", lineage
        )
        yield from _ref("unknown_item", items, buy.item_id, f"{path}.item_id", lineage)
    for k, stock in enumerate(snapshot.inventory):
        path = f"$.inventory[{k}].item_id"
        yield from _ref("unknown_item", items, stock.item_id, path, stock.lineage)
