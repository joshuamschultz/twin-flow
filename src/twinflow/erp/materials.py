"""Material readiness: net requirements against stock and purchases (COMP-115, D-122).

`material_ready_times(snapshot, jobs_order, policy)` returns, per requirement, the earliest
time (real elapsed seconds from `as_of`) the item's cumulative supply covers its cumulative
demand. Pure and deterministic: no clock, and shuffling any snapshot collection changes
nothing but the index inside issue paths.

Supply per item:

* `inventory.qty_available`, summed over sites, at 0.0;
* each `open` purchase line's `max(0, qty_ordered - qty_received)`, decided by DATE in
  `plant_timezone`: a promised date before `as_of`'s local date is supply at
  `policy.po_past_due_grace_seconds` with soft `po_past_due`; the local date of `as_of`
  itself is supply at 0.0 (not past due); a later date is supply at 00:00 local of that
  date, as real elapsed seconds (DST-safe);
* an open line with no promised date is not supply and gives soft `po_no_promise`;
* closed lines are ignored (no supply, no issue).

`po_past_due` and `po_no_promise` are emitted for every OPEN line, whether or not any
requirement needs it. Both point at the field that caused them,
`$.purchase_lines[k].promised_date`, the same path the validator uses, so the builder keeps
one copy.

Demand walks `jobs_order` as given (the builder already sorted it). Inside one order,
requirements go by the consuming operation's integer `sequence`; a requirement with no
`op_id` (job release) goes first; ties (same sequence) keep their position in the order's
`materials` tuple; an `op_id` the order does not list goes last. A requirement demands
`qty_required - qty_issued` when positive, else nothing.

Allocation is by running totals per item: a requirement is ready at the earliest supply time
whose cumulative supply covers the item's cumulative demand up to and including it. An
uncovered requirement still counts in cumulative demand, so later requirements of that item
stay uncovered until later supply covers the whole running total. Uncovered gives soft
`material_short` and no ready entry; with `shortage_policy = "hold"` its order is held.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from twinflow.erp.issues import ErpIssue
from twinflow.erp.profile import ErpPolicy
from twinflow.erp.schema import ErpSnapshot, MaterialRequirement, ProductionOrder, PurchaseLine

__all__ = ["MaterialPlan", "ReadyKey", "material_ready_times", "start_of_day_seconds"]

ReadyKey = tuple[str, str | None]

# Relative slack so float noise (0.1 + 0.2 vs 0.3) never turns a covered need into a shortage.
_COVER_TOLERANCE = 1e-9


@dataclass(frozen=True)
class MaterialPlan:
    """Ready times per `(order_id, op_id | None)`, held order ids, and soft issues."""

    ready: dict[ReadyKey, float]
    held_orders: frozenset[str]
    issues: tuple[ErpIssue, ...]


@dataclass(frozen=True)
class _Timeline:
    """One item's supply as sorted times with the cumulative quantity available by each."""

    times: tuple[float, ...]
    cumulative: tuple[float, ...]

    def covered_at(self, demand: float) -> float | None:
        """Earliest time cumulative supply reaches `demand`, or None if it never does."""
        needed = demand - _COVER_TOLERANCE * max(1.0, demand)
        index = bisect.bisect_left(self.cumulative, needed)
        return self.times[index] if index < len(self.times) else None


def start_of_day_seconds(day: dt.date, tz: ZoneInfo, as_of: dt.datetime) -> float:
    """Real elapsed seconds from aware `as_of` to 00:00 of `day` in `tz` (negative if before)."""
    midnight = dt.datetime.combine(day, dt.time(), tzinfo=tz)
    return midnight.timestamp() - as_of.timestamp()


def material_ready_times(
    snapshot: ErpSnapshot, jobs_order: Sequence[str], policy: ErpPolicy
) -> MaterialPlan:
    """Net open orders' material requirements against supply in `jobs_order` priority."""
    events, issues = _supply_events(snapshot, policy)
    timelines = {item: _timeline(item_events) for item, item_events in events.items()}
    orders = {order.id: (index, order) for index, order in enumerate(snapshot.production_orders)}
    demand: defaultdict[str, float] = defaultdict(float)
    ready: dict[ReadyKey, float] = {}
    held: set[str] = set()
    for order_id in jobs_order:
        if order_id not in orders:
            continue
        order_index, order = orders[order_id]
        for material_index, requirement in _allocation_order(order):
            remaining = requirement.qty_required - requirement.qty_issued
            if remaining <= 0:
                continue
            demand[requirement.item_id] += remaining
            timeline = timelines.get(requirement.item_id)
            when = timeline.covered_at(demand[requirement.item_id]) if timeline else None
            if when is not None:
                ready[(order.id, requirement.op_id)] = when
                continue
            issues.append(_short_issue(order_index, material_index, requirement))
            if policy.shortage_policy == "hold":
                held.add(order.id)
    return MaterialPlan(ready=ready, held_orders=frozenset(held), issues=tuple(issues))


def _supply_events(
    snapshot: ErpSnapshot, policy: ErpPolicy
) -> tuple[defaultdict[str, list[tuple[float, float]]], list[ErpIssue]]:
    """Collect `(time, qty)` supply per item and the purchase-line issues, in index order."""
    events: defaultdict[str, list[tuple[float, float]]] = defaultdict(list)
    for balance in snapshot.inventory:
        events[balance.item_id].append((0.0, balance.qty_available))
    issues: list[ErpIssue] = []
    tz = ZoneInfo(snapshot.plant_timezone)
    today = snapshot.as_of.astimezone(tz).date()
    for index, line in enumerate(snapshot.purchase_lines):
        if line.status != "open":
            continue
        if line.promised_date is None:
            issues.append(_po_issue("po_no_promise", index, line))
            continue
        if line.promised_date < today:
            issues.append(_po_issue("po_past_due", index, line))
            when = policy.po_past_due_grace_seconds
        elif line.promised_date == today:
            when = 0.0
        else:
            when = start_of_day_seconds(line.promised_date, tz, snapshot.as_of)
        events[line.item_id].append((when, max(0.0, line.qty_ordered - line.qty_received)))
    return events, issues


def _timeline(events: list[tuple[float, float]]) -> _Timeline:
    """Sort supply events (by time, then quantity, so input order never matters) and sum."""
    times: list[float] = []
    cumulative: list[float] = []
    total = 0.0
    for when, qty in sorted(events):
        total += qty
        times.append(when)
        cumulative.append(total)
    return _Timeline(times=tuple(times), cumulative=tuple(cumulative))


def _allocation_order(order: ProductionOrder) -> Iterator[tuple[int, MaterialRequirement]]:
    """Yield `(materials index, requirement)`: release first, then by op sequence, stable."""
    sequences = {operation.op_id: operation.sequence for operation in order.operations}

    def rank(entry: tuple[int, MaterialRequirement]) -> tuple[int, int, int]:
        index, requirement = entry
        if requirement.op_id is None:
            return (0, 0, index)
        if requirement.op_id not in sequences:
            return (2, 0, index)
        return (1, sequences[requirement.op_id], index)

    yield from sorted(enumerate(order.materials), key=rank)


def _po_issue(code: str, index: int, line: PurchaseLine) -> ErpIssue:
    """A soft purchase-line issue pointing at `$.purchase_lines[index].promised_date`."""
    messages = {
        "po_past_due": "open purchase line promised before as_of; counted at the grace time",
        "po_no_promise": "open purchase line has no promised date; not counted as supply",
    }
    return ErpIssue(
        code=code,
        severity="soft",
        path=f"$.purchase_lines[{index}].promised_date",
        lineage=line.lineage,
        message=messages[code],
    )


def _short_issue(
    order_index: int, material_index: int, requirement: MaterialRequirement
) -> ErpIssue:
    """A soft `material_short` issue for a requirement no supply covers."""
    return ErpIssue(
        code="material_short",
        severity="soft",
        path=f"$.production_orders[{order_index}].materials[{material_index}]",
        lineage=requirement.lineage,
        message="cumulative supply never covers this requirement",
    )
