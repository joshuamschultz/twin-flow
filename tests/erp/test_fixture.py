"""Tests for the generated small-shop ERP fixture (T-198, COMP-124, REQ-178).

The generator lives in `tests/erp/fixtures/generate.py` and is imported as
`fixtures.generate` (pytest's default `prepend` import mode puts `tests/erp/` on
`sys.path`; `fixtures/` is a namespace package, so it merges with `tests/scheduling/fixtures/`,
which holds only JSON files).

Three groups:

* regeneration: the checked-in `small_shop.json` is byte-for-byte what `small_shop()` builds;
* referential cleanliness: the clean fixture has no fault that the SDD lists as a hard issue
  (checked here directly, before the COMP-112 validator exists);
* census: every scenario element that later builder and validator tests rely on is present.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import Counter
from collections.abc import Iterator
from zoneinfo import ZoneInfo

import pytest
from fixtures.generate import (
    SMALL_SHOP_JSON,
    dst_variant,
    scaled_shop,
    small_shop,
    to_json,
)

from twinflow.erp.digest import MAX_SNAPSHOT_BYTES, parse_snapshot, snapshot_digest
from twinflow.erp.schema import (
    ErpSnapshot,
    Machine,
    OperationOption,
    OrderOperation,
    Overtime,
    ProductionOrder,
    RoutingOperation,
    SalesOrderLine,
)

PLANT_TZ = ZoneInfo("America/Chicago")
OPEN_STATUSES = {"planned", "released", "in_process", "on_hold"}
REGENERATE_HINT = (
    "tests/erp/fixtures/small_shop.json is stale. Regenerate it with "
    "`python tests/erp/fixtures/generate.py` (calls fixtures.generate.main()) and review the diff."
)


@pytest.fixture(scope="module")
def shop() -> ErpSnapshot:
    return small_shop()


# --- helpers ------------------------------------------------------------------------------------


def all_operations(snap: ErpSnapshot) -> Iterator[RoutingOperation]:
    for routing in snap.routings:
        yield from routing.operations
    for order in snap.production_orders:
        yield from order.operations


def all_options(snap: ErpSnapshot) -> Iterator[OperationOption]:
    for op in all_operations(snap):
        yield from op.options


def order_ops(snap: ErpSnapshot) -> Iterator[tuple[ProductionOrder, OrderOperation]]:
    for order in snap.production_orders:
        for op in order.operations:
            yield order, op


def open_orders(snap: ErpSnapshot) -> list[ProductionOrder]:
    return [o for o in snap.production_orders if o.status in OPEN_STATUSES]


def remaining_demand(snap: ErpSnapshot) -> dict[str, list[float]]:
    """Open-order requirement quantities still to issue, per item (the COMP-115 demand)."""
    demand: dict[str, list[float]] = {}
    for order in open_orders(snap):
        for req in order.materials:
            qty = req.qty_required - req.qty_issued
            if qty > 0:
                demand.setdefault(req.item_id, []).append(qty)
    return demand


def supply(snap: ErpSnapshot, item_id: str) -> float:
    """Inventory plus open purchase quantity with a promised date (the COMP-115 supply)."""
    on_hand = sum(b.qty_available for b in snap.inventory if b.item_id == item_id)
    on_order = sum(
        p.qty_ordered - p.qty_received
        for p in snap.purchase_lines
        if p.item_id == item_id and p.status == "open" and p.promised_date is not None
    )
    return on_hand + on_order


def as_of_date(snap: ErpSnapshot) -> dt.date:
    return snap.as_of.astimezone(PLANT_TZ).date()


def machines_by_id(snap: ErpSnapshot) -> dict[str, Machine]:
    return {m.id: m for m in snap.machines}


def has_overtime_multiplier(overtime: Overtime | None, weekday: int, multiplier: float) -> bool:
    if overtime is None:
        return False
    return any(
        day.weekday == weekday and any(w.cost_multiplier == multiplier for w in day.windows)
        for day in overtime.week
    )


# --- regeneration and parsing ------------------------------------------------------------------


def test_small_shop_checked_in_json_matches_regenerated_bytes() -> None:
    expected = SMALL_SHOP_JSON.read_bytes()
    assert to_json(small_shop()).encode("utf-8") == expected, REGENERATE_HINT


def test_small_shop_is_deterministic_for_one_seed_and_differs_across_seeds() -> None:
    assert to_json(small_shop(seed=7)) == to_json(small_shop(seed=7))
    assert to_json(small_shop(seed=7)) != to_json(small_shop(seed=8))


def test_small_shop_json_parses_through_the_input_boundary() -> None:
    result = parse_snapshot(SMALL_SHOP_JSON.read_bytes())
    assert isinstance(result, ErpSnapshot), result
    assert result == small_shop()


def test_small_shop_carries_its_own_correct_digest(shop: ErpSnapshot) -> None:
    assert shop.digest is not None
    assert shop.digest == snapshot_digest(shop)


def test_small_shop_as_of_is_monday_0500_plant_local(shop: ErpSnapshot) -> None:
    local = shop.as_of.astimezone(PLANT_TZ)
    assert shop.plant_timezone == "America/Chicago"
    assert local.weekday() == 0
    assert (local.hour, local.minute, local.second) == (5, 0, 0)


def test_dst_variant_horizon_crosses_fall_back() -> None:
    snap = dst_variant()
    assert isinstance(parse_snapshot(to_json(snap).encode("utf-8")), ErpSnapshot)
    local = snap.as_of.astimezone(PLANT_TZ)
    assert local.date() == dt.date(2026, 10, 26)
    assert local.weekday() == 0
    horizon_end = (snap.as_of + dt.timedelta(days=14)).astimezone(PLANT_TZ)
    assert local.utcoffset() != horizon_end.utcoffset()
    assert local.date() < dt.date(2026, 11, 1) < horizon_end.date()


def test_dst_variant_shifts_calendar_exceptions_with_as_of() -> None:
    base, shifted = small_shop(), dst_variant()
    delta = as_of_date(shifted) - as_of_date(base)
    base_dates = sorted(e.date for c in base.calendars for e in c.exceptions)
    shifted_dates = sorted(e.date for c in shifted.calendars for e in c.exceptions)
    assert shifted_dates == [d + delta for d in base_dates]


# --- referential cleanliness (no SDD hard issue in the clean fixture) ---------------------------


def test_small_shop_ids_are_unique_per_collection(shop: ErpSnapshot) -> None:
    keyed = {
        "items": [i.id for i in shop.items],
        "boms": [b.id for b in shop.boms],
        "routings": [r.id for r in shop.routings],
        "work_centers": [w.id for w in shop.work_centers],
        "machines": [m.id for m in shop.machines],
        "calendars": [c.id for c in shop.calendars],
        "labor_pools": [p.id for p in shop.labor_pools],
        "production_orders": [o.id for o in shop.production_orders],
        "customers": [c.id for c in shop.customers],
        "vendors": [v.id for v in shop.vendors],
        "sales_order_lines": [
            (s.order_type, s.order_nbr, s.line_nbr) for s in shop.sales_order_lines
        ],
        "purchase_lines": [(p.order_type, p.order_nbr, p.line_nbr) for p in shop.purchase_lines],
    }
    for name, keys in keyed.items():
        duplicates = [k for k, n in Counter(keys).items() if n > 1]
        assert duplicates == [], name


def test_small_shop_every_reference_resolves(shop: ErpSnapshot) -> None:
    items = {i.id for i in shop.items}
    centers = {w.id for w in shop.work_centers}
    machines = machines_by_id(shop)
    calendars = {c.id for c in shop.calendars}
    pools = {p.id for p in shop.labor_pools}
    vendors = {v.id for v in shop.vendors}
    customers = {c.id for c in shop.customers}
    lines = {(s.order_type, s.order_nbr, str(s.line_nbr)) for s in shop.sales_order_lines}

    for op in all_operations(shop):
        assert op.work_center_id in centers
        if op.crew is not None:
            assert op.crew.labor_pool_id in pools
        if op.outside is not None:
            assert op.outside.vendor_id in vendors
    for option in all_options(shop):
        assert option.work_center_id in centers
        if option.machine_id is not None:
            assert machines[option.machine_id].work_center_id == option.work_center_id
    for owner in (*shop.work_centers, *shop.machines, *shop.labor_pools):
        assert owner.calendar_id is None or owner.calendar_id in calendars
    for machine in shop.machines:
        assert machine.work_center_id in centers
    for order in shop.production_orders:
        assert order.item_id in items
        assert {r.item_id for r in order.materials} <= items
        op_ids = {op.op_id for op in order.operations}
        assert {r.op_id for r in order.materials if r.op_id is not None} <= op_ids
        for link in order.sales_links:
            assert (link.order_type, link.order_nbr, link.line_nbr) in lines
        for op in order.operations:
            assert op.current_machine_id is None or op.current_machine_id in machines
    assert {r.item_id for r in shop.routings} <= items
    assert {b.item_id for b in shop.boms} | {
        line.item_id for b in shop.boms for line in b.lines
    } <= items
    assert {s.item_id for s in shop.sales_order_lines} <= items
    assert {s.customer_id for s in shop.sales_order_lines} <= customers
    assert {p.item_id for p in shop.purchase_lines} <= items
    assert {p.vendor_id for p in shop.purchase_lines} <= vendors
    assert {b.item_id for b in shop.inventory} <= items


def test_small_shop_every_in_plant_operation_has_an_active_option(shop: ErpSnapshot) -> None:
    machines = machines_by_id(shop)
    active_centers = {m.work_center_id for m in shop.machines if m.active}
    for op in all_operations(shop):
        if op.outside is not None:
            assert op.options == ()
            continue
        assert any(
            machines[o.machine_id].active if o.machine_id else o.work_center_id in active_centers
            for o in op.options
        ), op.lineage


def test_small_shop_outside_operations_are_never_first_or_last(shop: ErpSnapshot) -> None:
    routes = [r.operations for r in shop.routings] + [o.operations for o in shop.production_orders]
    for ops in routes:
        assert ops[0].outside is None
        assert ops[-1].outside is None


def test_small_shop_one_batch_capacity_per_work_center(shop: ErpSnapshot) -> None:
    capacities: dict[str, set[tuple[float, str]]] = {}
    for op in all_operations(shop):
        if op.batch is not None:
            capacities.setdefault(op.work_center_id, set()).add((op.batch.capacity, op.batch.unit))
    assert capacities
    assert all(len(caps) == 1 for caps in capacities.values())


def test_small_shop_quantities_are_consistent(shop: ErpSnapshot) -> None:
    for order, op in order_ops(shop):
        assert op.qty_to_produce == order.quantity
        assert op.qty_complete + op.qty_scrapped + op.qty_remaining == pytest.approx(
            op.qty_to_produce
        )
    for order in shop.production_orders:
        sequences = [op.sequence for op in order.operations]
        assert sequences == sorted(sequences)
        assert 3 <= len(order.operations) <= 6


# --- census: every planned scenario element is present -----------------------------------------


def test_census_order_status_mix(shop: ErpSnapshot) -> None:
    counts = Counter(o.status for o in shop.production_orders)
    assert len(shop.production_orders) == 30
    assert counts == {
        "planned": 8,
        "released": 8,
        "in_process": 7,
        "on_hold": 2,
        "complete": 3,
        "closed": 2,
    }


def test_census_two_rush_orders_both_open(shop: ErpSnapshot) -> None:
    rush = [o for o in shop.production_orders if o.rush]
    assert len(rush) == 2
    assert all(o.status in OPEN_STATUSES and not o.hold for o in rush)


def test_census_held_by_status_and_by_flag(shop: ErpSnapshot) -> None:
    assert any(o.status == "on_hold" for o in shop.production_orders)
    flagged = [o for o in shop.production_orders if o.hold]
    assert len(flagged) == 1
    assert flagged[0].status == "released"


def test_census_closed_orders_are_fully_complete(shop: ErpSnapshot) -> None:
    for order in shop.production_orders:
        if order.status in {"complete", "closed"}:
            assert all(op.status == "complete" for op in order.operations)
            assert all(op.qty_remaining == 0 for op in order.operations)


def test_census_outside_plating_operation_in_the_middle_with_3_day_lead(
    shop: ErpSnapshot,
) -> None:
    vendors = {v.id: v for v in shop.vendors}
    middles = [
        op
        for order in shop.production_orders
        for op in order.operations[1:-1]
        if op.outside is not None
    ]
    assert middles
    assert {op.outside.lead_seconds for op in middles if op.outside} == {259_200.0}
    assert {op.outside.vendor_id for op in middles if op.outside} <= vendors.keys()
    assert any(op.outside is not None for r in shop.routings for op in r.operations[1:-1])


def test_census_exactly_one_uncovered_wire_shortage(shop: ErpSnapshot) -> None:
    demand = remaining_demand(shop)
    short = [item for item, qtys in demand.items() if sum(qtys) > supply(shop, item)]
    assert len(short) == 1
    item = next(i for i in shop.items if i.id == short[0])
    assert "wire_family" in item.attributes
    # Supply misses the total by less than the smallest requirement, so whatever order
    # COMP-115 allocates in, exactly one requirement stays uncovered.
    qtys = demand[short[0]]
    assert sum(qtys) - supply(shop, short[0]) < min(qtys)


def test_census_shortage_item_has_a_past_due_purchase_line(shop: ErpSnapshot) -> None:
    today = as_of_date(shop)
    past_due = [
        p
        for p in shop.purchase_lines
        if p.status == "open" and p.promised_date is not None and p.promised_date < today
    ]
    assert len(past_due) == 1
    assert past_due[0].qty_ordered > past_due[0].qty_received


def test_census_late_purchase_line_after_every_due_date_of_its_item(shop: ErpSnapshot) -> None:
    def due_dates(item_id: str) -> list[dt.date]:
        return [
            o.due_date
            for o in open_orders(shop)
            if o.due_date is not None and any(r.item_id == item_id for r in o.materials)
        ]

    today = as_of_date(shop)
    late = [
        p
        for p in shop.purchase_lines
        if p.status == "open"
        and p.promised_date is not None
        and p.promised_date >= today
        and due_dates(p.item_id)
        and p.promised_date > max(due_dates(p.item_id))
    ]
    assert len(late) == 1
    item = late[0].item_id
    on_hand = sum(b.qty_available for b in shop.inventory if b.item_id == item)
    assert on_hand < sum(remaining_demand(shop)[item]) <= supply(shop, item)


def test_census_purchase_receipts_partial_and_closed(shop: ErpSnapshot) -> None:
    assert any(p.status == "closed" and p.receipts for p in shop.purchase_lines)
    partial = [p for p in shop.purchase_lines if p.status == "open" and p.qty_received > 0]
    assert partial
    for line in shop.purchase_lines:
        assert sum(r.qty for r in line.receipts) == pytest.approx(line.qty_received)
        assert all(r.received_at < shop.as_of for r in line.receipts)


def test_census_materials_with_and_without_op_id(shop: ErpSnapshot) -> None:
    reqs = [r for o in shop.production_orders for r in o.materials]
    assert any(r.op_id is not None for r in reqs)
    assert any(r.op_id is None for r in reqs)


def test_census_night_shift_crosses_midnight(shop: ErpSnapshot) -> None:
    assert any(
        w.end_minute <= w.start_minute
        for c in shop.calendars
        for day in c.week
        for w in day.windows
    )


def test_census_three_calendars_with_holiday_and_short_day(shop: ErpSnapshot) -> None:
    assert len(shop.calendars) == 3
    exceptions = [e for c in shop.calendars for e in c.exceptions]
    assert any(e.windows == () for e in exceptions)
    assert any(
        e.windows and sum((w.end_minute - w.start_minute) % 1440 for w in e.windows) < 8 * 60
        for e in exceptions
    )
    saturday = [c for c in shop.calendars if any(d.weekday == 5 for d in c.week)]
    full_days = [
        c
        for c in shop.calendars
        if {d.weekday for d in c.week} == {0, 1, 2, 3, 4}
        and all(
            any(w.start_minute == 0 and w.end_minute == 1440 for w in d.windows) for d in c.week
        )
    ]
    assert saturday and full_days


def test_census_customer_priority_classes(shop: ErpSnapshot) -> None:
    classes = Counter(c.priority_class for c in shop.customers)
    assert classes["A-KEY"] == classes["B-CORE"] == classes["C-SPOT"] == 3
    unmapped = [c for c in shop.customers if c.priority_class == "D-NEW"]
    assert len(unmapped) == 1
    linked = [
        s
        for s in shop.sales_order_lines
        if s.customer_id == unmapped[0].id
        and any(
            (link.order_nbr, link.line_nbr) == (s.order_nbr, str(s.line_nbr))
            for o in open_orders(shop)
            for link in o.sales_links
        )
    ]
    assert len(linked) == 1


def test_census_one_stock_order_with_its_own_due_date(shop: ErpSnapshot) -> None:
    stock = [o for o in shop.production_orders if not o.sales_links]
    assert len(stock) == 1
    assert stock[0].status in OPEN_STATUSES
    assert stock[0].due_date is not None


def test_census_due_date_sources(shop: ErpSnapshot) -> None:
    lines = {(s.order_nbr, str(s.line_nbr)): s for s in shop.sales_order_lines}

    def line_of(order: ProductionOrder) -> SalesOrderLine:
        link = order.sales_links[0]
        return lines[(link.order_nbr, link.line_nbr)]

    linked = [o for o in open_orders(shop) if o.sales_links]
    requested_only = [
        o
        for o in linked
        if line_of(o).promised_date is None and line_of(o).requested_date is not None
    ]
    no_due = [
        o
        for o in linked
        if line_of(o).promised_date is None
        and line_of(o).requested_date is None
        and o.due_date is None
    ]
    assert len(requested_only) == 1
    assert len(no_due) == 1
    today = as_of_date(shop)
    for line in shop.sales_order_lines:
        for date in (line.requested_date, line.promised_date):
            assert date is None or date >= today


def test_census_per_machine_option_times_differ(shop: ErpSnapshot) -> None:
    def differing(op: RoutingOperation) -> bool:
        machine_opts = [o for o in op.options if o.machine_id is not None]
        return len({o.run_seconds_per_unit for o in machine_opts}) >= 2

    assert any(differing(op) for op in all_operations(shop))
    assert any(len({o.preference for o in op.options}) >= 2 for op in all_operations(shop))
    assert any(
        o.machine_id is None
        for op in all_operations(shop)
        if op.outside is None
        for o in op.options
    )


def test_census_setup_params_carry_wire_attributes(shop: ErpSnapshot) -> None:
    items = {i.id: i for i in shop.items}
    families = {i.attributes["wire_family"] for i in shop.items if "wire_family" in i.attributes}
    diameters = {
        i.attributes["wire_diameter_mm"] for i in shop.items if "wire_diameter_mm" in i.attributes
    }
    assert len(families) == 3
    assert len(diameters) == 3
    for order, op in order_ops(shop):
        if "wire_family" in op.setup_params:
            assert op.setup_params["wire_family"] == items[order.item_id].attributes["wire_family"]
    assert any("wire_diameter_mm" in op.setup_params for op in all_operations(shop))


def test_census_batch_and_crew_operations(shop: ErpSnapshot) -> None:
    pools = {p.id: p for p in shop.labor_pools}
    batch_ops = [op for op in all_operations(shop) if op.batch is not None]
    crew_ops = [op for op in all_operations(shop) if op.crew is not None]
    assert batch_ops and {op.work_center_id for op in batch_ops} == {"oven"}
    assert crew_ops and {op.work_center_id for op in crew_ops} == {"inspect"}
    assert all(pools[op.crew.labor_pool_id].size == 2 for op in crew_ops if op.crew)


def test_census_in_process_operation_with_and_without_current_machine(
    shop: ErpSnapshot,
) -> None:
    in_process = [op for _, op in order_ops(shop) if op.status == "in_process"]
    with_machine = [op for op in in_process if op.current_machine_id is not None]
    without = [op for op in in_process if op.current_machine_id is None]
    assert with_machine
    assert len(without) == 1
    for op in in_process:
        assert 0 < op.qty_complete < op.qty_to_produce
        assert 0 < op.qty_remaining < op.qty_to_produce
        assert op.actual_start is not None and op.actual_start < shop.as_of
    machines = machines_by_id(shop)
    for op in with_machine:
        current = machines[op.current_machine_id or ""]
        assert current.active and current.work_center_id == op.work_center_id


def test_census_work_centers_and_machines(shop: ErpSnapshot) -> None:
    per_center = Counter(m.work_center_id for m in shop.machines)
    assert per_center == {"coil": 2, "form": 3, "grind": 2, "oven": 1, "inspect": 1, "pack": 1}
    assert len(shop.work_centers) == 6
    assert [m.efficiency for m in shop.machines].count(0.9) == 1
    inactive = [m for m in shop.machines if not m.active]
    assert len(inactive) == 1
    assert any(o.machine_id == inactive[0].id for o in all_options(shop))


def test_census_overtime_on_work_center_and_machine_override(shop: ErpSnapshot) -> None:
    saturday = 5
    centers = [w for w in shop.work_centers if has_overtime_multiplier(w.overtime, saturday, 1.5)]
    assert len(centers) == 1
    overriders = [
        m
        for m in shop.machines
        if has_overtime_multiplier(m.overtime, saturday, 2.0) and m.work_center_id == centers[0].id
    ]
    assert len(overriders) == 1


def test_census_every_record_carries_generic_lineage(shop: ErpSnapshot) -> None:
    tables = {
        r.lineage.table
        for coll in (
            shop.items,
            shop.boms,
            shop.routings,
            shop.work_centers,
            shop.machines,
            shop.calendars,
            shop.labor_pools,
            shop.production_orders,
            shop.sales_order_lines,
            shop.customers,
            shop.vendors,
            shop.purchase_lines,
            shop.inventory,
        )
        for r in coll
    }
    assert "production_order" in tables
    order = shop.production_orders[0]
    assert order.lineage.key == (order.id,)
    assert all(op.lineage.key[0] == order.id for op in order.operations)


# --- scaled shop -------------------------------------------------------------------------------


@pytest.mark.slow
def test_scaled_shop_size_and_generation_time() -> None:
    started = time.perf_counter()
    snap = scaled_shop()
    elapsed = time.perf_counter() - started
    ops = sum(len(o.operations) for o in snap.production_orders)
    size = len(to_json(snap, indent=None).encode("utf-8"))
    print(f"scaled_shop: {elapsed:.2f}s, {ops} ops, {len(snap.machines)} machines, {size} bytes")
    assert len(snap.production_orders) == 600
    assert 2_500 <= ops <= 3_500
    assert 70 <= len(snap.machines) <= 90
    assert size < MAX_SNAPSHOT_BYTES
    assert elapsed < 2.0
    due = [o.due_date for o in snap.production_orders if o.due_date is not None]
    assert (max(due) - as_of_date(snap)).days >= 49  # due dates span about 8 weeks


@pytest.mark.slow
def test_scaled_shop_parses_and_keeps_the_scenario_elements() -> None:
    snap = scaled_shop()
    assert isinstance(parse_snapshot(to_json(snap, indent=None).encode("utf-8")), ErpSnapshot)
    assert sum(o.rush for o in snap.production_orders) == 2
    demand = remaining_demand(snap)
    assert [i for i, q in demand.items() if sum(q) > supply(snap, i)] != []
