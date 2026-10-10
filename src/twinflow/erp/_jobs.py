"""Job rules of the problem builder (COMP-116 steps 2, 3, 5, 6, 8 and 10).

Two passes, because material readiness (COMP-115) needs the job priority order first:

1. `job_heads` reads every open order into a `JobHead`: weight class (rush, else the first
   sales link's customer class, else the stock class), due (D-129: first link's promised,
   its requested, then the order's due date, at `due_at`, minus trailing outside lead),
   `promised_ship` (start of the promised date), the release floor (start of the release
   date and the leading outside lead, never below 0), the hold flag and the route walk.
2. `build_job` turns a head into a TF-002 `Job` with the material plan: op ready time =
   max(own queue, material ready), release = max(floor, material ready for the order).

Times are real elapsed seconds from `as_of` (DST-safe, via `start_of_day_seconds`).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterator
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from twinflow.erp._checks import (
    DONE_OPERATION_STATUSES,
    OPEN_ORDER_STATUSES,
    issue,
    line_key,
)
from twinflow.erp._operations import (
    RouteStep,
    RouteWalk,
    ShopIndex,
    infer_machine,
    lot_fits_batch,
    make_operation,
    processing_options,
    walk_route,
)
from twinflow.erp.issues import ErpIssue
from twinflow.erp.materials import MaterialPlan, start_of_day_seconds
from twinflow.erp.profile import ErpPolicy
from twinflow.erp.schema import ErpSnapshot, OrderOperation, ProductionOrder, SalesOrderLine
from twinflow.scheduling.model import (
    Job,
    Operation,
    OperationStatus,
    ProcessingOption,
    WeightClass,
)

__all__ = ["JobHead", "build_job", "job_heads", "jobs_order"]

_ONE_DAY = dt.timedelta(days=1)


@dataclass(frozen=True)
class JobHead:
    """An open order read for planning, before material readiness is known."""

    order: ProductionOrder
    path: str
    walk: RouteWalk
    weight_class: WeightClass
    due: float | None
    promised_ship: float | None
    release_floor: float
    held: bool
    issues: tuple[ErpIssue, ...]


@dataclass(frozen=True)
class _Dates:
    """What turns plant-local dates into seconds, plus the lookups a head needs."""

    tz: ZoneInfo
    as_of: dt.datetime
    policy: ErpPolicy
    sales_lines: dict[tuple[str, str, str], SalesOrderLine]
    customer_classes: dict[str, str | None]

    def start_of(self, day: dt.date) -> float:
        return start_of_day_seconds(day, self.tz, self.as_of)

    def due_of(self, day: dt.date) -> float:
        at_end = self.policy.due_at == "end_of_day"
        return self.start_of(day + _ONE_DAY if at_end else day)


def job_heads(snapshot: ErpSnapshot, policy: ErpPolicy) -> list[JobHead]:
    """One head per open order (open status, some op not done), sorted by order id."""
    dates = _dates(snapshot, policy)
    heads = [
        _job_head(order, f"$.production_orders[{index}]", dates)
        for index, order in enumerate(snapshot.production_orders)
        if _is_open(order)
    ]
    return sorted(heads, key=lambda head: head.order.id)


def jobs_order(heads: list[JobHead]) -> list[str]:
    """Material priority: due (None last), weight class descending, order id (D-122)."""

    def key(head: JobHead) -> tuple[float, int, str]:
        due = math.inf if head.due is None else head.due
        return (due, -head.weight_class, head.order.id)

    return [head.order.id for head in sorted(heads, key=key)]


def build_job(
    head: JobHead, plan: MaterialPlan, shop: ShopIndex, policy: ErpPolicy
) -> tuple[Job, tuple[ErpIssue, ...]]:
    """The TF-002 job of one head plus its per-operation builder issues."""
    order = head.order
    operations: list[Operation] = []
    issues: list[ErpIssue] = []
    op_paths = {op.op_id: f"{head.path}.operations[{j}]" for j, op in enumerate(order.operations)}
    for step in head.walk.steps:
        operation, inferred = _order_operation(order, step, plan, shop, policy)
        operations.append(operation)
        path = op_paths[step.op.op_id]
        if inferred:
            issues.append(issue("in_process_machine_inferred", path, step.op.lineage))
        if step.op.batch is not None and not lot_fits_batch(step.op, operation.quantity):
            issues.append(issue("batch_lot_exceeds_capacity", f"{path}.batch", step.op.lineage))
    held = head.held or order.id in plan.held_orders
    job = Job(
        id=order.id,
        weight_class=head.weight_class,
        release=max(head.release_floor, plan.ready.get((order.id, None), 0.0)),
        due=head.due,
        promised_ship=head.promised_ship,
        status="held" if held else "open",
        operations=tuple(operations),
    )
    return job, tuple(issues)


def _dates(snapshot: ErpSnapshot, policy: ErpPolicy) -> _Dates:
    sales_lines: dict[tuple[str, str, str], SalesOrderLine] = {}
    for line in snapshot.sales_order_lines:
        sales_lines.setdefault(line_key(line.order_type, line.order_nbr, line.line_nbr), line)
    customers: dict[str, str | None] = {}
    for customer in snapshot.customers:
        customers.setdefault(customer.id, customer.priority_class)
    return _Dates(
        tz=ZoneInfo(snapshot.plant_timezone),
        as_of=snapshot.as_of,
        policy=policy,
        sales_lines=sales_lines,
        customer_classes=customers,
    )


def _is_open(order: ProductionOrder) -> bool:
    return order.status in OPEN_ORDER_STATUSES and any(
        op.status not in DONE_OPERATION_STATUSES for op in order.operations
    )


def _job_head(order: ProductionOrder, path: str, dates: _Dates) -> JobHead:
    walk = walk_route(op for op in order.operations if op.status not in DONE_OPERATION_STATUSES)
    line = _first_line(order, dates)
    due_day = _due_day(order, line)
    due = None if due_day is None else dates.due_of(due_day) - walk.due_lead
    promised = None if line is None or line.promised_date is None else line.promised_date
    release_day = order.release_date
    release_date_seconds = 0.0 if release_day is None else dates.start_of(release_day)
    return JobHead(
        order=order,
        path=path,
        walk=walk,
        weight_class=_weight_class(order, line, dates),
        due=due,
        promised_ship=None if promised is None else dates.start_of(promised),
        release_floor=max(0.0, release_date_seconds, walk.release_lead),
        held=order.status == "on_hold" or order.hold,
        issues=tuple(_head_issues(order, path, walk, due_day)),
    )


def _first_line(order: ProductionOrder, dates: _Dates) -> SalesOrderLine | None:
    """The sales line of the order's FIRST link (None: no link or the line is missing)."""
    if not order.sales_links:
        return None
    link = order.sales_links[0]
    return dates.sales_lines.get(line_key(link.order_type, link.order_nbr, link.line_nbr))


def _due_day(order: ProductionOrder, line: SalesOrderLine | None) -> dt.date | None:
    if line is not None and line.promised_date is not None:
        return line.promised_date
    if line is not None and line.requested_date is not None:
        return line.requested_date
    return order.due_date


def _weight_class(
    order: ProductionOrder, line: SalesOrderLine | None, dates: _Dates
) -> WeightClass:
    """Rush wins; a stock order (no resolvable line) takes the stock class (REQ-154)."""
    policy = dates.policy
    if order.rush:
        return policy.rush_class
    if line is None:
        return policy.stock_class
    priority = dates.customer_classes.get(line.customer_id)
    if priority is None:
        return policy.default_class
    return policy.class_weights.get(priority, policy.default_class)


def _head_issues(
    order: ProductionOrder, path: str, walk: RouteWalk, due_day: dt.date | None
) -> Iterator[ErpIssue]:
    if due_day is None:
        yield issue("no_due_date", path, order.lineage)
    paths = {op.op_id: f"{path}.operations[{j}]" for j, op in enumerate(order.operations)}
    for outside in walk.trailing_outside:
        yield issue("outside_process_last", paths[outside.op_id], outside.lineage)


def _order_operation(
    order: ProductionOrder,
    step: RouteStep,
    plan: MaterialPlan,
    shop: ShopIndex,
    policy: ErpPolicy,
) -> tuple[Operation, bool]:
    """One built op and whether its running machine had to be inferred."""
    op = next(found for found in order.operations if found.op_id == step.op.op_id)
    options = processing_options(op, shop)
    status, inferred = _status(op, options, shop)
    material = plan.ready.get((order.id, op.op_id), 0.0)
    operation = make_operation(
        order.id,
        step,
        options,
        quantity=op.qty_remaining,
        batch_default=order.item_id,
        ready_time=max(step.queue, material),
        status=status,
        policy=policy,
    )
    return operation, inferred


def _status(
    op: OrderOperation, options: tuple[ProcessingOption, ...], shop: ShopIndex
) -> tuple[OperationStatus, bool]:
    """Pending (default) or in progress on its machine with the remaining run time.

    A missing (or unusable) current machine is inferred (REQ-157) and flagged.
    """
    if op.status != "in_process":
        return OperationStatus(), False
    units = {o.machine_id: o.unit_seconds for o in options}
    machine_id = op.current_machine_id
    inferred = machine_id not in units
    if inferred:
        machine_id = infer_machine(op, shop)
    if machine_id is None or machine_id not in units:
        return OperationStatus(), inferred
    remaining = op.qty_remaining * units[machine_id]
    return OperationStatus(
        state="in_progress", machine_id=machine_id, remaining_seconds=remaining
    ), inferred
