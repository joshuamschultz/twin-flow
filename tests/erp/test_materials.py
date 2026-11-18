"""Material readiness netting, hand-computed cases (T-201, COMP-115, D-122).

Requirement: REQ-156.

Contract pinned here (the implementation follows these tests):

    from twinflow.erp.materials import MaterialPlan, material_ready_times

    material_ready_times(
        snapshot: ErpSnapshot, jobs_order: Sequence[str], policy: ErpPolicy
    ) -> MaterialPlan

* `jobs_order` is the provisional priority order of OPEN production order ids, already
  sorted by the builder by (due seconds, weight class descending, order id). Materials does
  not re-sort orders: it walks `jobs_order` as given. Orders absent from `jobs_order` create
  no demand (closed, complete, cancelled orders are filtered out by the builder).
* Inside one order, requirements are allocated by the consuming operation's `sequence`
  (the integer field, not the `op_id` string); a requirement with no `op_id` (a job-release
  requirement) is allocated before every operation of its order.
* Supply per item: the sum of `inventory.qty_available` over all sites at time 0.0, plus each
  `status == "open"` purchase line's `max(0, qty_ordered - qty_received)` at its promised
  date's START of day (00:00 local in `plant_timezone`), as real elapsed seconds from
  `as_of`. A promised start of day before `as_of` is supply at
  `policy.po_past_due_grace_seconds` with a soft `po_past_due` issue. An open line with no
  promised date is not supply and gives a soft `po_no_promise` issue. Closed lines are
  ignored entirely (no supply, no issue).
* Demand: each requirement with `qty_required - qty_issued > 0`, of that remaining amount.
  A fully issued requirement produces nothing (no demand, no ready entry, no issue).
* Ready time: the earliest supply time at which cumulative supply of the item covers
  cumulative demand of the item up to and including that requirement.
* `MaterialPlan.ready`: `dict[(order_id, op_id | None), float]`, seconds from `as_of`.
  `(order_id, None)` is the job-release constraint.
* Uncovered requirement: soft `material_short`, path `$.production_orders[i].materials[j]`,
  lineage = the requirement's lineage, no `ready` entry. With `shortage_policy = "hold"`
  the order id is also in `MaterialPlan.held_orders`.
* Purchase line issues point at the field that caused them (one path rule with the
  validator): `po_past_due` and `po_no_promise` carry `$.purchase_lines[k].promised_date`
  and the line's lineage.
* `MaterialPlan.held_orders` is a `frozenset[str]`; `MaterialPlan.issues` a
  `tuple[ErpIssue, ...]`.
* Determinism: shuffling every snapshot collection leaves `ready` and `held_orders`
  identical and the issues identical up to their index-bearing `path` (each path still
  points at the record whose lineage the issue carries).
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from _factories import (
    AS_OF,
    PLANT_TZ,
    Data,
    inventory_balance,
    lineage,
    order_operation,
    production_order,
    purchase_line,
    snapshot,
)

from twinflow.erp.issues import ErpIssue
from twinflow.erp.materials import MaterialPlan, material_ready_times
from twinflow.erp.profile import ErpPolicy
from twinflow.erp.schema import ErpSnapshot

# 2026-10-07 00:00 CDT minus 2026-10-05 05:00 CDT = 43 hours.
OCT_7_SECONDS = 43 * 3600.0
# 2026-10-09 00:00 CDT minus as_of = 91 hours.
OCT_9_SECONDS = 91 * 3600.0
GRACE = 3_600.0

# --- builders --------------------------------------------------------------------------------


def req(
    item: str,
    qty: float,
    *,
    order_id: str,
    op_id: str | None = None,
    issued: float = 0.0,
) -> Data:
    """A material requirement with a lineage unique to (order, item, op)."""
    data: Data = {
        "item_id": item,
        "qty_required": qty,
        "qty_issued": issued,
        "lineage": lineage("Material", order_id, item, op_id or "-"),
    }
    if op_id is not None:
        data["op_id"] = op_id
    return data


def order(
    order_id: str,
    materials: Sequence[Data] = (),
    ops: Sequence[tuple[str, int]] = (("10", 10),),
) -> Data:
    data = production_order()
    operations = []
    for op_id, sequence in ops:
        operation = order_operation(op_id, sequence)
        operation["lineage"] = lineage("OrderOp", order_id, op_id)
        operations.append(operation)
    data.update(
        id=order_id,
        operations=operations,
        materials=list(materials),
        lineage=lineage("ProdOrder", order_id),
    )
    return data


def po(
    nbr: str,
    item: str,
    ordered: float,
    *,
    received: float = 0.0,
    promised: str | None = None,
    status: str = "open",
) -> Data:
    data = purchase_line()
    data.update(
        order_nbr=nbr,
        item_id=item,
        qty_ordered=ordered,
        qty_received=received,
        status=status,
        lineage=lineage("POLine", nbr, "1"),
    )
    if promised is not None:
        data["promised_date"] = promised
    return data


def inv(item: str, qty: float, site: str | None = None) -> Data:
    data = inventory_balance()
    data.update(item_id=item, qty_available=qty, lineage=lineage("Inventory", item, site or "-"))
    if site is not None:
        data["site_id"] = site
    return data


def snap(
    orders: Sequence[Data] = (),
    purchases: Sequence[Data] = (),
    inventory: Sequence[Data] = (),
) -> ErpSnapshot:
    data = snapshot()
    data.update(
        production_orders=list(orders),
        purchase_lines=list(purchases),
        inventory=list(inventory),
    )
    return ErpSnapshot.model_validate(data)


def start_of_day_seconds(day: str) -> float:
    """Real elapsed seconds from AS_OF to 00:00 of `day` in the plant time zone (oracle)."""
    local = datetime.combine(date.fromisoformat(day), datetime.min.time(), ZoneInfo(PLANT_TZ))
    return (local - datetime.fromisoformat(AS_OF)).total_seconds()


def run(
    snapshot_: ErpSnapshot,
    jobs_order: Sequence[str],
    policy: ErpPolicy | None = None,
) -> MaterialPlan:
    return material_ready_times(snapshot_, jobs_order, policy or ErpPolicy())


def codes(plan: MaterialPlan) -> list[str]:
    return sorted(issue.code for issue in plan.issues)


def issues_with(plan: MaterialPlan, code: str) -> list[ErpIssue]:
    return [issue for issue in plan.issues if issue.code == code]


_PATH_TOKEN = re.compile(r"\.([a-z_]+)|\[(\d+)\]")


def record_path(path: str) -> str:
    """The path of the record that owns `path`: a field path drops its `.promised_date`."""
    return re.sub(r"\.promised_date$", "", path)


def resolve(snapshot_: ErpSnapshot, path: str) -> Any:
    """Walk a `$.a[1].b[2]` JSON path through the snapshot's JSON form."""
    assert path.startswith("$"), path
    node: Any = snapshot_.model_dump(mode="json")
    rest = path[1:]
    consumed = 0
    for match in _PATH_TOKEN.finditer(rest):
        assert match.start() == consumed, f"unparseable path {path!r}"
        consumed = match.end()
        key, index = match.groups()
        node = node[key] if key is not None else node[int(index)]
    assert consumed == len(rest), f"unparseable path {path!r}"
    return node


# --- shape -----------------------------------------------------------------------------------


def test_material_ready_times_empty_snapshot_returns_empty_plan() -> None:
    plan = run(snap(), [])

    assert isinstance(plan, MaterialPlan)
    assert plan.ready == {}
    assert plan.held_orders == frozenset()
    assert isinstance(plan.held_orders, frozenset)
    assert plan.issues == ()
    assert isinstance(plan.issues, tuple)


# --- supply covers demand ---------------------------------------------------------------------


def test_material_ready_times_inventory_covers_all_ready_at_zero() -> None:
    orders = [
        order("A", [req("i2", 4.0, order_id="A", op_id="10")]),
        order("B", [req("i2", 6.0, order_id="B", op_id="10")]),
    ]

    plan = run(snap(orders, inventory=[inv("i2", 10.0)]), ["A", "B"])

    assert plan.ready == {("A", "10"): 0.0, ("B", "10"): 0.0}
    assert plan.issues == ()
    assert plan.held_orders == frozenset()


def test_material_ready_times_inventory_summed_across_sites() -> None:
    orders = [order("A", [req("i2", 10.0, order_id="A", op_id="10")])]
    stock = [inv("i2", 4.0, "MAIN"), inv("i2", 6.0, "EAST")]

    plan = run(snap(orders, inventory=stock), ["A"])

    assert plan.ready == {("A", "10"): 0.0}
    assert plan.issues == ()


def test_material_ready_times_po_covers_second_requirement_at_promised_start_of_day() -> None:
    orders = [
        order("A", [req("i2", 5.0, order_id="A", op_id="10")]),
        order("B", [req("i2", 5.0, order_id="B", op_id="10")]),
    ]
    supply = [po("P1", "i2", 5.0, promised="2026-10-07")]

    plan = run(snap(orders, supply, [inv("i2", 5.0)]), ["A", "B"])

    assert plan.ready == {("A", "10"): 0.0, ("B", "10"): OCT_7_SECONDS}
    assert OCT_7_SECONDS == start_of_day_seconds("2026-10-07")
    assert plan.issues == ()


def test_material_ready_times_promised_date_across_dst_uses_real_elapsed_seconds() -> None:
    # DST ends 2026-11-01 in America/Chicago: 00:00 on 11-02 is CST (-06:00), one hour
    # later in real time than a naive -05:00 subtraction would give.
    orders = [order("A", [req("i2", 5.0, order_id="A", op_id="10")])]
    supply = [po("P1", "i2", 5.0, promised="2026-11-02")]

    plan = run(snap(orders, supply), ["A"])

    expected = 28 * 86_400.0 - 4 * 3_600.0
    assert expected == start_of_day_seconds("2026-11-02")
    assert plan.ready == {("A", "10"): expected}


def test_material_ready_times_ready_is_earliest_time_cumulative_supply_covers() -> None:
    # 15 needed: 5 on hand, +5 on 10-07 (cum 10, short), +10 on 10-09 (cum 20, covered).
    orders = [order("A", [req("i2", 15.0, order_id="A", op_id="10")])]
    supply = [
        po("P2", "i2", 10.0, promised="2026-10-09"),
        po("P1", "i2", 5.0, promised="2026-10-07"),
    ]

    plan = run(snap(orders, supply, [inv("i2", 5.0)]), ["A"])

    assert plan.ready == {("A", "10"): OCT_9_SECONDS}


def test_material_ready_times_supply_of_other_item_does_not_cover() -> None:
    orders = [
        order(
            "A",
            [
                req("i2", 5.0, order_id="A", op_id="10"),
                req("i3", 5.0, order_id="A", op_id="20"),
            ],
            ops=(("10", 10), ("20", 20)),
        )
    ]
    supply = [po("P1", "i3", 5.0, promised="2026-10-07")]

    plan = run(snap(orders, supply, [inv("i2", 5.0)]), ["A"])

    assert plan.ready == {("A", "10"): 0.0, ("A", "20"): OCT_7_SECONDS}
    assert plan.issues == ()


# --- allocation order -------------------------------------------------------------------------


def test_material_ready_times_jobs_order_decides_who_waits() -> None:
    orders = [
        order("A", [req("i2", 10.0, order_id="A", op_id="10")]),
        order("B", [req("i2", 10.0, order_id="B", op_id="10")]),
    ]
    snapshot_ = snap(orders, [po("P1", "i2", 10.0, promised="2026-10-07")], [inv("i2", 10.0)])

    a_first = run(snapshot_, ["A", "B"])
    b_first = run(snapshot_, ["B", "A"])  # as if B's due date moved ahead of A's

    assert a_first.ready == {("A", "10"): 0.0, ("B", "10"): OCT_7_SECONDS}
    assert b_first.ready == {("B", "10"): 0.0, ("A", "10"): OCT_7_SECONDS}


def test_material_ready_times_jobs_order_not_resorted_by_order_id() -> None:
    # "Z" precedes "A" in jobs_order; materials must not fall back to id order.
    orders = [
        order("A", [req("i2", 10.0, order_id="A", op_id="10")]),
        order("Z", [req("i2", 10.0, order_id="Z", op_id="10")]),
    ]
    snapshot_ = snap(orders, [po("P1", "i2", 10.0, promised="2026-10-07")], [inv("i2", 10.0)])

    plan = run(snapshot_, ["Z", "A"])

    assert plan.ready == {("Z", "10"): 0.0, ("A", "10"): OCT_7_SECONDS}


def test_material_ready_times_within_order_allocates_by_op_sequence_not_list_or_id() -> None:
    # op "100" (sequence 100) is listed first and sorts first as a string; op "20"
    # (sequence 20) runs first on the floor and must get the stock on hand.
    orders = [
        order(
            "A",
            [
                req("i2", 5.0, order_id="A", op_id="100"),
                req("i2", 5.0, order_id="A", op_id="20"),
            ],
            ops=(("100", 100), ("20", 20)),
        )
    ]
    supply = [po("P1", "i2", 5.0, promised="2026-10-07")]

    plan = run(snap(orders, supply, [inv("i2", 5.0)]), ["A"])

    assert plan.ready == {("A", "20"): 0.0, ("A", "100"): OCT_7_SECONDS}


def test_material_ready_times_release_requirement_allocated_before_operations() -> None:
    orders = [
        order(
            "A",
            [
                req("i2", 5.0, order_id="A", op_id="10"),
                req("i2", 5.0, order_id="A"),
            ],
        )
    ]
    supply = [po("P1", "i2", 5.0, promised="2026-10-07")]

    plan = run(snap(orders, supply, [inv("i2", 5.0)]), ["A"])

    assert plan.ready == {("A", None): 0.0, ("A", "10"): OCT_7_SECONDS}


def test_material_ready_times_orders_outside_jobs_order_create_no_demand() -> None:
    orders = [
        order("CLOSED", [req("i2", 10.0, order_id="CLOSED", op_id="10")]),
        order("A", [req("i2", 10.0, order_id="A", op_id="10")]),
    ]

    plan = run(snap(orders, inventory=[inv("i2", 10.0)]), ["A"])

    assert plan.ready == {("A", "10"): 0.0}
    assert plan.issues == ()


# --- requirement keys and issued quantity ----------------------------------------------------


def test_material_ready_times_requirement_with_op_id_keys_order_and_op() -> None:
    orders = [order("A", [req("i2", 1.0, order_id="A", op_id="10")])]

    plan = run(snap(orders, inventory=[inv("i2", 1.0)]), ["A"])

    assert list(plan.ready) == [("A", "10")]


def test_material_ready_times_two_items_on_one_op_wait_for_the_later_cover() -> None:
    """An op needing two items is ready when BOTH are covered, whatever the list order."""
    late_first = [
        req("i1", 1.0, order_id="A", op_id="10"),
        req("i2", 1.0, order_id="A", op_id="10"),
    ]
    supply = [po("P1", "i1", 1.0, promised="2026-10-09")]
    stock = [inv("i2", 1.0)]

    plan = run(snap([order("A", late_first)], supply, stock), ["A"])
    flipped = run(snap([order("A", late_first[::-1])], supply, stock), ["A"])

    assert plan.ready == {("A", "10"): OCT_9_SECONDS}
    assert flipped.ready == plan.ready


def test_material_ready_times_two_release_items_wait_for_the_later_cover() -> None:
    materials = [req("i1", 1.0, order_id="A"), req("i2", 1.0, order_id="A")]
    supply = [po("P1", "i1", 1.0, promised="2026-10-07")]

    plan = run(snap([order("A", materials)], supply, [inv("i2", 1.0)]), ["A"])

    assert plan.ready == {("A", None): OCT_7_SECONDS}


def test_material_ready_times_requirement_without_op_id_keys_order_and_none() -> None:
    orders = [order("A", [req("i2", 1.0, order_id="A")])]

    plan = run(snap(orders, inventory=[inv("i2", 1.0)]), ["A"])

    assert list(plan.ready) == [("A", None)]


def test_material_ready_times_fully_issued_requirement_produces_nothing() -> None:
    # A's requirement is fully issued: no entry, no issue, and it consumes no stock, so B
    # still gets the 5 on hand.
    orders = [
        order("A", [req("i2", 5.0, order_id="A", op_id="10", issued=5.0)]),
        order("B", [req("i2", 5.0, order_id="B", op_id="10")]),
    ]

    plan = run(snap(orders, inventory=[inv("i2", 5.0)]), ["A", "B"])

    assert plan.ready == {("B", "10"): 0.0}
    assert plan.issues == ()


def test_material_ready_times_over_issued_requirement_produces_nothing() -> None:
    orders = [order("A", [req("i2", 5.0, order_id="A", op_id="10", issued=7.0)])]

    plan = run(snap(orders), ["A"])

    assert plan.ready == {}
    assert plan.issues == ()


def test_material_ready_times_partial_issue_nets_only_remaining_quantity() -> None:
    orders = [order("A", [req("i2", 10.0, order_id="A", op_id="10", issued=6.0)])]

    plan = run(snap(orders, inventory=[inv("i2", 4.0)]), ["A"])

    assert plan.ready == {("A", "10"): 0.0}
    assert plan.issues == ()


# --- purchase lines ---------------------------------------------------------------------------


def test_material_ready_times_partial_receipt_reduces_open_po_qty() -> None:
    # 10 ordered, 6 received: only 4 still to come. 5 needed -> short.
    orders = [order("A", [req("i2", 5.0, order_id="A", op_id="10")])]
    short = run(snap(orders, [po("P1", "i2", 10.0, received=6.0, promised="2026-10-07")]), ["A"])
    # 4 needed -> covered on the promise date.
    fits = [order("A", [req("i2", 4.0, order_id="A", op_id="10")])]
    covered = run(snap(fits, [po("P1", "i2", 10.0, received=6.0, promised="2026-10-07")]), ["A"])

    assert short.ready == {}
    assert codes(short) == ["material_short"]
    assert covered.ready == {("A", "10"): OCT_7_SECONDS}
    assert covered.issues == ()


def test_material_ready_times_over_received_po_is_not_negative_supply() -> None:
    # 3 on hand, P1 over-received (3 ordered, 5 received) adds 0 (not -2) on 10-07, P2 adds 2
    # on 10-09: cumulative 3, 3, 5 covers 5 on 10-09. Negative supply would leave it short.
    orders = [order("A", [req("i2", 5.0, order_id="A", op_id="10")])]
    supply = [
        po("P1", "i2", 3.0, received=5.0, promised="2026-10-07"),
        po("P2", "i2", 2.0, promised="2026-10-09"),
    ]

    plan = run(snap(orders, supply, [inv("i2", 3.0)]), ["A"])

    assert plan.ready == {("A", "10"): OCT_9_SECONDS}
    assert plan.issues == ()


def test_material_ready_times_past_due_po_ready_at_grace_with_po_past_due() -> None:
    orders = [order("A", [req("i2", 5.0, order_id="A", op_id="10")])]
    supply = [po("P0", "i2", 9.0), po("P1", "i2", 5.0, promised="2026-10-01")]
    supply[0]["status"] = "closed"
    snapshot_ = snap(orders, supply)

    plan = run(snapshot_, ["A"], ErpPolicy(po_past_due_grace_seconds=GRACE))

    assert plan.ready == {("A", "10"): GRACE}
    [issue] = plan.issues
    assert issue.code == "po_past_due"
    assert issue.severity == "soft"
    assert issue.path == "$.purchase_lines[1].promised_date"
    assert issue.lineage == snapshot_.purchase_lines[1].lineage


def test_material_ready_times_past_due_po_uses_default_grace() -> None:
    orders = [order("A", [req("i2", 5.0, order_id="A", op_id="10")])]

    plan = run(snap(orders, [po("P1", "i2", 5.0, promised="2026-10-01")]), ["A"])

    assert plan.ready == {("A", "10"): 86_400.0}


def test_material_ready_times_no_promise_po_is_not_supply() -> None:
    orders = [order("A", [req("i2", 5.0, order_id="A", op_id="10")])]
    snapshot_ = snap(orders, [po("P1", "i2", 5.0)])

    plan = run(snapshot_, ["A"])

    assert plan.ready == {}
    assert codes(plan) == ["material_short", "po_no_promise"]
    [no_promise] = issues_with(plan, "po_no_promise")
    assert no_promise.severity == "soft"
    assert no_promise.path == "$.purchase_lines[0].promised_date"
    assert no_promise.lineage == snapshot_.purchase_lines[0].lineage
    [short] = issues_with(plan, "material_short")
    assert short.lineage == snapshot_.production_orders[0].materials[0].lineage


def test_material_ready_times_closed_po_ignored_without_issue() -> None:
    orders = [order("A", [req("i2", 5.0, order_id="A", op_id="10")])]
    supply = [
        po("P1", "i2", 5.0, promised="2026-10-07", status="closed"),
        po("P2", "i2", 5.0, status="closed"),  # no promise, but closed: no po_no_promise
        po("P3", "i2", 5.0, promised="2026-10-01", status="closed"),  # no po_past_due
    ]

    plan = run(snap(orders, supply), ["A"])

    assert plan.ready == {}
    assert codes(plan) == ["material_short"]


# --- shortage policy --------------------------------------------------------------------------


def test_material_ready_times_uncovered_flag_gives_material_short_without_ready() -> None:
    orders = [
        order("A", [req("i2", 5.0, order_id="A", op_id="10")]),
        order(
            "B",
            [
                req("i3", 1.0, order_id="B", op_id="10"),
                req("i2", 5.0, order_id="B", op_id="20"),
            ],
            ops=(("10", 10), ("20", 20)),
        ),
    ]
    snapshot_ = snap(orders, inventory=[inv("i2", 5.0), inv("i3", 1.0)])

    plan = run(snapshot_, ["A", "B"], ErpPolicy(shortage_policy="flag"))

    assert plan.ready == {("A", "10"): 0.0, ("B", "10"): 0.0}
    assert ("B", "20") not in plan.ready
    assert plan.held_orders == frozenset()
    [issue] = plan.issues
    assert issue.code == "material_short"
    assert issue.severity == "soft"
    assert issue.path == "$.production_orders[1].materials[1]"
    assert issue.lineage == snapshot_.production_orders[1].materials[1].lineage


def test_material_ready_times_default_policy_is_flag() -> None:
    orders = [order("A", [req("i2", 5.0, order_id="A", op_id="10")])]

    plan = run(snap(orders), ["A"])

    assert codes(plan) == ["material_short"]
    assert plan.held_orders == frozenset()


def test_material_ready_times_uncovered_hold_holds_order_and_keeps_issue() -> None:
    orders = [
        order("A", [req("i2", 5.0, order_id="A", op_id="10")]),
        order("B", [req("i2", 5.0, order_id="B", op_id="10")]),
    ]
    snapshot_ = snap(orders, inventory=[inv("i2", 5.0)])

    plan = run(snapshot_, ["A", "B"], ErpPolicy(shortage_policy="hold"))

    assert plan.held_orders == frozenset({"B"})
    assert plan.ready[("A", "10")] == 0.0
    [issue] = plan.issues
    assert issue.code == "material_short"
    assert issue.severity == "soft"
    assert issue.path == "$.production_orders[1].materials[0]"
    assert issue.lineage == snapshot_.production_orders[1].materials[0].lineage


# --- determinism ------------------------------------------------------------------------------


def _rich_orders() -> list[Data]:
    return [
        order(
            "A",
            [
                req("i2", 6.0, order_id="A", op_id="10"),
                req("i3", 2.0, order_id="A"),
                req("i2", 3.0, order_id="A", op_id="20"),
            ],
            ops=(("10", 10), ("20", 20), ("30", 30)),
        ),
        order("B", [req("i2", 8.0, order_id="B", op_id="10")]),
        order(
            "C",
            [
                req("i3", 5.0, order_id="C", op_id="20"),
                req("i4", 1.0, order_id="C", op_id="10", issued=1.0),
            ],
            ops=(("10", 10), ("20", 20)),
        ),
        order("D", [req("i2", 50.0, order_id="D")]),
    ]


def _rich_purchases() -> list[Data]:
    return [
        po("P1", "i2", 5.0, promised="2026-10-07"),
        po("P2", "i2", 10.0, received=4.0, promised="2026-10-09"),
        po("P3", "i3", 4.0, promised="2026-10-01"),
        po("P4", "i3", 7.0),
        po("P5", "i2", 99.0, promised="2026-10-06", status="closed"),
    ]


def _rich_inventory() -> list[Data]:
    return [inv("i2", 3.0, "MAIN"), inv("i2", 4.0, "EAST"), inv("i3", 1.0, "MAIN")]


def _shuffled(seed: int) -> ErpSnapshot:
    rng = random.Random(seed)  # noqa: S311 - deterministic shuffle, not cryptography
    orders = _rich_orders()
    for record in orders:
        rng.shuffle(record["materials"])
        rng.shuffle(record["operations"])
    purchases, inventory = _rich_purchases(), _rich_inventory()
    for collection in (orders, purchases, inventory):
        rng.shuffle(collection)
    return snap(orders, purchases, inventory)


def _issue_identity(issue: ErpIssue) -> tuple[str, str, str]:
    lineage_json = issue.lineage.model_dump_json() if issue.lineage is not None else ""
    return (issue.code, issue.severity, lineage_json)


JOBS = ["B", "A", "C", "D"]
HOLD = ErpPolicy(shortage_policy="hold", po_past_due_grace_seconds=GRACE)


def test_material_ready_times_rich_case_matches_hand_netting() -> None:
    # i2 supply: 7 @0, 5 @10-07, 6 @10-09 (P2 10-4), P5 closed.
    #   B:10 needs 8 -> cum 8 -> 10-07; A:10 needs 6 -> cum 14 -> 10-07 (cum supply 12 < 14)
    #   -> 10-09 (18); A:20 needs 3 -> cum 17 -> 10-09; D:None needs 50 -> short.
    # i3 supply: 1 @0, 4 @grace (P3 past due), P4 no promise.
    #   A:None needs 2 -> cum 2 -> grace; C:20 needs 5 -> cum 7 > 5 -> short.
    # i4: fully issued, nothing.
    snapshot_ = snap(_rich_orders(), _rich_purchases(), _rich_inventory())

    plan = run(snapshot_, JOBS, HOLD)

    assert plan.ready == {
        ("B", "10"): OCT_7_SECONDS,
        ("A", "10"): OCT_9_SECONDS,
        ("A", "20"): OCT_9_SECONDS,
        ("A", None): GRACE,
    }
    assert plan.held_orders == frozenset({"C", "D"})
    assert codes(plan) == ["material_short", "material_short", "po_no_promise", "po_past_due"]


def test_material_ready_times_same_input_twice_is_identical() -> None:
    snapshot_ = snap(_rich_orders(), _rich_purchases(), _rich_inventory())

    first = run(snapshot_, JOBS, HOLD)
    second = run(snapshot_, JOBS, HOLD)

    assert first.ready == second.ready
    assert first.held_orders == second.held_orders
    assert first.issues == second.issues


@pytest.mark.parametrize("seed", range(8))
def test_material_ready_times_shuffled_collections_give_same_plan(seed: int) -> None:
    baseline = run(snap(_rich_orders(), _rich_purchases(), _rich_inventory()), JOBS, HOLD)

    shuffled_snapshot = _shuffled(seed)
    shuffled = run(shuffled_snapshot, JOBS, HOLD)

    assert shuffled.ready == baseline.ready
    assert shuffled.held_orders == baseline.held_orders
    assert sorted(map(_issue_identity, shuffled.issues)) == sorted(
        map(_issue_identity, baseline.issues)
    )
    for issue in shuffled.issues:
        record = resolve(shuffled_snapshot, record_path(issue.path))
        assert record["lineage"] == issue.lineage.model_dump(mode="json")


def test_material_ready_times_every_issue_path_points_at_its_lineage_record() -> None:
    snapshot_ = snap(_rich_orders(), _rich_purchases(), _rich_inventory())

    plan = run(snapshot_, JOBS, HOLD)

    assert plan.issues
    for issue in plan.issues:
        assert issue.lineage is not None
        if issue.code == "material_short":
            assert re.fullmatch(r"\$\.production_orders\[\d+\]\.materials\[\d+\]", issue.path)
        else:
            assert re.fullmatch(r"\$\.purchase_lines\[\d+\]\.promised_date", issue.path)
        record = resolve(snapshot_, record_path(issue.path))
        assert record["lineage"] == issue.lineage.model_dump(mode="json")
