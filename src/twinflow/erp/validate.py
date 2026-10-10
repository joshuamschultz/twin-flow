"""Snapshot validator: every cross-record rule of `twinflow.erp/1` (COMP-112, REQ-152).

`validate_snapshot(snapshot, profile)` returns a `ValidationReport` with every issue found
(no first-error stop), sorted by (severity with hard first, path, code). The schema
(COMP-110) already checked each record on its own; this module checks the rules that span
records: duplicate keys, references (in `_checks.py`), options that resolve to no usable
machine, quantities against their order, batch capacities, sales links, due dates,
calendars, purchase lines and priority classes. Mapper `source_issues` are carried in with
their own severity.

Pure and deterministic: no clock read. "Today" is the plant-local date of `as_of`; the
calendar horizon is `as_of + profile.erp.horizon_days`. Each rule family is one small
checker over lookups built once, so the cost is O(records) plus calendar expansion.

Builder-only codes (`material_short`, `po_no_promise`, `in_process_machine_inferred`,
`outside_process_last`, `builder_invariant`) and parse-gate codes (`schema`,
`unsupported_schema_version`) are not raised here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Hashable, Iterable, Iterator
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from twinflow.erp._checks import (
    OPEN_ORDER_STATUSES,
    Index,
    LineKey,
    build_index,
    is_open,
    issue,
    line_key,
    order_operations,
    reference_issues,
)
from twinflow.erp.calendars import expand
from twinflow.erp.digest import snapshot_digest
from twinflow.erp.issues import ErpIssue
from twinflow.erp.profile import PlanningProfile
from twinflow.erp.schema import (
    ErpSnapshot,
    Lineage,
    OrderOperation,
    ProductionOrder,
    PurchaseLine,
    SalesLink,
    SalesOrderLine,
    Severity,
)

__all__ = ["ValidationReport", "validate_snapshot"]

# Relative slack so float noise (0.1 + 0.2 vs 0.3) never reads as an inconsistent quantity.
_QTY_TOLERANCE = 1e-9

_SEVERITY_RANK: dict[Severity, int] = {"hard": 0, "soft": 1}


class ValidationReport(BaseModel):
    """Every issue in a snapshot plus counts; provisional ERP-validation evidence (REQ-177)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_digest: str
    issues: tuple[ErpIssue, ...]
    hard_count: int
    soft_count: int
    validity: Literal["provisional"] = "provisional"
    evidence_kind: Literal["erp_validation"] = "erp_validation"


def validate_snapshot(snapshot: ErpSnapshot, profile: PlanningProfile) -> ValidationReport:
    """Check every cross-record rule; return all issues sorted by (severity, path, code).

    `snapshot_digest` in the report is always the recomputed SHA-256, never the supplied one.
    """
    digest = snapshot_digest(snapshot)
    index = build_index(snapshot)
    tz = ZoneInfo(snapshot.plant_timezone)
    today = snapshot.as_of.astimezone(tz).date()
    found = [
        *_digest_issues(snapshot, digest),
        *_duplicate_key_issues(snapshot),
        *reference_issues(snapshot, index),
        *_order_issues(snapshot, index, today),
        *_operation_issues(snapshot, index),
        *_batch_issues(snapshot),
        *_priority_class_issues(snapshot, profile),
        *_purchase_issues(snapshot, today),
        *_calendar_issues(snapshot, tz, profile.erp.horizon_days),
        *_source_issues(snapshot),
    ]
    issues = tuple(sorted(found, key=_sort_key))
    hard = sum(1 for found_issue in issues if found_issue.severity == "hard")
    return ValidationReport(
        snapshot_digest=digest,
        issues=issues,
        hard_count=hard,
        soft_count=len(issues) - hard,
    )


def _sort_key(found: ErpIssue) -> tuple[int, str, str]:
    return (_SEVERITY_RANK[found.severity], found.path, found.code)


# --- digest and duplicate keys -----------------------------------------------------------------


def _digest_issues(snapshot: ErpSnapshot, digest: str) -> Iterator[ErpIssue]:
    """An absent digest is fine; a supplied one must match the recomputed digest."""
    if snapshot.digest is not None and snapshot.digest != digest:
        yield issue("digest_mismatch", "$.digest", None)


def _keyed_records(
    snapshot: ErpSnapshot,
) -> Iterator[tuple[str, Iterable[tuple[Hashable, Lineage]]]]:
    """Yield each keyed collection's name with its `(key, lineage)` pairs in index order."""
    s = snapshot
    yield "items", ((r.id, r.lineage) for r in s.items)
    yield "boms", ((r.id, r.lineage) for r in s.boms)
    yield "routings", ((r.id, r.lineage) for r in s.routings)
    yield "work_centers", ((r.id, r.lineage) for r in s.work_centers)
    yield "machines", ((r.id, r.lineage) for r in s.machines)
    yield "calendars", ((r.id, r.lineage) for r in s.calendars)
    yield "labor_pools", ((r.id, r.lineage) for r in s.labor_pools)
    yield "production_orders", ((r.id, r.lineage) for r in s.production_orders)
    yield "customers", ((r.id, r.lineage) for r in s.customers)
    yield "vendors", ((r.id, r.lineage) for r in s.vendors)
    sales = s.sales_order_lines
    yield "sales_order_lines", ((_record_line_key(r), r.lineage) for r in sales)
    yield "purchase_lines", ((_record_line_key(r), r.lineage) for r in s.purchase_lines)
    yield "inventory", (((r.item_id, r.site_id), r.lineage) for r in s.inventory)


def _record_line_key(record: SalesOrderLine | SalesLink | PurchaseLine) -> LineKey:
    return line_key(record.order_type, record.order_nbr, record.line_nbr)


def _duplicate_key_issues(snapshot: ErpSnapshot) -> Iterator[ErpIssue]:
    """Report each record whose key an earlier record of the same collection already used."""
    for name, keyed in _keyed_records(snapshot):
        seen: set[Hashable] = set()
        for k, (key, lineage) in enumerate(keyed):
            if key in seen:
                yield issue("duplicate_key", f"$.{name}[{k}]", lineage)
            seen.add(key)


# --- orders: sales links and due dates ---------------------------------------------------------


def _order_issues(snapshot: ErpSnapshot, index: Index, today: dt.date) -> Iterator[ErpIssue]:
    """Open orders link to real sales lines and have a due date that is not unpromised-past."""
    for i, order in enumerate(snapshot.production_orders):
        if order.status in OPEN_ORDER_STATUSES:
            path = f"$.production_orders[{i}]"
            yield from _sales_link_issues(order, path, index)
            yield from _due_issues(order, path, _linked_lines(order, index), today)


def _sales_link_issues(order: ProductionOrder, path: str, index: Index) -> Iterator[ErpIssue]:
    """A sales link must name a sales order line in the snapshot (lineage: the order's)."""
    for k, link in enumerate(order.sales_links):
        if _record_line_key(link) not in index.sales_lines:
            yield issue("sales_link_missing", f"{path}.sales_links[{k}]", order.lineage)


def _linked_lines(order: ProductionOrder, index: Index) -> list[SalesOrderLine]:
    """The sales order lines an order's links resolve to, in link order."""
    found = (index.sales_lines.get(_record_line_key(link)) for link in order.sales_links)
    return [line for line in found if line is not None]


def _due_issues(
    order: ProductionOrder, path: str, lines: list[SalesOrderLine], today: dt.date
) -> Iterator[ErpIssue]:
    """`no_due_date` with no date at all; `past_due_no_promise` when the unpromised due passed.

    The due is the first present of a linked line's promised date, a linked line's requested
    date and the order's due date (REQ-155). Past due is strictly before today's local date.
    """
    if _first_date(line.promised_date for line in lines) is not None:
        return
    due = _first_date(line.requested_date for line in lines) or order.due_date
    if due is None:
        yield issue("no_due_date", path, order.lineage)
    elif due < today:
        yield issue("past_due_no_promise", path, order.lineage)


def _first_date(dates: Iterable[dt.date | None]) -> dt.date | None:
    return next((day for day in dates if day is not None), None)


# --- operations: quantities, options, batches --------------------------------------------------


def _operation_issues(snapshot: ErpSnapshot, index: Index) -> Iterator[ErpIssue]:
    """Quantities of every order operation; options of open in-plant operations only."""
    for path, order, op in order_operations(snapshot):
        yield from _quantity_issues(order, op, path)
        if op.outside is None and is_open(order, op):
            yield from _option_issues(op, path, index)


def _quantity_issues(order: ProductionOrder, op: OrderOperation, path: str) -> Iterator[ErpIssue]:
    """Remaining above the order quantity (hard); complete + scrapped above to-produce (soft)."""
    if op.qty_remaining > order.quantity:
        yield issue("qty_remaining_exceeds_order", f"{path}.qty_remaining", op.lineage)
    slack = _QTY_TOLERANCE * max(1.0, op.qty_to_produce)
    if op.qty_complete + op.qty_scrapped > op.qty_to_produce + slack:
        yield issue("qty_inconsistent", path, op.lineage)


def _option_issues(op: OrderOperation, path: str, index: Index) -> Iterator[ErpIssue]:
    """No machine at all, only inactive machines, or some named machines inactive.

    A center-level option resolves to every machine of its center; only an option naming an
    inactive machine makes `inactive_machine_option`. Unknown machines resolve to nothing
    (they are reported by the reference checks).
    """
    named = [index.machines[o.machine_id] for o in op.options if o.machine_id in index.machines]
    centered = [
        machine
        for o in op.options
        if o.machine_id is None
        for machine in index.machines_by_center.get(o.work_center_id, ())
    ]
    if not named and not centered:
        yield issue("no_option", path, op.lineage)
    elif not any(machine.active for machine in (*named, *centered)):
        yield issue("no_active_option", path, op.lineage)
    elif not all(machine.active for machine in named):
        yield issue("inactive_machine_option", path, op.lineage)


def _batch_issues(snapshot: ErpSnapshot) -> Iterator[ErpIssue]:
    """The first open batch capacity on a work center is the reference; a different one is not."""
    reference: dict[str, float] = {}
    for path, order, op in order_operations(snapshot):
        if op.batch is None or not is_open(order, op):
            continue
        first = reference.setdefault(op.work_center_id, op.batch.capacity)
        if op.batch.capacity != first:
            yield issue("batch_capacity_conflict", f"{path}.batch", op.lineage)


# --- customers, purchases, calendars, source issues --------------------------------------------


def _priority_class_issues(snapshot: ErpSnapshot, profile: PlanningProfile) -> Iterator[ErpIssue]:
    """Each customer class must be in the profile's class map (the builder dedupes per job)."""
    classes = profile.erp.class_weights
    for k, customer in enumerate(snapshot.customers):
        if customer.priority_class is not None and customer.priority_class not in classes:
            path = f"$.customers[{k}].priority_class"
            yield issue("unknown_priority_class", path, customer.lineage)


def _purchase_issues(snapshot: ErpSnapshot, today: dt.date) -> Iterator[ErpIssue]:
    """An open purchase line promised strictly before today's plant-local date."""
    for k, line in enumerate(snapshot.purchase_lines):
        promised = line.promised_date
        if line.status == "open" and promised is not None and promised < today:
            yield issue("po_past_due", f"$.purchase_lines[{k}].promised_date", line.lineage)


def _calendar_issues(snapshot: ErpSnapshot, tz: ZoneInfo, horizon_days: int) -> Iterator[ErpIssue]:
    """A calendar with no working second between `as_of` and the profile horizon."""
    horizon_end = snapshot.as_of + dt.timedelta(days=horizon_days)
    for k, calendar in enumerate(snapshot.calendars):
        if not expand(calendar, tz, snapshot.as_of, horizon_end):
            yield issue("calendar_no_working_time", f"$.calendars[{k}]", calendar.lineage)


def _source_issues(snapshot: ErpSnapshot) -> Iterator[ErpIssue]:
    """Mapper issues, copied with their own code, severity, lineage and message."""
    for k, source in enumerate(snapshot.source_issues):
        yield ErpIssue(
            code=source.code,
            severity=source.severity,
            path=f"$.source_issues[{k}]",
            lineage=source.lineage,
            message=source.message,
        )
