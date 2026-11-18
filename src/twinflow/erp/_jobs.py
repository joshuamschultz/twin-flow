"""Job rules of the problem builder (COMP-116 steps 2, 3, 5, 6, 8 and 10).

Two passes, because material readiness (COMP-115) needs the job priority order first:

1. `job_heads` reads every open order into a `JobHead`: weight class (rush, else the first
   sales link's customer class, else the stock class), due (D-129 as revised by ruling C:
   `_dates.order_due_day`, the earliest linked-line due, at `due_at`, minus trailing outside
   lead), `promised_ship` (start of `_dates.order_promised_day`), the release floor (start
   of the release date and the leading outside lead, never below 0), the hold flag and the
   route walk.
2. `build_job` turns a head into a TF-002 `Job` with the material plan: op ready time =
   max(own queue, material ready), release = max(floor, material ready for the order). A
   release (or an op's material ready time) at or after the calendar end
   (`calendars.expansion_seconds`: the expansion cap, or the horizon with `hard_horizon`)
   holds the job (soft `release_after_horizon`), since no calendar has working time there
   (ruling A). A release past the planning window but inside the cap is planned normally.

`running_machines` gives every in-process op its machine across ALL jobs (TF-005 ruling):
explicit current machines claim first, in (order id, op sequence) order; an op whose machine
is unusable or already claimed falls through to inference, which in the same order gives
each op its most preferred active option machine not yet held (soft
`in_process_machine_inferred`). With none free the op is pending from now (soft
`in_progress_machine_unknown`). No machine ever holds two in-progress ops. An op with a
machine whose crew pool is off shift at `as_of` is paused there (ruling B, `_paused.py`;
soft `in_progress_paused_offshift`).

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
from twinflow.erp._dates import order_due_day, order_promised_day
from twinflow.erp._operations import (
    RouteStep,
    RouteWalk,
    ShopIndex,
    lot_fits_batch,
    make_operation,
    preferred_machines,
    processing_options,
    walk_route,
)
from twinflow.erp._paused import OpKey, paused_option
from twinflow.erp.calendars import expansion_seconds
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

__all__ = ["JobHead", "Running", "build_job", "job_heads", "jobs_order", "running_machines"]

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
class Running:
    """Each in-process op's running machine (None: none free) and which of them are paused."""

    machines: dict[OpKey, str | None]
    paused: frozenset[OpKey]


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


def running_machines(
    heads: list[JobHead], shop: ShopIndex, off_shift_pools: frozenset[str]
) -> Running:
    """The running machine of every in-process in-plant op (see the module docstring)."""
    running = sorted(
        (
            (head.order.id, op)
            for head in heads
            for op in head.order.operations
            if op.status == "in_process" and op.outside is None
        ),
        key=lambda pair: (pair[0], pair[1].sequence, pair[1].op_id),
    )
    ranked = {(order_id, op.op_id): preferred_machines(op, shop) for order_id, op in running}
    result: dict[OpKey, str | None] = {}
    for order_id, op in running:
        machine = op.current_machine_id
        if machine in ranked[(order_id, op.op_id)] and machine not in result.values():
            result[(order_id, op.op_id)] = machine
    for order_id, op in running:
        key = (order_id, op.op_id)
        if key not in result:
            held = set(result.values())
            result[key] = next((m for m in ranked[key] if m not in held), None)
    paused = frozenset(
        (order_id, op.op_id)
        for order_id, op in running
        if result[(order_id, op.op_id)] is not None
        and op.crew is not None
        and op.crew.labor_pool_id in off_shift_pools
    )
    return Running(machines=result, paused=paused)


def build_job(
    head: JobHead,
    plan: MaterialPlan,
    shop: ShopIndex,
    policy: ErpPolicy,
    running: Running,
) -> tuple[Job, tuple[ErpIssue, ...]]:
    """The TF-002 job of one head plus its per-operation builder issues."""
    order = head.order
    operations: list[Operation] = []
    issues: list[ErpIssue] = []
    by_id = {op.op_id: (j, op) for j, op in enumerate(order.operations)}
    for step in head.walk.steps:
        j, op = by_id[step.op.op_id]
        operation = _order_operation(order, op, step, plan, shop, policy, running)
        operations.append(operation)
        path = f"{head.path}.operations[{j}]"
        key = (order.id, op.op_id)
        issues.extend(_operation_issues(op, operation, path, running.machines.get(key)))
        if key in running.paused:
            issues.append(issue("in_progress_paused_offshift", path, op.lineage))
    release = max(head.release_floor, plan.ready.get((order.id, None), 0.0))
    starts = (release, *(operation.ready_time for operation in operations))
    late = max(starts) >= expansion_seconds(policy)
    if late:
        issues.append(issue("release_after_horizon", head.path, order.lineage))
    job = Job(
        id=order.id,
        weight_class=head.weight_class,
        release=release,
        due=head.due,
        promised_ship=head.promised_ship,
        status="held" if late or head.held or order.id in plan.held_orders else "open",
        operations=tuple(operations),
    )
    return job, tuple(issues)


def _operation_issues(
    op: OrderOperation, operation: Operation, path: str, machine: str | None
) -> Iterator[ErpIssue]:
    """Builder issues of one built op: its running machine (if any) and its batch lot."""
    if op.status == "in_process" and machine is None:
        yield issue("in_progress_machine_unknown", path, op.lineage)
    elif op.status == "in_process" and machine != op.current_machine_id:
        yield issue("in_process_machine_inferred", path, op.lineage)
    if op.batch is not None and not lot_fits_batch(op, operation.quantity):
        yield issue("batch_lot_exceeds_capacity", f"{path}.batch", op.lineage)


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
    due_day = order_due_day(order, dates.sales_lines)
    due = None if due_day is None else dates.due_of(due_day) - walk.due_lead
    promised = order_promised_day(order, dates.sales_lines)
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
    op: OrderOperation,
    step: RouteStep,
    plan: MaterialPlan,
    shop: ShopIndex,
    policy: ErpPolicy,
    running: Running,
) -> Operation:
    """One built op of an order (`op` is the order operation of `step`).

    A paused op (ruling B) is pending on its one running-machine option from now, with its
    crew needs, and never batched.
    """
    key = (order.id, op.op_id)
    options = processing_options(op, shop)
    status = _status(op, options, running.machines.get(key))
    ready_time = max(step.queue, plan.ready.get(key, 0.0))
    paused = key in running.paused
    if paused:
        options, status, ready_time = paused_option(options, status), OperationStatus(), 0.0
    operation = make_operation(
        order.id,
        step,
        options,
        quantity=op.qty_remaining,
        batch_default=order.item_id,
        ready_time=ready_time,
        status=status,
        policy=policy,
    )
    return operation.model_copy(update={"batch_family": None}) if paused else operation


def _status(
    op: OrderOperation, options: tuple[ProcessingOption, ...], machine_id: str | None
) -> OperationStatus:
    """Pending (default), or in progress on `machine_id` with the remaining run time."""
    if machine_id is None:
        return OperationStatus()
    units = {o.machine_id: o.unit_seconds for o in options}
    remaining = op.qty_remaining * units[machine_id]
    return OperationStatus(state="in_progress", machine_id=machine_id, remaining_seconds=remaining)
