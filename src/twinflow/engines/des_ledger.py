"""COMP-153: dollars for one discrete-event period move (REQ-198, D-141).

Between two snapshots `before` (time t0) and `after` (time t1):

- holding    = sum over stocks of the level at t0 x `holding_per_unit_day` x (t1 - t0) days.
               This is a left rectangle on the sampled levels (D-141), an approximation: a
               shorter `every_hours` tracks the true area more closely. The exact area is in
               `twinflow.instrumentation.inventory` and can replace this later.
- setup      = machines whose setup group changed between t0 and t1 x `setup.cost`. Only
               sampled changes are seen: two changes inside one interval count once.
- revenue    = good units of orders completed in (t0, t1] x the part's `price`.
- production = the same units x the part's `unit_cost` (zero when not given).
- late       = days late of orders completed in (t0, t1] x the part's `late_cost_per_day`.
- lost_sales = 0: a make-to-order floor ships late rather than losing the sale.

Times are simulation seconds; due dates use the same clock. Missing money facts cost $0
(D-133); `des_economics` returns the plain-word info issues that say which ones.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from twinflow.plan.observers import OrderView
from twinflow.state.economics import Economics, parse_economics
from twinflow.state.issues import Issue
from twinflow.state.records import Ledger, Snapshot

SECONDS_PER_DAY = 86400.0


def _names(entries: object) -> list[str]:
    if not isinstance(entries, list):
        return []
    return [
        str(entry["name"]) for entry in entries if isinstance(entry, Mapping) and "name" in entry
    ]


def des_economics(model: Mapping[str, object]) -> tuple[Economics, list[Issue]]:
    """Parse the free-form `economics:` section of a DES model dict.

    The model's things are its stocks and part types; an `economics.things` key that is
    neither is an error naming its path. A missing section gives one info issue.
    """
    things = list(dict.fromkeys([*_names(model.get("stocks")), *_names(model.get("part_types"))]))
    return parse_economics(model.get("economics"), things)


def _setup_changes(before: Snapshot, after: Snapshot) -> int:
    return sum(
        1
        for machine, group in after.setup.items()
        if group is not None and before.setup.get(machine) != group
    )


def completed_between(orders: Sequence[OrderView], t0: float, t1: float) -> list[OrderView]:
    """Orders whose completion time lies in the half-open interval (t0, t1]."""
    return [
        order
        for order in orders
        if order.completed_at is not None and t0 < order.completed_at <= t1
    ]


def period_ledger(
    before: Snapshot,
    after: Snapshot,
    orders: Sequence[OrderView],
    economics: Economics,
) -> tuple[Ledger, dict[str, float]]:
    """The ledger and the `events` map (`orders_done`, `done.<part>` units) for one period."""
    days = max(after.t - before.t, 0.0) / SECONDS_PER_DAY
    holding = sum(
        level * economics.thing(stock).holding_per_unit_day * days
        for stock, level in before.stock.items()
    )
    revenue = production = late = 0.0
    events: dict[str, float] = {}
    done = completed_between(orders, before.t, after.t)
    for order in done:
        money = economics.thing(order.part)
        units = min(order.qty, order.delivered)
        revenue += units * money.price
        production += units * money.unit_cost
        finished = order.completed_at if order.completed_at is not None else after.t
        days_late = max(finished - order.due_date, 0.0) / SECONDS_PER_DAY
        late += days_late * money.late_cost_per_day
        events[f"done.{order.part}"] = events.get(f"done.{order.part}", 0.0) + units
    if done:
        events = {"orders_done": float(len(done)), **events}
    ledger = Ledger(
        revenue=revenue,
        production=production,
        setup=_setup_changes(before, after) * economics.setup.cost,
        holding=holding,
        late=late,
    )
    return ledger, events
