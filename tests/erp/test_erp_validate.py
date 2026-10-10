"""Tests for the snapshot validator (T-199, COMP-112, REQ-148, REQ-152, REQ-177).

Contract under test: `validate_snapshot(snapshot, profile) -> ValidationReport` with fields
`snapshot_digest`, `issues: tuple[ErpIssue, ...]`, `hard_count`, `soft_count`,
`validity == "provisional"` and `evidence_kind == "erp_validation"`. Every issue is reported
(no first-error stop), sorted by (severity with hard first, path, code). Pure: no clock read;
the horizon is `as_of + profile.erp.horizon_days`.

Each test starts from the clean `small_shop()` fixture, makes one targeted change with
`model_copy(update=...)` (the models are frozen) and re-digests the result, so the only new
issue is the one under test (an un-redigested copy would also raise `digest_mismatch`).

Choices made where the SDD leaves room (the GREEN implementation must follow them):

* Which codes the validator raises. All of REQ-152 plus the SDD hard codes `digest_mismatch`,
  `batch_capacity_conflict` and `qty_remaining_exceeds_order`, the soft `qty_inconsistent`
  and `inactive_machine_option`, and `unknown_priority_class` (the validator has the profile
  and its class map, so it flags each customer whose class is missing; the builder dedupes).
  Builder-only codes (COMP-115/116) are NOT raised here: `material_short`,
  `in_process_machine_inferred`, `outside_process_last`, `po_no_promise`, `builder_invariant`.
  `overtime_overlaps_exception` is not a validator code: the schema already rejects two
  overtime exceptions on one date (orchestrator deviation). `schema` and
  `unsupported_schema_version` belong to the parse gate (COMP-111).
* Clean fixture soft set: exactly {`unknown_priority_class` (customer CUST-D01, class D-NEW),
  `no_due_date` (MO-0001), `po_past_due` (PO-7001), `inactive_machine_option` (form ops that
  list form-3)}. No hard issue.
* Path granularity. A `$`-rooted JSON path to the record, plus `.field` when one field is the
  culprit (unknown references, `qty_remaining`, a PO's `promised_date`, a customer's
  `priority_class`, an op's `batch`). Whole-record judgements use the record path alone:
  `duplicate_key` (the later duplicate), `no_option`, `no_active_option`,
  `inactive_machine_option`, `qty_inconsistent`, `no_due_date`, `past_due_no_promise`,
  `calendar_no_working_time`, `sales_link_missing` (the link: `...sales_links[k]`), and source
  issues (`$.source_issues[i]`). `digest_mismatch` is `$.digest` with no lineage.
* Lineage is that of the record that holds the path: an option's own lineage for option
  fields, the operation's for operation fields (including `crew`, `outside`, `batch`), the
  order's for order fields and for a sales link (links carry no lineage).
* `qty_remaining_exceeds_order` is the order-level rule: an operation's `qty_remaining` above
  its order's `quantity` (the schema already rejects op-level `qty_remaining >
  qty_to_produce`). An op whose `qty_to_produce` alone exceeds the order quantity (a scrap
  allowance) is not flagged.
* "Open" means order status planned, released, in_process or on_hold and operation status not
  complete or cancelled. Option checks (`no_option`, `no_active_option`,
  `inactive_machine_option`) apply to open operations only.
* `no_option`: an open, in-plant operation whose options resolve to no machine at all (a
  center-level option on a work center with no machines). `no_active_option`: machines
  exist but every one is inactive. They do not both fire on one operation.
* `batch_capacity_conflict`: the first batch capacity seen on a work center in document order
  is the reference; each later order operation with a different capacity is reported at its
  `.batch`.
* Past due means a date strictly before the plant-local date of `as_of`. `po_past_due` is for
  open purchase lines only. `past_due_no_promise` is for an open order whose due (requested
  date or order due date) is past and which has no promised date on any linked line.
* `snapshot_digest` in the report is the recomputed SHA-256, never the supplied value.
* `SalesLink.line_nbr` (text) matches `SalesOrderLine.line_nbr` (int) through `str(line_nbr)`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fixtures.generate import small_shop

from twinflow.erp.digest import snapshot_digest
from twinflow.erp.issues import ErpIssue
from twinflow.erp.profile import PlanningProfile, load_planning_profile
from twinflow.erp.schema import (
    CrewNeed,
    ErpSnapshot,
    Lineage,
    OperationOption,
    OrderOperation,
    OutsideProcess,
    ProductionOrder,
    SourceIssue,
    TimeWindow,
    WeekdayWindows,
)
from twinflow.erp.validate import ValidationReport, validate_snapshot

PLANT_TZ = ZoneInfo("America/Chicago")
OPEN_ORDERS = {"planned", "released", "in_process", "on_hold"}
DONE_OPS = {"complete", "cancelled"}
WRONG_DIGEST = "0" * 64

CLEAN_SOFT_CODES = frozenset(
    {"unknown_priority_class", "no_due_date", "po_past_due", "inactive_machine_option"}
)
BUILDER_ONLY_CODES = frozenset(
    {
        "material_short",
        "in_process_machine_inferred",
        "outside_process_last",
        "po_no_promise",
        "builder_invariant",
        "overtime_overlaps_exception",
    }
)

MAPPED_CLASSES = '"A-KEY" = 4\n"B-CORE" = 2\n"C-SPOT" = 1\n'


def make_profile(horizon_days: int = 56, extra_classes: str = "") -> PlanningProfile:
    """The profile maps the fixture's A/B/C classes; D-NEW stays unmapped unless added."""
    text = (
        f"[erp]\nhorizon_days = {horizon_days}\n\n"
        f"[erp.class_weights]\n{MAPPED_CLASSES}{extra_classes}"
    )
    return load_planning_profile(text)


@pytest.fixture(scope="module")
def shop() -> ErpSnapshot:
    return small_shop()


@pytest.fixture(scope="module")
def profile() -> PlanningProfile:
    return make_profile()


@pytest.fixture(scope="module")
def clean_report(shop: ErpSnapshot, profile: PlanningProfile) -> ValidationReport:
    return validate_snapshot(shop, profile)


# --- helpers ------------------------------------------------------------------------------------


def redigest(snap: ErpSnapshot) -> ErpSnapshot:
    """Set the digest to the true one so a mutation adds no `digest_mismatch`."""
    return snap.model_copy(update={"digest": snapshot_digest(snap)})


def with_records(snap: ErpSnapshot, collection: str, records: tuple[Any, ...]) -> ErpSnapshot:
    return redigest(snap.model_copy(update={collection: records}))


def replace_record(snap: ErpSnapshot, collection: str, index: int, **update: Any) -> ErpSnapshot:
    records = list(getattr(snap, collection))
    records[index] = records[index].model_copy(update=update)
    return with_records(snap, collection, tuple(records))


def replace_op(snap: ErpSnapshot, order_ix: int, op_ix: int, **update: Any) -> ErpSnapshot:
    order = snap.production_orders[order_ix]
    ops = list(order.operations)
    ops[op_ix] = ops[op_ix].model_copy(update=update)
    return replace_record(snap, "production_orders", order_ix, operations=tuple(ops))


def is_open(order: ProductionOrder, op: OrderOperation) -> bool:
    return order.status in OPEN_ORDERS and op.status not in DONE_OPS


def find_open_op(snap: ErpSnapshot, predicate: Any) -> tuple[int, int]:
    """Indexes of the first open order operation matching `predicate(op)`."""
    for i, order in enumerate(snap.production_orders):
        for j, op in enumerate(order.operations):
            if is_open(order, op) and predicate(op):
                return i, j
    raise AssertionError("fixture has no matching open operation")


def op_path(i: int, j: int) -> str:
    return f"$.production_orders[{i}].operations[{j}]"


def issues_with(report: ValidationReport, code: str) -> list[ErpIssue]:
    return [issue for issue in report.issues if issue.code == code]


def issue_at(report: ValidationReport, code: str, path: str) -> ErpIssue:
    """The single issue with `code` at exactly `path`."""
    found = [issue for issue in issues_with(report, code) if issue.path == path]
    paths = sorted(issue.path for issue in issues_with(report, code))
    assert len(found) == 1, f"expected one {code} at {path}; {code} paths: {paths}"
    return found[0]


def codes(report: ValidationReport) -> set[str]:
    return {issue.code for issue in report.issues}


def form_op_with_explicit_machines(snap: ErpSnapshot) -> tuple[int, int]:
    return find_open_op(
        snap,
        lambda op: (
            op.work_center_id == "form"
            and len(op.options) == 3
            and all(o.machine_id is not None for o in op.options)
        ),
    )


# --- report envelope (REQ-152, REQ-177) ---------------------------------------------------------


def test_validate_report_is_provisional_erp_validation_evidence(
    clean_report: ValidationReport,
) -> None:
    assert clean_report.validity == "provisional"
    assert clean_report.evidence_kind == "erp_validation"
    assert isinstance(clean_report.issues, tuple)


def test_validate_report_snapshot_digest_is_recomputed(
    shop: ErpSnapshot, profile: PlanningProfile, clean_report: ValidationReport
) -> None:
    assert clean_report.snapshot_digest == snapshot_digest(shop)
    wrong = shop.model_copy(update={"digest": WRONG_DIGEST})
    assert validate_snapshot(wrong, profile).snapshot_digest == snapshot_digest(shop)


def test_validate_report_counts_match_issue_severities(shop: ErpSnapshot) -> None:
    snap = replace_record(shop, "machines", 0, work_center_id="ghost-wc")
    report = validate_snapshot(snap, make_profile())
    hard = sum(1 for issue in report.issues if issue.severity == "hard")
    soft = sum(1 for issue in report.issues if issue.severity == "soft")
    assert hard >= 1
    assert (report.hard_count, report.soft_count) == (hard, soft)


def test_validate_issues_sorted_hard_first_then_path_then_code(shop: ErpSnapshot) -> None:
    snap = replace_record(shop, "machines", 0, work_center_id="ghost-wc")
    snap = replace_record(snap, "sales_order_lines", 0, customer_id="ghost-customer")
    snap = snap.model_copy(update={"digest": WRONG_DIGEST})
    report = validate_snapshot(snap, make_profile())
    rank = {"hard": 0, "soft": 1}
    keys = [(rank[i.severity], i.path, i.code) for i in report.issues]
    assert keys == sorted(keys)
    assert report.issues[0].severity == "hard"
    assert report.issues[-1].severity == "soft"


# --- clean fixture ------------------------------------------------------------------------------


def test_validate_clean_small_shop_has_no_hard_issue(clean_report: ValidationReport) -> None:
    hard = [(i.code, i.path) for i in clean_report.issues if i.severity == "hard"]
    assert hard == []
    assert clean_report.hard_count == 0


def test_validate_clean_small_shop_soft_codes_are_exactly_the_planted_set(
    clean_report: ValidationReport,
) -> None:
    assert codes(clean_report) == CLEAN_SOFT_CODES
    assert clean_report.soft_count == len(clean_report.issues)


def test_validate_raises_no_builder_only_code(clean_report: ValidationReport) -> None:
    assert codes(clean_report).isdisjoint(BUILDER_ONLY_CODES)


def test_validate_clean_issues_all_carry_lineage(clean_report: ValidationReport) -> None:
    assert all(issue.lineage is not None for issue in clean_report.issues)


# --- digest -------------------------------------------------------------------------------------


def test_validate_digest_mismatch_is_hard_at_digest(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    report = validate_snapshot(shop.model_copy(update={"digest": WRONG_DIGEST}), profile)
    issue = issue_at(report, "digest_mismatch", "$.digest")
    assert issue.severity == "hard"
    assert issue.lineage is None


def test_validate_absent_digest_is_not_a_mismatch(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    report = validate_snapshot(shop.model_copy(update={"digest": None}), profile)
    assert issues_with(report, "digest_mismatch") == []


# --- duplicate keys -----------------------------------------------------------------------------


def test_validate_duplicate_key_machine_reports_the_later_record(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    dup_lineage = Lineage(table="machine", key=("coil-1", "dup"))
    dup = shop.machines[0].model_copy(update={"lineage": dup_lineage})
    snap = with_records(shop, "machines", (*shop.machines, dup))
    issue = issue_at(
        validate_snapshot(snap, profile), "duplicate_key", f"$.machines[{len(shop.machines)}]"
    )
    assert issue.severity == "hard"
    assert issue.lineage == dup_lineage


def test_validate_duplicate_key_sales_line_uses_composite_key(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    dup_lineage = Lineage(table="sales_order_line", key=("SO", "dup", "1"))
    dup = shop.sales_order_lines[0].model_copy(update={"lineage": dup_lineage})
    snap = with_records(shop, "sales_order_lines", (*shop.sales_order_lines, dup))
    path = f"$.sales_order_lines[{len(shop.sales_order_lines)}]"
    issue = issue_at(validate_snapshot(snap, profile), "duplicate_key", path)
    assert issue.severity == "hard"
    assert issue.lineage == dup_lineage


# --- unknown references (hard) ------------------------------------------------------------------


def test_validate_unknown_work_center_on_machine(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    snap = replace_record(shop, "machines", 0, work_center_id="ghost-wc")
    issue = issue_at(
        validate_snapshot(snap, profile), "unknown_work_center", "$.machines[0].work_center_id"
    )
    assert issue.severity == "hard"
    assert issue.lineage == shop.machines[0].lineage


def test_validate_unknown_work_center_on_order_operation(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    i, j = form_op_with_explicit_machines(shop)
    snap = replace_op(shop, i, j, work_center_id="ghost-wc")
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_work_center", f"{op_path(i, j)}.work_center_id")
    assert issue.severity == "hard"
    assert issue.lineage == shop.production_orders[i].operations[j].lineage


def test_validate_unknown_machine_on_option(shop: ErpSnapshot, profile: PlanningProfile) -> None:
    i, j = form_op_with_explicit_machines(shop)
    op = shop.production_orders[i].operations[j]
    options = (op.options[0].model_copy(update={"machine_id": "ghost-9"}), *op.options[1:])
    snap = replace_op(shop, i, j, options=options)
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_machine", f"{op_path(i, j)}.options[0].machine_id")
    assert issue.severity == "hard"
    assert issue.lineage == op.options[0].lineage


def test_validate_unknown_machine_on_current_machine(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    i, j = find_open_op(shop, lambda op: op.current_machine_id is not None)
    snap = replace_op(shop, i, j, current_machine_id="ghost-9")
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_machine", f"{op_path(i, j)}.current_machine_id")
    assert issue.severity == "hard"
    assert issue.lineage == shop.production_orders[i].operations[j].lineage


def test_validate_unknown_calendar_on_work_center(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    snap = replace_record(shop, "work_centers", 0, calendar_id="ghost-cal")
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_calendar", "$.work_centers[0].calendar_id")
    assert issue.severity == "hard"
    assert issue.lineage == shop.work_centers[0].lineage


def test_validate_unknown_calendar_on_labor_pool(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    snap = replace_record(shop, "labor_pools", 0, calendar_id="ghost-cal")
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_calendar", "$.labor_pools[0].calendar_id")
    assert issue.severity == "hard"
    assert issue.lineage == shop.labor_pools[0].lineage


def test_validate_unknown_labor_pool_on_crew(shop: ErpSnapshot, profile: PlanningProfile) -> None:
    i, j = find_open_op(shop, lambda op: op.crew is not None)
    snap = replace_op(shop, i, j, crew=CrewNeed(labor_pool_id="ghost-pool", size=1))
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_labor_pool", f"{op_path(i, j)}.crew.labor_pool_id")
    assert issue.severity == "hard"
    assert issue.lineage == shop.production_orders[i].operations[j].lineage


def test_validate_unknown_item_on_production_order(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    snap = replace_record(shop, "production_orders", 8, item_id="ghost-item")
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_item", "$.production_orders[8].item_id")
    assert issue.severity == "hard"
    assert issue.lineage == shop.production_orders[8].lineage


def test_validate_unknown_item_on_material_requirement(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    order = shop.production_orders[8]
    materials = (order.materials[0].model_copy(update={"item_id": "ghost-item"}),)
    snap = replace_record(shop, "production_orders", 8, materials=materials + order.materials[1:])
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_item", "$.production_orders[8].materials[0].item_id")
    assert issue.severity == "hard"
    assert issue.lineage == order.materials[0].lineage


def test_validate_unknown_item_on_inventory(shop: ErpSnapshot, profile: PlanningProfile) -> None:
    snap = replace_record(shop, "inventory", 0, item_id="ghost-item")
    issue = issue_at(validate_snapshot(snap, profile), "unknown_item", "$.inventory[0].item_id")
    assert issue.severity == "hard"
    assert issue.lineage == shop.inventory[0].lineage


# --- options and machines -----------------------------------------------------------------------


def test_validate_machine_outside_work_center(shop: ErpSnapshot, profile: PlanningProfile) -> None:
    i, j = form_op_with_explicit_machines(shop)
    op = shop.production_orders[i].operations[j]
    options = (op.options[0].model_copy(update={"machine_id": "coil-1"}), *op.options[1:])
    snap = replace_op(shop, i, j, options=options)
    report = validate_snapshot(snap, profile)
    path = f"{op_path(i, j)}.options[0].machine_id"
    issue = issue_at(report, "machine_outside_work_center", path)
    assert issue.severity == "hard"
    assert issue.lineage == op.options[0].lineage
    assert [x for x in issues_with(report, "unknown_machine") if x.path == path] == []


def test_validate_no_option_when_work_center_has_no_machine(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    machines = tuple(m for m in shop.machines if m.work_center_id != "oven")
    snap = with_records(shop, "machines", machines)
    i, j = find_open_op(shop, lambda op: op.work_center_id == "oven")
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "no_option", op_path(i, j))
    assert issue.severity == "hard"
    assert issue.lineage == shop.production_orders[i].operations[j].lineage
    assert [x for x in issues_with(report, "no_active_option") if x.path == op_path(i, j)] == []


def test_validate_no_active_option_when_every_option_is_inactive(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    i, j = form_op_with_explicit_machines(shop)
    op = shop.production_orders[i].operations[j]
    only_inactive = tuple(o for o in op.options if o.machine_id == "form-3")
    snap = replace_op(shop, i, j, options=only_inactive)
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "no_active_option", op_path(i, j))
    assert issue.severity == "hard"
    assert issue.lineage == op.lineage
    stale = [x for x in issues_with(report, "inactive_machine_option") if x.path == op_path(i, j)]
    assert stale == []


def test_validate_inactive_machine_option_when_some_options_inactive(
    shop: ErpSnapshot, clean_report: ValidationReport
) -> None:
    i, j = form_op_with_explicit_machines(shop)
    issue = issue_at(clean_report, "inactive_machine_option", op_path(i, j))
    assert issue.severity == "soft"
    assert issue.lineage == shop.production_orders[i].operations[j].lineage


def test_validate_inactive_machine_option_skips_done_orders(
    shop: ErpSnapshot, clean_report: ValidationReport
) -> None:
    done_paths = {
        op_path(i, j)
        for i, order in enumerate(shop.production_orders)
        for j, op in enumerate(order.operations)
        if not is_open(order, op)
    }
    flagged = {x.path for x in issues_with(clean_report, "inactive_machine_option")}
    assert flagged
    assert flagged.isdisjoint(done_paths)


# --- quantities ---------------------------------------------------------------------------------


def test_validate_qty_remaining_exceeds_order_quantity(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    i, j = form_op_with_explicit_machines(shop)
    too_many = shop.production_orders[i].quantity + 100.0
    snap = replace_op(shop, i, j, qty_to_produce=too_many, qty_remaining=too_many)
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "qty_remaining_exceeds_order", f"{op_path(i, j)}.qty_remaining")
    assert issue.severity == "hard"
    assert issue.lineage == shop.production_orders[i].operations[j].lineage


def test_validate_qty_to_produce_above_order_alone_is_not_flagged(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    i, j = form_op_with_explicit_machines(shop)
    allowance = shop.production_orders[i].quantity + 100.0
    snap = replace_op(shop, i, j, qty_to_produce=allowance)
    assert issues_with(validate_snapshot(snap, profile), "qty_remaining_exceeds_order") == []


def test_validate_qty_inconsistent_when_complete_plus_scrap_exceeds(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    i, j = form_op_with_explicit_machines(shop)
    qty = shop.production_orders[i].operations[j].qty_to_produce
    snap = replace_op(shop, i, j, qty_complete=qty, qty_scrapped=5.0, qty_remaining=0.0)
    issue = issue_at(validate_snapshot(snap, profile), "qty_inconsistent", op_path(i, j))
    assert issue.severity == "soft"
    assert issue.lineage == shop.production_orders[i].operations[j].lineage


def test_validate_batch_capacity_conflict_on_one_work_center(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    batch_ops = [
        (i, j)
        for i, order in enumerate(shop.production_orders)
        for j, op in enumerate(order.operations)
        if op.batch is not None and is_open(order, op)
    ]
    i, j = batch_ops[-1]
    op = shop.production_orders[i].operations[j]
    assert op.batch is not None
    half = op.batch.model_copy(update={"capacity": op.batch.capacity / 2})
    snap = replace_op(shop, i, j, batch=half)
    issue = issue_at(
        validate_snapshot(snap, profile), "batch_capacity_conflict", f"{op_path(i, j)}.batch"
    )
    assert issue.severity == "hard"
    assert issue.lineage == op.lineage


# --- customers, vendors, sales links (soft) -----------------------------------------------------


def test_validate_unknown_customer_on_sales_line(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    snap = replace_record(shop, "sales_order_lines", 0, customer_id="ghost-customer")
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_customer", "$.sales_order_lines[0].customer_id")
    assert issue.severity == "soft"
    assert issue.lineage == shop.sales_order_lines[0].lineage


def test_validate_unknown_vendor_on_purchase_line(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    snap = replace_record(shop, "purchase_lines", 2, vendor_id="ghost-vendor")
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_vendor", "$.purchase_lines[2].vendor_id")
    assert issue.severity == "soft"
    assert issue.lineage == shop.purchase_lines[2].lineage


def test_validate_unknown_vendor_on_outside_process(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    i, j = find_open_op(shop, lambda op: op.outside is not None)
    op = shop.production_orders[i].operations[j]
    assert op.outside is not None
    outside = OutsideProcess(vendor_id="ghost-vendor", lead_seconds=op.outside.lead_seconds)
    snap = replace_op(shop, i, j, outside=outside)
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "unknown_vendor", f"{op_path(i, j)}.outside.vendor_id")
    assert issue.severity == "soft"
    assert issue.lineage == op.lineage


def test_validate_unknown_priority_class_for_unmapped_customer(
    shop: ErpSnapshot, clean_report: ValidationReport
) -> None:
    k = next(n for n, c in enumerate(shop.customers) if c.priority_class == "D-NEW")
    issue = issue_at(clean_report, "unknown_priority_class", f"$.customers[{k}].priority_class")
    assert issue.severity == "soft"
    assert issue.lineage == shop.customers[k].lineage
    assert len(issues_with(clean_report, "unknown_priority_class")) == 1


def test_validate_unknown_priority_class_reads_the_profile_map(shop: ErpSnapshot) -> None:
    report = validate_snapshot(shop, make_profile(extra_classes='"D-NEW" = 2\n'))
    assert issues_with(report, "unknown_priority_class") == []


def test_validate_sales_link_missing(shop: ErpSnapshot, profile: PlanningProfile) -> None:
    order = shop.production_orders[8]
    assert order.sales_links
    broken = order.sales_links[0].model_copy(update={"line_nbr": "99"})
    snap = replace_record(shop, "production_orders", 8, sales_links=(broken,))
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "sales_link_missing", "$.production_orders[8].sales_links[0]")
    assert issue.severity == "soft"
    assert issue.lineage == order.lineage


# --- due dates and promises ---------------------------------------------------------------------


def test_validate_no_due_date_on_clean_fixture_order(
    shop: ErpSnapshot, clean_report: ValidationReport
) -> None:
    k = next(n for n, o in enumerate(shop.production_orders) if o.due_date is None)
    issue = issue_at(clean_report, "no_due_date", f"$.production_orders[{k}]")
    assert issue.severity == "soft"
    assert issue.lineage == shop.production_orders[k].lineage


def test_validate_no_due_date_on_stock_order_without_due(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    k = next(n for n, o in enumerate(shop.production_orders) if not o.sales_links)
    snap = replace_record(shop, "production_orders", k, due_date=None)
    issue = issue_at(validate_snapshot(snap, profile), "no_due_date", f"$.production_orders[{k}]")
    assert issue.severity == "soft"


def test_validate_no_due_date_ignores_closed_orders(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    k = next(n for n, o in enumerate(shop.production_orders) if o.status == "closed")
    snap = replace_record(shop, "production_orders", k, due_date=None, sales_links=())
    report = validate_snapshot(snap, profile)
    assert [x for x in issues_with(report, "no_due_date") if x.path.endswith(f"[{k}]")] == []


def test_validate_past_due_no_promise(shop: ErpSnapshot, profile: PlanningProfile) -> None:
    k = next(n for n, o in enumerate(shop.production_orders) if not o.sales_links)
    snap = replace_record(shop, "production_orders", k, due_date=dt.date(2026, 10, 1))
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "past_due_no_promise", f"$.production_orders[{k}]")
    assert issue.severity == "soft"
    assert issue.lineage == shop.production_orders[k].lineage


def test_validate_past_due_with_a_promise_is_not_flagged(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    order = shop.production_orders[8]
    nbr = order.sales_links[0].order_nbr
    s = next(n for n, line in enumerate(shop.sales_order_lines) if line.order_nbr == nbr)
    snap = replace_record(shop, "production_orders", 8, due_date=dt.date(2026, 10, 1))
    snap = replace_record(snap, "sales_order_lines", s, promised_date=dt.date(2026, 10, 2))
    assert issues_with(validate_snapshot(snap, profile), "past_due_no_promise") == []


# --- purchase lines -----------------------------------------------------------------------------


def test_validate_po_past_due_on_open_line(
    shop: ErpSnapshot, clean_report: ValidationReport
) -> None:
    issue = issue_at(clean_report, "po_past_due", "$.purchase_lines[0].promised_date")
    assert issue.severity == "soft"
    assert issue.lineage == shop.purchase_lines[0].lineage


def test_validate_po_past_due_skips_closed_line(
    shop: ErpSnapshot, clean_report: ValidationReport
) -> None:
    assert shop.purchase_lines[3].status == "closed"
    assert [x.path for x in issues_with(clean_report, "po_past_due")] == [
        "$.purchase_lines[0].promised_date"
    ]


def test_validate_po_past_due_is_judged_against_as_of_not_the_clock(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    # PO-7001 promised 2026-10-09, PO-7003 promised 2026-10-13. A later as_of makes both past
    # due; an earlier one makes neither, whatever today's date is.
    later = redigest(
        shop.model_copy(update={"as_of": dt.datetime(2026, 10, 20, 5, tzinfo=PLANT_TZ)})
    )
    earlier = redigest(
        shop.model_copy(update={"as_of": dt.datetime(2026, 10, 1, 5, tzinfo=PLANT_TZ)})
    )
    later_paths = {x.path for x in issues_with(validate_snapshot(later, profile), "po_past_due")}
    assert later_paths == {
        "$.purchase_lines[0].promised_date",
        "$.purchase_lines[2].promised_date",
    }
    assert issues_with(validate_snapshot(earlier, profile), "po_past_due") == []


# --- calendars and the horizon ------------------------------------------------------------------


def _calendar_index(snap: ErpSnapshot, calendar_id: str) -> int:
    return next(n for n, c in enumerate(snap.calendars) if c.id == calendar_id)


def test_validate_calendar_no_working_time(shop: ErpSnapshot, profile: PlanningProfile) -> None:
    k = _calendar_index(shop, "24x5")
    snap = replace_record(shop, "calendars", k, week=(), exceptions=())
    report = validate_snapshot(snap, profile)
    issue = issue_at(report, "calendar_no_working_time", f"$.calendars[{k}]")
    assert issue.severity == "soft"
    assert issue.lineage == shop.calendars[k].lineage


def test_validate_calendar_working_time_is_measured_in_the_profile_horizon(
    shop: ErpSnapshot,
) -> None:
    # as_of is Monday 05:00; a Saturday-only calendar has no time in a 1-day horizon.
    k = _calendar_index(shop, "24x5")
    saturday = WeekdayWindows(weekday=5, windows=(TimeWindow(start_minute=0, end_minute=1440),))
    snap = replace_record(shop, "calendars", k, week=(saturday,), exceptions=())
    short = validate_snapshot(snap, make_profile(horizon_days=1))
    week = validate_snapshot(snap, make_profile(horizon_days=7))
    assert issue_at(short, "calendar_no_working_time", f"$.calendars[{k}]").severity == "soft"
    assert issues_with(week, "calendar_no_working_time") == []


def test_validate_calendar_exception_alone_gives_working_time(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    k = _calendar_index(shop, "24x5")
    exc = (
        shop.calendars[k]
        .exceptions[0]
        .model_copy(
            update={
                "date": shop.as_of.date() + dt.timedelta(days=2),
                "windows": (TimeWindow(start_minute=420, end_minute=930),),
            }
        )
    )
    snap = replace_record(shop, "calendars", k, week=(), exceptions=(exc,))
    assert issues_with(validate_snapshot(snap, profile), "calendar_no_working_time") == []


# --- source issues ------------------------------------------------------------------------------


def test_validate_source_issues_are_carried_with_their_own_severity(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    hard_lineage = Lineage(table="AMProdOper", key=("MO-9", "0010"))
    source = (
        SourceIssue(code="orphan_operation", severity="hard", lineage=hard_lineage, message="m"),
        SourceIssue(code="unmapped_status", severity="soft", lineage=None, message="m"),
    )
    snap = redigest(shop.model_copy(update={"source_issues": source}))
    report = validate_snapshot(snap, profile)
    hard = issue_at(report, "orphan_operation", "$.source_issues[0]")
    soft = issue_at(report, "unmapped_status", "$.source_issues[1]")
    assert (hard.severity, hard.lineage) == ("hard", hard_lineage)
    assert (soft.severity, soft.lineage) == ("soft", None)
    assert report.hard_count == 1


# --- all faults, purity -------------------------------------------------------------------------


def test_validate_reports_every_fault_not_only_the_first(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    snap = replace_record(shop, "machines", 0, work_center_id="ghost-wc")
    snap = replace_record(snap, "machines", 1, work_center_id="ghost-wc")
    snap = replace_record(snap, "work_centers", 0, calendar_id="ghost-cal")
    snap = replace_record(snap, "sales_order_lines", 0, customer_id="ghost-customer")
    snap = snap.model_copy(update={"digest": WRONG_DIGEST})
    report = validate_snapshot(snap, profile)
    issue_at(report, "unknown_work_center", "$.machines[0].work_center_id")
    issue_at(report, "unknown_work_center", "$.machines[1].work_center_id")
    issue_at(report, "unknown_calendar", "$.work_centers[0].calendar_id")
    issue_at(report, "unknown_customer", "$.sales_order_lines[0].customer_id")
    issue_at(report, "digest_mismatch", "$.digest")
    assert CLEAN_SOFT_CODES <= codes(report)
    assert report.hard_count >= 4


def test_validate_is_pure_two_calls_give_equal_reports(
    shop: ErpSnapshot, profile: PlanningProfile
) -> None:
    before = shop.model_dump()
    first = validate_snapshot(shop, profile)
    second = validate_snapshot(shop, profile)
    assert first == second
    assert shop.model_dump() == before


def test_validate_options_are_typed_option_records(shop: ErpSnapshot) -> None:
    # Guard on the fixture assumption the option tests rely on.
    i, j = form_op_with_explicit_machines(shop)
    op = shop.production_orders[i].operations[j]
    assert all(isinstance(o, OperationOption) for o in op.options)
    assert {o.machine_id for o in op.options} == {"form-1", "form-2", "form-3"}
