"""Problem builder: ERP snapshot + planning profile -> TF-002 `SolveRequest` (T-202, COMP-116).

Requirements: REQ-153, REQ-154, REQ-155, REQ-156, REQ-157, REQ-159. Decisions: D-111, D-114,
D-122, D-123, D-124, D-129.

Contract pinned here (the implementation follows these tests):

    from twinflow.erp.build import BUILDER_VERSION, BuildResult, build_problem_from_erp

    build_problem_from_erp(snapshot: ErpSnapshot, profile: PlanningProfile) -> BuildResult

* `BuildResult` attributes: `status` ("ready" | "blocked"), `issues` (tuple of `ErpIssue`),
  `request` (`SolveRequest | None`), `problem_digest` (`str | None`), `snapshot_digest`,
  `profile_digest`, `builder_version`, `counts` (a plain `dict[str, int]` with exactly the keys
  `jobs`, `operations`, `machines`, `held_jobs`, `done_operations`), `lineage_index`
  (`dict[str, Lineage]`, one entry per job id and per operation id).
* `BUILDER_VERSION == "erp-build-1"`.
* Problem id (PINNED CONCATENATION): `"erp-" + sha256("\\n".join([snapshot_digest,
  profile_digest, BUILDER_VERSION]).encode("utf-8")).hexdigest()[:16]`, where the snapshot
  digest is `snapshot_digest(snapshot)` (recomputed) and the profile digest is
  `profile_digest(profile)`.
* Problem digest (PINNED): SHA-256 hex of `json.dumps(request.model_dump(mode="json"),
  sort_keys=True, separators=(",", ":"), allow_nan=False)` of the returned request.
* Times are real elapsed seconds from `as_of` (`now = 0`). A date `d` read at "start of day"
  is 00:00 of `d` in `plant_timezone`; "end of day" is 00:00 of `d + 1`.
* Due (D-129): first present of the first sales link's line `promised_date`, its
  `requested_date`, then the order's `due_date`, at `profile.erp.due_at` (default end of day).
* `promised_ship` (PINNED, SDD Time convention: "Release and promise dates use the same rule
  with start of day"): 00:00 local of the linked line's `promised_date`; None when there is
  no promised date (even when the due comes from requested or the order due date).
* Op status mapping (PINNED): `in_process` -> `OperationStatus("in_progress", machine,
  remaining_seconds = qty_remaining * unit_seconds of that machine's option)`; `pending` ->
  default `OperationStatus()`; `complete` / `cancelled` ops are NOT emitted (SDD COMP-116 step
  4 "skip", REQ-153 "one Operation per not-done operation") and are counted in
  `counts["done_operations"]` (done ops of built jobs only; closed orders count nothing).
* Machine inference (PINNED, revised by the TF-005 builder-gap ruling): explicit current
  machines claim first; then each in_process op with no current machine, in (order id, op
  sequence) order, runs on its most preferred active option machine (lowest `preference`; a
  work-center-level option resolves to that center's active machines by id) that no other
  in-process op holds. Soft `in_process_machine_inferred`. The overflow case (no free
  machine) is tested in `test_builder_gaps.py`.
* Precedence (D-123): each emitted op links to the previous EMITTED in-plant op (done and
  outside ops are not emitted) with `min_lag = predecessor.move_seconds +
  successor.queue_seconds + sum(lead_seconds of outside ops between)`. An op with no
  predecessor gets its own `queue_seconds` in `ready_time`. Outside lead before the first
  emitted op shifts the job release; outside lead after the last emitted op is subtracted
  from the due date (soft `outside_process_last`).
* Material readiness (D-122): the builder calls `material_ready_times(snapshot, jobs_order,
  profile.erp)` with `jobs_order` = every built job id sorted by (due seconds with None last,
  weight class descending, order id). Op ready time = max(own queue contribution, material
  ready for `(order_id, op_id)`).
* Job release (PINNED, orchestrator ruling): `max(0, start_of_day(order.release_date) if
  set, outside-first lead, material ready for (order_id, None))`, where start of day is
  00:00 local in real elapsed seconds from `as_of`; a past release date therefore adds 0.
* Batch lots (SDD deviation, orchestrator ruling): an open batch op whose `qty_remaining`
  exceeds its `BatchSpec.capacity` is emitted WITHOUT `batch_family` (it runs alone, full
  time) with soft `batch_lot_exceeds_capacity` at `$.production_orders[i].operations[j].batch`
  and the op's lineage; the work center's machines still get `BatchCapacity`. A lot equal to
  the capacity is batched normally.
* Issue paths point at the field that caused them: `po_past_due` at
  `$.purchase_lines[k].promised_date`, from validator and materials alike, kept once.
* Machine calendar: `machine.calendar_id or work_center.calendar_id`, expanded by
  `twinflow.erp.calendars.expand` over the horizon. Overtime (T-219) is not asserted here; a
  machine touched by overtime is only required to keep its regular working time.
* Determinism (REQ-159): the snapshot digest sorts TOP-LEVEL collections, so shuffling them
  gives the same snapshot digest and therefore the identical request and problem digest.
  Nested tuples keep their order in the digest; for the ones whose order carries no meaning
  for the builder (an order's `operations`, ordered by `sequence`; an op's `options`, ranked
  by `preference`) the problem content minus its id must not change.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
from collections import Counter
from collections.abc import Iterable, Iterator
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fixtures.generate import dst_variant, small_shop

from twinflow.erp.build import BUILDER_VERSION, BuildResult, build_problem_from_erp
from twinflow.erp.calendars import expand
from twinflow.erp.digest import snapshot_digest
from twinflow.erp.materials import material_ready_times
from twinflow.erp.profile import ErpPolicy, PlanningProfile, load_planning_profile, profile_digest
from twinflow.erp.schema import (
    BatchSpec,
    ErpSnapshot,
    Machine,
    OrderOperation,
    OutsideProcess,
    ProductionOrder,
    SalesLink,
    SalesOrderLine,
)
from twinflow.scheduling import (
    BaselineSolver,
    BatchCapacity,
    Job,
    Operation,
    OperationStatus,
    OperatorNeed,
    OperatorPool,
    SolveRequest,
    parse_request,
    solve,
    validate,
)

OPEN_STATUSES = frozenset({"planned", "released", "in_process", "on_hold"})
DONE_OP_STATUSES = frozenset({"complete", "cancelled"})
PENALTY = 450.0
DEFAULT_CLASS = 2
STOCK_CLASS = 4
RUSH_CLASS = 4
CLASS_MAP = {"A-KEY": 4, "B-CORE": 2, "C-SPOT": 1}
COIL_FUNCTION_ID = "coil-wire"
SHUFFLES = 20
# Compare calendars up to a day before the horizon end, so the horizon-end arithmetic
# (wall-clock vs elapsed days across fall-back) is not pinned here.
CALENDAR_COMPARE_DAYS = 55

BASE_TOML = f"""
[erp]
default_class = {DEFAULT_CLASS}
stock_class = {STOCK_CLASS}
rush_class = {RUSH_CLASS}
alternate_penalty_seconds = {PENALTY}

[erp.class_weights]
A-KEY = 4
B-CORE = 2
C-SPOT = 1
"""

COIL_SETUP_TOML = f"""
[erp.setup_functions.coil]
id = "{COIL_FUNCTION_ID}"
terms = [{{ kind = "mismatch", param = "wire_family", seconds = 1800.0 }}]
"""


# --- profiles and snapshot edits -----------------------------------------------------------------


def base_profile() -> PlanningProfile:
    return load_planning_profile(BASE_TOML)


def with_erp(profile: PlanningProfile, **changes: Any) -> PlanningProfile:
    """The profile with validated `[erp]` changes."""
    erp = ErpPolicy.model_validate({**profile.erp.model_dump(), **changes})
    return PlanningProfile(scheduling=profile.scheduling, erp=erp)


def coil_setup_profile() -> PlanningProfile:
    return load_planning_profile(BASE_TOML + COIL_SETUP_TOML)


def redigest(snapshot: ErpSnapshot, **changes: Any) -> ErpSnapshot:
    """A copy with `changes` and a correct digest."""
    edited = snapshot.model_copy(update={**changes, "digest": None})
    return edited.model_copy(update={"digest": snapshot_digest(edited)})


def edit_order(snapshot: ErpSnapshot, order_id: str, **changes: Any) -> ErpSnapshot:
    orders = tuple(
        o.model_copy(update=changes) if o.id == order_id else o for o in snapshot.production_orders
    )
    return redigest(snapshot, production_orders=orders)


def edit_op(order: ProductionOrder, op_id: str, **changes: Any) -> tuple[OrderOperation, ...]:
    return tuple(
        op.model_copy(update=changes) if op.op_id == op_id else op for op in order.operations
    )


def order_by_id(snapshot: ErpSnapshot, order_id: str) -> ProductionOrder:
    return next(o for o in snapshot.production_orders if o.id == order_id)


def edit_machine(snapshot: ErpSnapshot, machine_id: str, **changes: Any) -> ErpSnapshot:
    machines = tuple(
        m.model_copy(update=changes) if m.id == machine_id else m for m in snapshot.machines
    )
    return redigest(snapshot, machines=machines)


# --- independent expectations -------------------------------------------------------------------


def seconds_at(day: dt.date, snapshot: ErpSnapshot) -> float:
    """Real elapsed seconds from `as_of` to 00:00 of `day` in the plant timezone.

    Uses timestamps on purpose: subtracting two datetimes that share one `ZoneInfo` object
    gives the WALL-CLOCK difference, which is wrong across a DST change.
    """
    midnight = dt.datetime.combine(day, dt.time(), tzinfo=ZoneInfo(snapshot.plant_timezone))
    return midnight.timestamp() - snapshot.as_of.timestamp()


def end_of_day(day: dt.date, snapshot: ErpSnapshot) -> float:
    return seconds_at(day + dt.timedelta(days=1), snapshot)


def is_open(order: ProductionOrder) -> bool:
    return order.status in OPEN_STATUSES and any(
        op.status not in DONE_OP_STATUSES for op in order.operations
    )


def open_orders(snapshot: ErpSnapshot) -> list[ProductionOrder]:
    return [o for o in snapshot.production_orders if is_open(o)]


def sales_line(snapshot: ErpSnapshot, link: SalesLink) -> SalesOrderLine | None:
    for line in snapshot.sales_order_lines:
        key = (line.order_type, line.order_nbr, str(line.line_nbr))
        if key == (link.order_type, link.order_nbr, link.line_nbr):
            return line
    return None


def first_line(snapshot: ErpSnapshot, order: ProductionOrder) -> SalesOrderLine | None:
    return sales_line(snapshot, order.sales_links[0]) if order.sales_links else None


def expected_class(snapshot: ErpSnapshot, order: ProductionOrder) -> int:
    if order.rush:
        return RUSH_CLASS
    line = first_line(snapshot, order)
    if line is None:
        return STOCK_CLASS
    customer = next(c for c in snapshot.customers if c.id == line.customer_id)
    return CLASS_MAP.get(customer.priority_class or "", DEFAULT_CLASS)


def due_date_of(snapshot: ErpSnapshot, order: ProductionOrder) -> dt.date | None:
    line = first_line(snapshot, order)
    if line is not None and line.promised_date is not None:
        return line.promised_date
    if line is not None and line.requested_date is not None:
        return line.requested_date
    return order.due_date


def internal_ops(order: ProductionOrder) -> list[OrderOperation]:
    """The ops that become problem operations: not done, not outside, in sequence order."""
    ops = sorted(order.operations, key=lambda op: op.sequence)
    return [op for op in ops if op.status not in DONE_OP_STATUSES and op.outside is None]


def op_id(order: ProductionOrder, op: OrderOperation) -> str:
    return f"{order.id}:{op.op_id}"


def route_walk(order: ProductionOrder) -> tuple[dict[str, tuple[float, tuple]], float, float]:
    """Per emitted op: (own queue contribution, ((pred id, min_lag),)); release lead; due lead."""
    walk: dict[str, tuple[float, tuple]] = {}
    previous: OrderOperation | None = None
    lead = 0.0
    release_lead = 0.0
    for op in sorted(order.operations, key=lambda o: o.sequence):
        if op.status in DONE_OP_STATUSES:
            continue
        if op.outside is not None:
            lead += op.outside.lead_seconds
            continue
        if previous is None:
            release_lead += lead
            walk[op_id(order, op)] = (op.queue_seconds, ())
        else:
            lag = previous.move_seconds + op.queue_seconds + lead
            walk[op_id(order, op)] = (0.0, ((op_id(order, previous), lag),))
        previous, lead = op, 0.0
    return walk, release_lead, lead


def expected_options(
    snapshot: ErpSnapshot, op: OrderOperation, erp: ErpPolicy
) -> dict[str, tuple[float, float, float]]:
    """machine id -> (fixed_seconds, unit_seconds, preference_cost)."""
    machines = {m.id: m for m in snapshot.machines}
    with_setup = op.work_center_id not in erp.setup_functions
    result: dict[str, tuple[float, float, float]] = {}
    for option in op.options:
        if option.machine_id is not None:
            targets = [machines[option.machine_id]]
        else:
            targets = [m for m in snapshot.machines if m.work_center_id == option.work_center_id]
        for machine in targets:
            if not machine.active:
                continue
            setup = option.setup_seconds if with_setup else 0.0
            fixed = (option.fixed_run_seconds + setup) / machine.efficiency
            unit = option.run_seconds_per_unit / machine.efficiency
            result[machine.id] = (fixed, unit, option.preference * erp.alternate_penalty_seconds)
    return result


def inferred_machine(snapshot: ErpSnapshot, order: ProductionOrder, op: OrderOperation) -> str:
    """The machine the ruled assignment gives an in_process op without a current machine."""
    machine = running_assignment(snapshot)[(order.id, op.op_id)]
    if machine is None:
        raise AssertionError("fixture in_process op has no free option machine")
    return machine


def running_assignment(snapshot: ErpSnapshot) -> dict[tuple[str, str], str | None]:
    """Explicit machines claim first; then inferred ops by (order id, sequence) take their
    most preferred active option machine that no other in-process op holds."""
    machines = {m.id: m for m in snapshot.machines}

    def ranked(op: OrderOperation) -> list[str]:
        found: list[str] = []
        key = lambda o: (o.preference, o.machine_id or "", o.work_center_id)  # noqa: E731
        for option in sorted(op.options, key=key):
            if option.machine_id is not None:
                targets = [option.machine_id] if machines[option.machine_id].active else []
            else:
                targets = sorted(
                    m.id
                    for m in snapshot.machines
                    if m.work_center_id == option.work_center_id and m.active
                )
            found.extend(t for t in targets if t not in found)
        return found

    running = sorted(
        (
            (order, op)
            for order in open_orders(snapshot)
            for op in internal_ops(order)
            if op.status == "in_process"
        ),
        key=lambda pair: (pair[0].id, pair[1].sequence),
    )
    held = {op.current_machine_id for _, op in running if op.current_machine_id is not None}
    result: dict[tuple[str, str], str | None] = {}
    for order, op in running:
        if op.current_machine_id is not None:
            result[(order.id, op.op_id)] = op.current_machine_id
            continue
        free = [m for m in ranked(op) if m not in held]
        result[(order.id, op.op_id)] = free[0] if free else None
        held.update(free[:1])
    return result


def jobs_order(jobs: Iterable[Job]) -> list[str]:
    def key(job: Job) -> tuple[float, int, str]:
        return (math.inf if job.due is None else job.due, -job.weight_class, job.id)

    return [job.id for job in sorted(jobs, key=key)]


# --- result access ------------------------------------------------------------------------------


def build(snapshot: ErpSnapshot, profile: PlanningProfile | None = None) -> BuildResult:
    return build_problem_from_erp(snapshot, profile or base_profile())


def ready(result: BuildResult) -> SolveRequest:
    codes = sorted({i.code for i in result.issues if i.severity == "hard"})
    assert result.status == "ready", f"blocked by hard issues {codes}"
    assert result.request is not None
    return result.request


def jobs_by_id(request: SolveRequest) -> dict[str, Job]:
    return {job.id: job for job in request.problem.jobs}


def ops_by_id(request: SolveRequest) -> dict[str, Operation]:
    return {op.id: op for job in request.problem.jobs for op in job.operations}


def issue_codes(result: BuildResult) -> Counter[str]:
    return Counter(issue.code for issue in result.issues)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@pytest.fixture(scope="module")
def shop() -> ErpSnapshot:
    return small_shop()


@pytest.fixture(scope="module")
def built(shop: ErpSnapshot) -> BuildResult:
    return build(shop)


@pytest.fixture(scope="module")
def request_(built: BuildResult) -> SolveRequest:
    return ready(built)


# --- envelope -----------------------------------------------------------------------------------


def test_builder_version_is_erp_build_1() -> None:
    assert BUILDER_VERSION == "erp-build-1"


def test_build_small_shop_is_ready_with_v2_request(built: BuildResult) -> None:
    request = ready(built)
    assert request.schema_version == "2.0"
    assert request.problem.now == 0.0
    assert built.builder_version == BUILDER_VERSION


def test_build_small_shop_reports_snapshot_and_profile_digests(
    shop: ErpSnapshot, built: BuildResult
) -> None:
    assert built.snapshot_digest == snapshot_digest(shop)
    assert built.profile_digest == profile_digest(base_profile())


def test_build_problem_id_is_erp_prefix_and_16_hex_of_pinned_concatenation(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    joined = "\n".join([snapshot_digest(shop), profile_digest(base_profile()), BUILDER_VERSION])
    expected = "erp-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
    assert request_.problem.id == expected


def test_build_problem_digest_is_sha256_of_canonical_request_json(built: BuildResult) -> None:
    request = ready(built)
    body = canonical(request.model_dump(mode="json")).encode("utf-8")
    assert built.problem_digest == hashlib.sha256(body).hexdigest()


def test_build_profile_change_changes_problem_id(shop: ErpSnapshot, request_: SolveRequest) -> None:
    other = ready(build(shop, with_erp(base_profile(), alternate_penalty_seconds=900.0)))
    assert other.problem.id != request_.problem.id


def test_build_request_carries_the_profile_scheduling_half(request_: SolveRequest) -> None:
    assert request_.profile == base_profile().scheduling


def test_build_counts_match_the_request(shop: ErpSnapshot, built: BuildResult) -> None:
    request = ready(built)
    opened = open_orders(shop)
    done = sum(1 for o in opened for op in o.operations if op.status in DONE_OP_STATUSES)
    assert built.counts == {
        "jobs": len(request.problem.jobs),
        "operations": sum(len(job.operations) for job in request.problem.jobs),
        "machines": len(request.problem.machines),
        "held_jobs": sum(1 for job in request.problem.jobs if job.status == "held"),
        "done_operations": done,
    }
    assert built.counts["jobs"] == len(opened)
    assert built.counts["operations"] == sum(len(internal_ops(o)) for o in opened)
    assert built.counts["machines"] == sum(1 for m in shop.machines if m.active)


def test_build_lineage_index_covers_every_job_and_operation(
    shop: ErpSnapshot, built: BuildResult
) -> None:
    request = ready(built)
    expected = {}
    for order in open_orders(shop):
        expected[order.id] = order.lineage
        for op in internal_ops(order):
            expected[op_id(order, op)] = op.lineage
    ids = {job.id for job in request.problem.jobs} | set(ops_by_id(request))
    assert set(built.lineage_index) == ids
    assert built.lineage_index == expected


def test_build_issues_are_unique_by_code_and_path(built: BuildResult) -> None:
    keys = [(issue.code, issue.path) for issue in built.issues]
    assert len(keys) == len(set(keys))


def test_build_issues_dedupe_validator_and_materials_overlap(
    shop: ErpSnapshot, built: BuildResult
) -> None:
    from twinflow.erp.validate import validate_snapshot

    request = ready(built)
    plan = material_ready_times(shop, jobs_order(request.problem.jobs), base_profile().erp)
    report = validate_snapshot(shop, base_profile())
    keys = {(issue.code, issue.path) for issue in built.issues}
    assert {(i.code, i.path) for i in plan.issues} <= keys
    assert {(i.code, i.path) for i in report.issues} <= keys
    (past_due,) = [i for i in built.issues if i.code == "po_past_due"]  # both sources, kept once
    today = shop.as_of.date()
    (k,) = [
        k
        for k, line in enumerate(shop.purchase_lines)
        if line.status == "open" and line.promised_date is not None and line.promised_date < today
    ]
    assert past_due.path == f"$.purchase_lines[{k}].promised_date"
    assert past_due.lineage == shop.purchase_lines[k].lineage


def test_build_small_shop_carries_only_soft_issues(built: BuildResult) -> None:
    assert built.issues
    assert all(issue.severity == "soft" for issue in built.issues)


# --- jobs ---------------------------------------------------------------------------------------


def test_build_one_job_per_open_order_none_for_complete_or_closed(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    expected = {o.id for o in open_orders(shop)}
    skipped = {o.id for o in shop.production_orders if o.status in {"complete", "closed"}}
    assert skipped, "fixture must contain complete and closed orders"
    assert {job.id for job in request_.problem.jobs} == expected
    assert len(request_.problem.jobs) == len(expected)


def test_build_cancelled_order_and_order_with_every_op_done_give_no_job(
    shop: ErpSnapshot,
) -> None:
    cancelled, all_done = "MO-0009", "MO-0011"
    snapshot = edit_order(shop, cancelled, status="cancelled")
    order = order_by_id(snapshot, all_done)
    done_ops = tuple(op.model_copy(update={"status": "complete"}) for op in order.operations)
    snapshot = edit_order(snapshot, all_done, operations=done_ops, materials=())

    request = ready(build(snapshot))

    assert {cancelled, all_done}.isdisjoint(jobs_by_id(request))
    assert {o.id for o in open_orders(snapshot)} == set(jobs_by_id(request))


def test_build_operation_ids_are_order_colon_op_in_sequence_order(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    jobs = jobs_by_id(request_)
    for order in open_orders(shop):
        expected = [op_id(order, op) for op in internal_ops(order)]
        assert [op.id for op in jobs[order.id].operations] == expected


def test_build_operations_ignore_snapshot_operation_order(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0001")
    snapshot = edit_order(shop, order.id, operations=tuple(reversed(order.operations)))
    job = jobs_by_id(ready(build(snapshot)))[order.id]
    assert [op.id for op in job.operations] == [op_id(order, op) for op in internal_ops(order)]


def test_build_quantity_is_qty_remaining(shop: ErpSnapshot, request_: SolveRequest) -> None:
    ops = ops_by_id(request_)
    for order in open_orders(shop):
        for op in internal_ops(order):
            assert ops[op_id(order, op)].quantity == op.qty_remaining


def test_build_work_center_and_setup_params_copied(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    ops = ops_by_id(request_)
    for order in open_orders(shop):
        for op in internal_ops(order):
            built_op = ops[op_id(order, op)]
            assert built_op.work_center_id == op.work_center_id
            assert built_op.setup_params == op.setup_params


def test_build_weight_class_follows_class_map_rush_and_stock(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    jobs = jobs_by_id(request_)
    for order in open_orders(shop):
        assert jobs[order.id].weight_class == expected_class(shop, order), order.id
        assert jobs[order.id].weight_override is None


def test_build_rush_raises_a_low_class_customer_to_rush_class(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    rushed = [
        o for o in open_orders(shop) if o.rush and o.sales_links and first_line(shop, o) is not None
    ]
    low = [o for o in rushed if expected_class(shop, o.model_copy(update={"rush": False})) < 4]
    assert low, "fixture needs a rush order whose customer class is below 4"
    for order in low:
        assert jobs_by_id(request_)[order.id].weight_class == RUSH_CLASS


def test_build_stock_order_takes_stock_class(shop: ErpSnapshot, request_: SolveRequest) -> None:
    stock = [o for o in open_orders(shop) if not o.sales_links and not o.rush]
    assert stock
    for order in stock:
        assert jobs_by_id(request_)[order.id].weight_class == STOCK_CLASS


def test_build_unmapped_priority_class_takes_default_with_soft_issue(
    shop: ErpSnapshot, built: BuildResult
) -> None:
    request = ready(built)
    unmapped = [
        o
        for o in open_orders(shop)
        if not o.rush
        and (line := first_line(shop, o)) is not None
        and next(c for c in shop.customers if c.id == line.customer_id).priority_class
        not in CLASS_MAP
    ]
    assert unmapped
    for order in unmapped:
        assert jobs_by_id(request)[order.id].weight_class == DEFAULT_CLASS
    soft = [i for i in built.issues if i.code == "unknown_priority_class"]
    assert soft and all(i.severity == "soft" for i in soft)


def test_build_weight_class_uses_the_first_sales_link_customer(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0016")  # first link: a C-SPOT customer line
    a_key = order_by_id(shop, "MO-0019")  # its line belongs to an A-KEY customer
    assert expected_class(shop, order) == 1 and expected_class(shop, a_key) == 4
    links = (*order.sales_links, *a_key.sales_links)
    snapshot = edit_order(shop, order.id, sales_links=links)
    assert jobs_by_id(ready(build(snapshot)))[order.id].weight_class == 1
    reversed_links = (*a_key.sales_links, *order.sales_links)
    snapshot = edit_order(shop, order.id, sales_links=reversed_links)
    assert jobs_by_id(ready(build(snapshot)))[order.id].weight_class == 4


def test_build_on_hold_order_and_hold_flag_give_held_jobs(
    shop: ErpSnapshot, built: BuildResult
) -> None:
    request = ready(built)
    held = {o.id for o in open_orders(shop) if o.status == "on_hold" or o.hold}
    assert any(o.hold and o.status != "on_hold" for o in open_orders(shop))
    assert any(o.status == "on_hold" for o in open_orders(shop))
    statuses = {job.id: job.status for job in request.problem.jobs}
    assert {jid for jid, s in statuses.items() if s == "held"} == held
    assert all(s == "open" for jid, s in statuses.items() if jid not in held)


def test_build_shortage_policy_hold_holds_the_short_order(shop: ErpSnapshot) -> None:
    profile = with_erp(base_profile(), shortage_policy="hold")
    request = ready(build(shop, profile))
    plan = material_ready_times(shop, jobs_order(request.problem.jobs), profile.erp)
    assert plan.held_orders, "fixture must leave one requirement uncovered"
    jobs = jobs_by_id(request)
    for order_id in plan.held_orders:
        assert jobs[order_id].status == "held"


def test_build_shortage_policy_flag_does_not_hold(built: BuildResult, shop: ErpSnapshot) -> None:
    request = ready(built)
    shorts = [i for i in built.issues if i.code == "material_short"]
    assert shorts and all(i.severity == "soft" for i in shorts)
    flagged_holds = {o.id for o in open_orders(shop) if o.status == "on_hold" or o.hold}
    assert {j.id for j in request.problem.jobs if j.status == "held"} == flagged_holds


# --- due dates ----------------------------------------------------------------------------------


def test_build_due_is_end_of_day_of_promised_then_requested_then_order_due(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    jobs = jobs_by_id(request_)
    for order in open_orders(shop):
        day = due_date_of(shop, order)
        expected = None if day is None else end_of_day(day, shop)
        assert jobs[order.id].due == expected, order.id


def test_build_due_prefers_promised_over_requested(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0011")
    line = first_line(shop, order)
    assert line is not None and line.promised_date != line.requested_date
    job = jobs_by_id(ready(build(shop)))[order.id]
    assert line.promised_date is not None
    assert job.due == end_of_day(line.promised_date, shop)


def test_build_due_falls_back_to_requested_then_order_due(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0021")
    line = first_line(shop, order)
    assert line is not None
    requested, order_due = dt.date(2026, 10, 27), dt.date(2026, 10, 29)
    lines = tuple(
        ln.model_copy(update={"promised_date": None, "requested_date": requested})
        if ln is line
        else ln
        for ln in shop.sales_order_lines
    )
    snapshot = edit_order(redigest(shop, sales_order_lines=lines), order.id, due_date=order_due)
    assert jobs_by_id(ready(build(snapshot)))[order.id].due == end_of_day(requested, snapshot)

    lines = tuple(
        ln.model_copy(update={"promised_date": None, "requested_date": None}) if ln is line else ln
        for ln in shop.sales_order_lines
    )
    snapshot = edit_order(redigest(shop, sales_order_lines=lines), order.id, due_date=order_due)
    job = jobs_by_id(ready(build(snapshot)))[order.id]
    assert job.due == end_of_day(order_due, snapshot)
    assert job.promised_ship is None


def test_build_due_at_start_of_day_profile_uses_midnight_of_the_date(shop: ErpSnapshot) -> None:
    request = ready(build(shop, with_erp(base_profile(), due_at="start_of_day")))
    jobs = jobs_by_id(request)
    for order in open_orders(shop):
        day = due_date_of(shop, order)
        expected = None if day is None else seconds_at(day, shop)
        assert jobs[order.id].due == expected, order.id


def test_build_promised_ship_is_start_of_promised_date_else_none(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    jobs = jobs_by_id(request_)
    for order in open_orders(shop):
        line = first_line(shop, order)
        promised = None if line is None else line.promised_date
        expected = None if promised is None else seconds_at(promised, shop)
        assert jobs[order.id].promised_ship == expected, order.id


def test_build_no_due_date_gives_none_and_soft_issue(shop: ErpSnapshot, built: BuildResult) -> None:
    request = ready(built)
    missing = [o for o in open_orders(shop) if due_date_of(shop, o) is None]
    assert missing
    for order in missing:
        assert jobs_by_id(request)[order.id].due is None
    lineages = {i.lineage for i in built.issues if i.code == "no_due_date"}
    assert all(i.severity == "soft" for i in built.issues if i.code == "no_due_date")
    assert {o.lineage for o in missing} <= lineages


def test_build_due_across_fall_back_uses_real_elapsed_seconds() -> None:
    snapshot = dst_variant()
    request = ready(build(snapshot))
    jobs = jobs_by_id(request)
    fall_back = dt.date(2026, 11, 1)
    crossing = [
        o
        for o in open_orders(snapshot)
        if (day := due_date_of(snapshot, o)) is not None and day >= fall_back
    ]
    assert crossing, "dst_variant must have a due date after the fall-back"
    tz = ZoneInfo(snapshot.plant_timezone)
    for order in crossing:
        day = due_date_of(snapshot, order)
        assert day is not None
        midnight = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=tz)
        wall_clock = (
            midnight.replace(tzinfo=None) - snapshot.as_of.replace(tzinfo=None)
        ).total_seconds()
        assert jobs[order.id].due == wall_clock + 3_600.0
        assert jobs[order.id].due == end_of_day(day, snapshot)


# --- options ------------------------------------------------------------------------------------


def test_build_options_expand_to_active_machines_with_efficiency_and_penalty(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    ops = ops_by_id(request_)
    erp = base_profile().erp
    for order in open_orders(shop):
        for op in internal_ops(order):
            got = {
                o.machine_id: (o.fixed_seconds, o.unit_seconds, o.preference_cost)
                for o in ops[op_id(order, op)].options
            }
            want = expected_options(shop, op, erp)
            assert set(got) == set(want), op_id(order, op)
            for machine_id, values in want.items():
                assert got[machine_id] == pytest.approx(values), (op_id(order, op), machine_id)


def test_build_work_center_level_option_expands_to_every_active_machine(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    ops = ops_by_id(request_)
    active_form = sorted(m.id for m in shop.machines if m.work_center_id == "form" and m.active)
    inactive_form = [m.id for m in shop.machines if m.work_center_id == "form" and not m.active]
    assert len(active_form) >= 2 and inactive_form
    wc_level = [
        op_id(order, op)
        for order in open_orders(shop)
        for op in internal_ops(order)
        if op.work_center_id == "form" and all(o.machine_id is None for o in op.options)
    ]
    assert wc_level
    for oid in wc_level:
        assert sorted(o.machine_id for o in ops[oid].options) == active_form


def test_build_machine_level_option_gives_one_processing_option(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    ops = ops_by_id(request_)
    order = order_by_id(shop, "MO-0007")
    pack = next(op for op in order.operations if op.work_center_id == "pack")
    assert [o.machine_id for o in pack.options] == ["pack-1"]
    assert [o.machine_id for o in ops[op_id(order, pack)].options] == ["pack-1"]


def test_build_slow_machine_divides_run_rate_by_its_efficiency(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    slow = next(m for m in shop.machines if m.efficiency == 0.9)
    found = False
    for order in open_orders(shop):
        for op in internal_ops(order):
            erp_option = next((o for o in op.options if o.machine_id == slow.id), None)
            if erp_option is None:
                continue
            built_option = next(
                o for o in ops_by_id(request_)[op_id(order, op)].options if o.machine_id == slow.id
            )
            assert built_option.unit_seconds == pytest.approx(erp_option.run_seconds_per_unit / 0.9)
            setup_plus_fixed = erp_option.fixed_run_seconds + erp_option.setup_seconds
            assert built_option.fixed_seconds == pytest.approx(setup_plus_fixed / 0.9)
            found = True
    assert found


def test_build_inactive_machines_are_dropped_from_machines_and_options(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    inactive = {m.id for m in shop.machines if not m.active}
    assert inactive
    assert {m.id for m in request_.problem.machines} == {m.id for m in shop.machines if m.active}
    used = {o.machine_id for op in ops_by_id(request_).values() for o in op.options}
    assert used.isdisjoint(inactive)


def test_build_profile_setup_function_replaces_erp_setup_seconds(shop: ErpSnapshot) -> None:
    profile = coil_setup_profile()
    request = ready(build(shop, profile))
    ops = ops_by_id(request)
    coil_ops = [
        (order, op)
        for order in open_orders(shop)
        for op in internal_ops(order)
        if op.work_center_id == "coil"
    ]
    assert coil_ops
    machines = {m.id: m for m in shop.machines}
    for order, op in coil_ops:
        by_machine = {o.machine_id: o for o in ops[op_id(order, op)].options}
        for erp_option in op.options:
            assert erp_option.machine_id is not None and erp_option.setup_seconds > 0
            fixed = erp_option.fixed_run_seconds / machines[erp_option.machine_id].efficiency
            assert by_machine[erp_option.machine_id].fixed_seconds == pytest.approx(fixed)
    function = profile.erp.setup_functions["coil"]
    assert function in request.problem.setup_functions
    centers = {wc.id: wc for wc in request.problem.work_centers}
    assert centers["coil"].setup_function_id == COIL_FUNCTION_ID
    assert all(centers[wc].setup_function_id is None for wc in centers if wc != "coil")


def test_build_without_setup_function_adds_erp_setup_to_fixed_seconds(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    order = next(o for o in open_orders(shop) if internal_ops(o)[0].work_center_id == "coil")
    op = internal_ops(order)[0]
    option = op.options[0]
    assert option.setup_seconds > 0 and option.machine_id is not None
    built_option = next(
        o
        for o in ops_by_id(request_)[op_id(order, op)].options
        if o.machine_id == option.machine_id
    )
    assert built_option.fixed_seconds == pytest.approx(
        option.fixed_run_seconds + option.setup_seconds
    )
    assert request_.problem.setup_functions == ()
    assert all(wc.setup_function_id is None for wc in request_.problem.work_centers)


def test_build_work_centers_mirror_the_snapshot(shop: ErpSnapshot, request_: SolveRequest) -> None:
    assert {wc.id for wc in request_.problem.work_centers} == {wc.id for wc in shop.work_centers}


def test_build_setup_family_comes_from_profile_param_else_none(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    assert all(op.setup_family is None for op in ops_by_id(request_).values())
    profile = with_erp(base_profile(), setup_family_param="wire_family")
    ops = ops_by_id(ready(build(shop, profile)))
    for order in open_orders(shop):
        for op in internal_ops(order):
            assert ops[op_id(order, op)].setup_family == op.setup_params.get("wire_family")


# --- batches, crews, pools ----------------------------------------------------------------------


def test_build_batch_op_gets_item_id_family_and_wc_machines_get_capacity(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    ops = ops_by_id(request_)
    batch_wcs: dict[str, BatchSpec] = {}
    for order in open_orders(shop):
        for op in internal_ops(order):
            built_op = ops[op_id(order, op)]
            if op.batch is None:
                assert built_op.batch_family is None
                continue
            batch_wcs[op.work_center_id] = op.batch
            assert built_op.batch_family == order.item_id
    assert batch_wcs
    for machine in request_.problem.machines:
        spec = batch_wcs.get(machine.work_center_id)
        expected = None if spec is None else BatchCapacity(capacity=spec.capacity, unit=spec.unit)
        assert machine.batch == expected, machine.id


def test_build_batch_family_uses_setup_family_when_profile_names_a_param(
    shop: ErpSnapshot,
) -> None:
    profile = with_erp(base_profile(), setup_family_param="recipe")
    ops = ops_by_id(ready(build(shop, profile)))
    batch_ops = [(o, op) for o in open_orders(shop) for op in internal_ops(o) if op.batch]
    assert batch_ops
    for order, op in batch_ops:
        built_op = ops[op_id(order, op)]
        assert built_op.setup_family == op.setup_params["recipe"]
        assert built_op.batch_family == op.setup_params["recipe"]


def test_build_two_batch_capacities_on_one_work_center_block(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0001")
    oven = next(op for op in order.operations if op.batch is not None)
    other = BatchSpec(capacity=250.0, unit=oven.batch.unit if oven.batch else "ea")
    snapshot = edit_order(shop, order.id, operations=edit_op(order, oven.op_id, batch=other))

    result = build(snapshot)

    assert result.status == "blocked"
    assert result.request is None and result.problem_digest is None
    conflict = [i for i in result.issues if i.code == "batch_capacity_conflict"]
    assert conflict and all(i.severity == "hard" for i in conflict)


def _oven_lot(shop: ErpSnapshot, quantity: float) -> tuple[ErpSnapshot, int, int]:
    """MO-0001 resized to `quantity`; returns the snapshot and its oven op's (i, j) indexes."""
    i, order = next((i, o) for i, o in enumerate(shop.production_orders) if o.id == "MO-0001")
    j = next(j for j, op in enumerate(order.operations) if op.batch is not None)
    assert all(op.status == "pending" for op in order.operations)
    resized = tuple(
        op.model_copy(update={"qty_to_produce": quantity, "qty_remaining": quantity})
        for op in order.operations
    )
    return edit_order(shop, order.id, quantity=quantity, operations=resized), i, j


def test_build_clean_fixture_has_no_batch_lot_above_capacity(built: BuildResult) -> None:
    assert issue_codes(built)["batch_lot_exceeds_capacity"] == 0


def test_build_batch_lot_above_capacity_runs_alone_with_soft_issue(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0001")
    oven = next(op for op in order.operations if op.batch is not None)
    assert oven.batch is not None
    snapshot, i, j = _oven_lot(shop, oven.batch.capacity + 500.0)

    result = build(snapshot)
    request = ready(result)

    built_op = ops_by_id(request)[op_id(order, oven)]
    assert built_op.quantity == oven.batch.capacity + 500.0
    assert built_op.batch_family is None
    others = [
        ops_by_id(request)[op_id(o, op)]
        for o in open_orders(snapshot)
        if o.id != order.id
        for op in internal_ops(o)
        if op.batch is not None
    ]
    assert others and all(op.batch_family is not None for op in others)
    capacity = BatchCapacity(capacity=oven.batch.capacity, unit=oven.batch.unit)
    ovens = [m for m in request.problem.machines if m.work_center_id == oven.work_center_id]
    assert ovens and all(m.batch == capacity for m in ovens)
    issues = [i for i in result.issues if i.code == "batch_lot_exceeds_capacity"]
    assert [(x.severity, x.path, x.lineage) for x in issues] == [
        ("soft", f"$.production_orders[{i}].operations[{j}].batch", oven.lineage)
    ]


def test_build_batch_lot_above_capacity_passes_tf002_and_solves_verified(
    shop: ErpSnapshot,
) -> None:
    order = order_by_id(shop, "MO-0001")
    oven = next(op for op in order.operations if op.batch is not None)
    assert oven.batch is not None
    snapshot, _, _ = _oven_lot(shop, oven.batch.capacity + 500.0)

    request = ready(build(snapshot))

    assert validate(request) == []
    assert solve(request, solver=BaselineSolver()).verified is True


def test_build_batch_lot_equal_to_capacity_is_batched_without_issue(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0001")
    oven = next(op for op in order.operations if op.batch is not None)
    assert oven.batch is not None
    snapshot, _, _ = _oven_lot(shop, oven.batch.capacity)

    result = build(snapshot)

    assert ops_by_id(ready(result))[op_id(order, oven)].batch_family == order.item_id
    assert issue_codes(result)["batch_lot_exceeds_capacity"] == 0


def test_build_crew_becomes_operator_need_both_phases(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    ops = ops_by_id(request_)
    crewed = 0
    for order in open_orders(shop):
        for op in internal_ops(order):
            needs = ops[op_id(order, op)].operator_needs
            if op.crew is None:
                assert needs == ()
                continue
            crewed += 1
            assert needs == (
                OperatorNeed(pool_id=op.crew.labor_pool_id, quantity=op.crew.size, phase="both"),
            )
    assert crewed


def test_build_labor_pools_become_operator_pools(shop: ErpSnapshot, request_: SolveRequest) -> None:
    expected = {
        OperatorPool(id=p.id, size=p.size, calendar_id=p.calendar_id) for p in shop.labor_pools
    }
    assert set(request_.problem.operator_pools) == expected


# --- precedence, outside processing, ready times -----------------------------------------------


def test_build_precedence_lag_is_move_plus_queue_plus_outside_lead(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    ops = ops_by_id(request_)
    for order in open_orders(shop):
        walk, _, _ = route_walk(order)
        for oid, (_, preds) in walk.items():
            got = tuple((p.operation_id, p.min_lag) for p in ops[oid].predecessors)
            assert len(got) == len(preds), oid
            for (got_id, got_lag), (want_id, want_lag) in zip(got, preds, strict=True):
                assert got_id == want_id
                assert got_lag == pytest.approx(want_lag), oid


def test_build_outside_op_in_the_middle_is_a_lag_not_an_operation(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    ops = ops_by_id(request_)
    order = order_by_id(shop, "MO-0001")
    sequence = sorted(order.operations, key=lambda op: op.sequence)
    position = next(i for i, op in enumerate(sequence) if op.outside is not None)
    before, outside, after = sequence[position - 1], sequence[position], sequence[position + 1]
    assert outside.outside is not None
    assert op_id(order, outside) not in ops
    (link,) = ops[op_id(order, after)].predecessors
    assert link.operation_id == op_id(order, before)
    lag = before.move_seconds + after.queue_seconds + outside.outside.lead_seconds
    assert link.min_lag == pytest.approx(lag)


def test_build_outside_op_first_shifts_job_release_by_its_lead(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0009")  # released, no hold, release date in the past
    assert order.release_date is not None and seconds_at(order.release_date, shop) <= 0
    first = sorted(order.operations, key=lambda op: op.sequence)[0]
    lead = 3 * 86_400.0
    plating = first.model_copy(
        update={
            "op_id": "0005",
            "sequence": 5,
            "options": (),
            "outside": OutsideProcess(vendor_id="VEND-PLATE", lead_seconds=lead),
            "queue_seconds": 0.0,
            "move_seconds": 0.0,
            "batch": None,
            "crew": None,
        }
    )
    operations = (plating, *edit_op(order, first.op_id, queue_seconds=0.0))
    snapshot = edit_order(shop, order.id, operations=operations, materials=())

    job = jobs_by_id(ready(build(snapshot)))[order.id]

    assert job.release == pytest.approx(lead)
    assert [op.id for op in job.operations][0] == op_id(order, first)
    assert job.operations[0].predecessors == ()
    assert job.operations[0].ready_time == 0.0


def test_build_outside_op_last_moves_due_earlier_with_soft_issue(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0011")
    last = sorted(order.operations, key=lambda op: op.sequence)[-1]
    lead = 3 * 86_400.0
    plating = last.model_copy(
        update={
            "op_id": "0090",
            "sequence": last.sequence + 10,
            "options": (),
            "outside": OutsideProcess(vendor_id="VEND-PLATE", lead_seconds=lead),
            "batch": None,
            "crew": None,
            "lineage": last.lineage.model_copy(update={"key": (order.id, "0090")}),
        }
    )
    snapshot = edit_order(shop, order.id, operations=(*order.operations, plating), materials=())

    result = build(snapshot)
    job = jobs_by_id(ready(result))[order.id]

    day = due_date_of(snapshot, order)
    assert day is not None
    assert job.due == pytest.approx(end_of_day(day, snapshot) - lead)
    assert op_id(order, plating) not in ops_by_id(ready(result))
    issues = [i for i in result.issues if i.code == "outside_process_last"]
    assert [(i.severity, i.lineage) for i in issues] == [("soft", plating.lineage)]


def test_build_cancelled_op_is_skipped_and_precedence_bridges_it(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0019")
    sequence = sorted(order.operations, key=lambda op: op.sequence)
    before, cancelled, after = sequence[0], sequence[1], sequence[2]
    operations = edit_op(order, cancelled.op_id, status="cancelled")
    snapshot = edit_order(shop, order.id, operations=operations)

    ops = ops_by_id(ready(build(snapshot)))

    assert op_id(order, cancelled) not in ops
    (link,) = ops[op_id(order, after)].predecessors
    assert link.operation_id == op_id(order, before)
    assert link.min_lag == pytest.approx(before.move_seconds + after.queue_seconds)


def test_build_ready_times_are_max_of_queue_and_material(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    plan = material_ready_times(shop, jobs_order(request_.problem.jobs), base_profile().erp)
    ops = ops_by_id(request_)
    for order in open_orders(shop):
        walk, _, _ = route_walk(order)
        for op in internal_ops(order):
            if op.status == "in_process":
                continue
            queue, _ = walk[op_id(order, op)]
            material = plan.ready.get((order.id, op.op_id), 0.0)
            assert ops[op_id(order, op)].ready_time == pytest.approx(max(queue, material))


def test_build_late_material_on_an_op_beats_its_queue(shop: ErpSnapshot) -> None:
    """small_shop has no op whose material is later than its queue, so force one."""
    order = order_by_id(shop, "MO-0001")
    first = sorted(order.operations, key=lambda op: op.sequence)[0]
    (wire,) = [r for r in order.materials if r.op_id == first.op_id]
    assert wire.qty_required > wire.qty_issued
    arrival = shop.as_of.date() + dt.timedelta(days=5)
    template = shop.purchase_lines[0]
    supply = template.model_copy(
        update={
            "order_nbr": "PO-7999",
            "item_id": wire.item_id,
            "qty_ordered": 1_000_000.0,
            "qty_received": 0.0,
            "status": "open",
            "promised_date": arrival,
            "receipts": (),
            "lineage": template.lineage.model_copy(update={"key": ("PO", "PO-7999", "1")}),
        }
    )
    inventory = tuple(
        b.model_copy(update={"qty_available": 0.0}) if b.item_id == wire.item_id else b
        for b in shop.inventory
    )
    purchases = tuple(p for p in shop.purchase_lines if p.item_id != wire.item_id) + (supply,)
    snapshot = redigest(shop, inventory=inventory, purchase_lines=purchases)

    op = ops_by_id(ready(build(snapshot)))[op_id(order, first)]

    assert first.queue_seconds < seconds_at(arrival, snapshot)
    assert op.ready_time == pytest.approx(seconds_at(arrival, snapshot))


def test_build_job_release_is_material_ready_time_without_op(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    plan = material_ready_times(shop, jobs_order(request_.problem.jobs), base_profile().erp)
    assert any(v > 0 for (_, op), v in plan.ready.items() if op is None)
    jobs = jobs_by_id(request_)
    for order in open_orders(shop):
        _, release_lead, _ = route_walk(order)
        release = 0.0 if order.release_date is None else seconds_at(order.release_date, shop)
        expected = max(0.0, release, release_lead, plan.ready.get((order.id, None), 0.0))
        assert jobs[order.id].release == pytest.approx(expected), order.id


def test_build_future_release_date_holds_back_job_release(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    jobs = jobs_by_id(request_)
    future = [
        o
        for o in open_orders(shop)
        if o.release_date is not None and seconds_at(o.release_date, shop) > 0
    ]
    assert future, "fixture needs a planned order released after as_of"
    for order in future:
        assert order.release_date is not None
        assert jobs[order.id].release >= seconds_at(order.release_date, shop)


def test_build_past_release_date_does_not_move_release_below_zero(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0009")
    snapshot = edit_order(shop, order.id, materials=(), release_date=dt.date(2026, 9, 1))
    assert jobs_by_id(ready(build(snapshot)))[order.id].release == 0.0


def test_build_release_is_max_of_release_date_and_outside_first_lead(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0009")
    first = sorted(order.operations, key=lambda op: op.sequence)[0]
    lead = 86_400.0
    plating = first.model_copy(
        update={
            "op_id": "0005",
            "sequence": 5,
            "options": (),
            "outside": OutsideProcess(vendor_id="VEND-PLATE", lead_seconds=lead),
            "queue_seconds": 0.0,
            "move_seconds": 0.0,
        }
    )
    release_day = shop.as_of.date() + dt.timedelta(days=3)
    snapshot = edit_order(
        shop,
        order.id,
        operations=(plating, *order.operations),
        materials=(),
        release_date=release_day,
    )
    job = jobs_by_id(ready(build(snapshot)))[order.id]
    assert seconds_at(release_day, snapshot) > lead
    assert job.release == pytest.approx(seconds_at(release_day, snapshot))


# --- shop-floor status --------------------------------------------------------------------------


def test_build_in_process_op_is_in_progress_on_its_machine_with_remaining(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    ops = ops_by_id(request_)
    running = [
        (o, op) for o in open_orders(shop) for op in internal_ops(o) if op.status == "in_process"
    ]
    assert running
    for order, op in running:
        machine_id = op.current_machine_id or inferred_machine(shop, order, op)
        unit = expected_options(shop, op, base_profile().erp)[machine_id][1]
        status = ops[op_id(order, op)].status
        assert status.state == "in_progress"
        assert status.machine_id == machine_id
        assert status.remaining_seconds == pytest.approx(op.qty_remaining * unit)


def test_build_pending_op_has_default_status(shop: ErpSnapshot, request_: SolveRequest) -> None:
    ops = ops_by_id(request_)
    for order in open_orders(shop):
        for op in internal_ops(order):
            if op.status == "pending":
                assert ops[op_id(order, op)].status == OperationStatus()


def test_build_in_process_without_machine_infers_most_preferred_with_soft_issue(
    shop: ErpSnapshot, built: BuildResult
) -> None:
    request = ready(built)
    inferred = [
        (o, op)
        for o in open_orders(shop)
        for op in internal_ops(o)
        if op.status == "in_process" and op.current_machine_id is None
    ]
    assert inferred
    for order, op in inferred:
        assert ops_by_id(request)[op_id(order, op)].status.machine_id == inferred_machine(
            shop, order, op
        )
    issues = [i for i in built.issues if i.code == "in_process_machine_inferred"]
    assert {(i.severity, i.lineage) for i in issues} == {("soft", op.lineage) for _, op in inferred}


def test_build_running_machine_available_from_is_end_of_its_op(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    erp = base_profile().erp
    ends: dict[str, float] = {}
    for order in open_orders(shop):
        for op in internal_ops(order):
            if op.status != "in_process":
                continue
            machine_id = op.current_machine_id or inferred_machine(shop, order, op)
            assert machine_id not in ends, "fixture: one running op per unary machine"
            ends[machine_id] = op.qty_remaining * expected_options(shop, op, erp)[machine_id][1]
    assert len(ends) >= 2
    for machine in request_.problem.machines:
        expected = ends.get(machine.id, 0.0)
        assert machine.state.available_from == pytest.approx(expected), machine.id


# --- calendars ----------------------------------------------------------------------------------


def _horizon(snapshot: ErpSnapshot, days: int) -> dt.datetime:
    return snapshot.as_of + dt.timedelta(days=days)


def _clipped(intervals: Iterable[tuple[float, float]], cut: float) -> list[tuple[float, float]]:
    return [(s, min(e, cut)) for s, e in intervals if s < cut]


def _covers(outer: list[tuple[float, float]], inner: list[tuple[float, float]]) -> bool:
    return all(any(s <= a and b <= e for s, e in outer) for a, b in inner)


def _has_overtime(snapshot: ErpSnapshot, machine: Machine) -> bool:
    center = next(wc for wc in snapshot.work_centers if wc.id == machine.work_center_id)
    return machine.overtime is not None or center.overtime is not None


def assert_machine_calendars(snapshot: ErpSnapshot, request: SolveRequest) -> None:
    tz = ZoneInfo(snapshot.plant_timezone)
    horizon = _horizon(snapshot, base_profile().erp.horizon_days)
    cut = seconds_at(snapshot.as_of.date(), snapshot) + CALENDAR_COMPARE_DAYS * 86_400.0
    calendars = {c.id: c for c in request.problem.calendars}
    erp_calendars = {c.id: c for c in snapshot.calendars}
    centers = {wc.id: wc for wc in snapshot.work_centers}
    for machine in (m for m in snapshot.machines if m.active):
        built_machine = next(m for m in request.problem.machines if m.id == machine.id)
        source_id = machine.calendar_id or centers[machine.work_center_id].calendar_id
        assert source_id is not None
        regular = _clipped(expand(erp_calendars[source_id], tz, snapshot.as_of, horizon), cut)
        assert built_machine.calendar_id in calendars, machine.id
        got = _clipped(calendars[built_machine.calendar_id].intervals, cut)
        if _has_overtime(snapshot, machine):
            assert _covers(got, regular), machine.id  # overtime (T-219) may add time
            continue
        assert built_machine.calendar_id == source_id, machine.id
        assert got == pytest.approx(regular), machine.id


def test_build_machine_calendars_fall_back_to_work_center_calendar(
    shop: ErpSnapshot, request_: SolveRequest
) -> None:
    assert all(m.calendar_id is None for m in shop.machines)
    assert_machine_calendars(shop, request_)


def test_build_machine_own_calendar_wins_over_work_center(shop: ErpSnapshot) -> None:
    snapshot = edit_machine(shop, "grind-1", calendar_id="24x5")
    request = ready(build(snapshot))
    machine = next(m for m in request.problem.machines if m.id == "grind-1")
    assert machine.calendar_id == "24x5"
    assert_machine_calendars(snapshot, request)


def test_build_pool_calendars_are_present(request_: SolveRequest) -> None:
    calendar_ids = {c.id for c in request_.problem.calendars}
    for pool in request_.problem.operator_pools:
        assert pool.calendar_id is None or pool.calendar_id in calendar_ids


# --- TF-002 round trip and solve ----------------------------------------------------------------


def test_build_request_passes_tf002_parse_request_and_validate(shop: ErpSnapshot) -> None:
    request = ready(build(shop))
    parsed = parse_request(request.model_dump_json())
    assert isinstance(parsed, SolveRequest), parsed
    assert parsed == request
    assert validate(request) == []


def test_build_small_shop_baseline_solve_is_verified(shop: ErpSnapshot) -> None:
    request = ready(build(shop))
    result = solve(request, solver=BaselineSolver())
    assert result.schedule.status in {"FEASIBLE", "OPTIMAL"}, result.issues
    assert result.verified is True, result.issues


def test_build_with_setup_function_solves_verified(shop: ErpSnapshot) -> None:
    request = ready(build(shop, coil_setup_profile()))
    result = solve(request, solver=BaselineSolver())
    assert result.verified is True, result.issues


# --- blocked builds -----------------------------------------------------------------------------


def test_build_hard_issue_blocks_with_no_request_and_no_digest(shop: ErpSnapshot) -> None:
    order = order_by_id(shop, "MO-0009")
    last = sorted(order.operations, key=lambda op: op.sequence)[-1]
    snapshot = edit_order(
        shop, order.id, operations=edit_op(order, last.op_id, work_center_id="paint")
    )

    result = build(snapshot)

    assert result.status == "blocked"
    assert result.request is None
    assert result.problem_digest is None
    assert result.snapshot_digest == snapshot_digest(snapshot)
    assert result.builder_version == BUILDER_VERSION
    hard = [i for i in result.issues if i.code == "unknown_work_center"]
    assert hard and all(i.severity == "hard" for i in hard)


# --- determinism --------------------------------------------------------------------------------


TOP_LEVEL_COLLECTIONS = (
    "items",
    "boms",
    "routings",
    "work_centers",
    "machines",
    "calendars",
    "labor_pools",
    "production_orders",
    "sales_order_lines",
    "customers",
    "vendors",
    "purchase_lines",
    "inventory",
)


def _mixed(values: tuple[Any, ...], rng: random.Random) -> tuple[Any, ...]:
    items = list(values)
    rng.shuffle(items)
    return tuple(items)


def _top_level_shuffled(snapshot: ErpSnapshot, rng: random.Random) -> ErpSnapshot:
    """Every top-level collection shuffled; nested tuples untouched."""
    changes = {name: _mixed(getattr(snapshot, name), rng) for name in TOP_LEVEL_COLLECTIONS}
    return redigest(snapshot, **changes)


def _nested_shuffled(snapshot: ErpSnapshot, rng: random.Random) -> ErpSnapshot:
    """Each order's operations (ordered by `sequence`) and each op's options (ranked by
    `preference`) shuffled. Materials and sales links keep their order: it carries meaning
    (allocation ties, first link)."""
    orders = []
    for order in snapshot.production_orders:
        ops = tuple(
            op.model_copy(update={"options": _mixed(op.options, rng)}) for op in order.operations
        )
        orders.append(order.model_copy(update={"operations": _mixed(ops, rng)}))
    return redigest(snapshot, production_orders=tuple(orders))


def _content(result: BuildResult) -> tuple[str, str, dict[str, int], Counter[str]]:
    """Everything but the problem id: request minus id, lineage, counts, issue bag.

    Issue paths carry collection indexes, so issues compare by (code, severity, lineage).
    """
    request = ready(result)
    body = request.model_dump(mode="json")
    del body["problem"]["id"]
    lineage = canonical({k: v.model_dump(mode="json") for k, v in result.lineage_index.items()})
    issues = Counter(
        canonical([i.code, i.severity, i.lineage.model_dump(mode="json") if i.lineage else None])
        for i in result.issues
    )
    return canonical(body), lineage, dict(result.counts), issues


def test_build_top_level_shuffles_give_the_identical_problem_digest_and_request(
    shop: ErpSnapshot, built: BuildResult
) -> None:
    rng = random.Random(2026)  # noqa: S311  # deterministic test shuffles, not crypto
    reference = ready(built)
    moved = 0
    for _ in range(SHUFFLES):
        shuffled = _top_level_shuffled(shop, rng)
        moved += shuffled.production_orders != shop.production_orders
        assert snapshot_digest(shuffled) == snapshot_digest(shop)
        result = build(shuffled)
        assert result.problem_digest == built.problem_digest
        assert ready(result) == reference
        assert result.lineage_index == built.lineage_index
        assert result.counts == built.counts
    assert moved == SHUFFLES  # every shuffle really reordered the snapshot


def test_build_nested_order_free_shuffles_keep_the_problem_content(
    shop: ErpSnapshot, built: BuildResult
) -> None:
    rng = random.Random(2027)  # noqa: S311  # deterministic test shuffles, not crypto
    reference = _content(built)
    for _ in range(SHUFFLES):
        shuffled = _nested_shuffled(shop, rng)
        assert snapshot_digest(shuffled) != snapshot_digest(shop)  # nested order is in the digest
        result = build(shuffled)
        assert _content(result) == reference
        assert build(shuffled).problem_digest == result.problem_digest


def test_build_same_snapshot_and_profile_give_the_same_problem_digest(shop: ErpSnapshot) -> None:
    first, second = build(shop), build(small_shop())
    assert first.problem_digest is not None
    assert first.problem_digest == second.problem_digest
    assert ready(first).problem.id == ready(second).problem.id


def test_build_json_round_trip_of_snapshot_gives_the_same_problem_digest(
    shop: ErpSnapshot, built: BuildResult
) -> None:
    round_tripped = ErpSnapshot.model_validate_json(shop.model_dump_json())
    assert snapshot_digest(round_tripped) == snapshot_digest(shop)
    assert build(round_tripped).problem_digest == built.problem_digest


def _iter_ids(request: SolveRequest) -> Iterator[list[str]]:
    yield [job.id for job in request.problem.jobs]
    yield [m.id for m in request.problem.machines]
    yield [wc.id for wc in request.problem.work_centers]
    yield [c.id for c in request.problem.calendars]
    yield [p.id for p in request.problem.operator_pools]
    for job in request.problem.jobs:
        for op in job.operations:
            yield [o.machine_id for o in op.options]


def test_build_output_collections_are_sorted_by_id(request_: SolveRequest) -> None:
    for ids in _iter_ids(request_):
        assert ids == sorted(ids)
