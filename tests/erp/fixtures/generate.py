"""Generated ERP-neutral spring-shop snapshots for TF-005 tests (T-198, SDD COMP-124, REQ-178).

Everything is built from code with one `random.Random(seed)`; no wall clock, no client data.
The shape follows a coil-spring shop: coil, form, grind, oven (batch), inspect (crew) and
pack work centers, an outside plating step in the middle of some routes, three shift
calendars, three wire families x three diameters, and orders in every status.

`small_shop()` is the clean fixture: it carries only SDD *soft* issues, each placed on
purpose (`unknown_priority_class`, `no_due_date`, `po_past_due`, `material_short`,
`in_process_machine_inferred`, `inactive_machine_option`) and no hard issue.
`dst_variant()` is the same shop two weeks later, so a 14-day horizon crosses the
2026-11-01 fall-back in America/Chicago. `scaled_shop()` is the 600-order performance shape.

Regenerate the checked-in JSON with `python tests/erp/fixtures/generate.py` (runs `main()`).
Import from tests as `from fixtures.generate import small_shop` (pytest prepend mode puts
`tests/erp/` on `sys.path`).

Resolved SDD gaps (documented so later tests do not guess):
* An outside operation needs a `work_center_id` but the SDD lists only six (machine) work
  centers; the plating step carries the work center of the in-plant step before it and has
  no options, so it never needs a machine.
* `SalesLink.line_nbr` is an `Id` (text) while `SalesOrderLine.line_nbr` is an int; links
  here use `str(line_nbr)`.
* "Late" purchase line: open, promised after every due date of the orders that need its item.
* The shortage misses total open demand by half the smallest requirement, so exactly one
  requirement is uncovered whatever order COMP-115 allocates in.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from twinflow.erp.digest import snapshot_digest
from twinflow.erp.schema import (
    BatchSpec,
    Bom,
    BomLine,
    Calendar,
    CalendarException,
    CrewNeed,
    Customer,
    ErpSnapshot,
    InventoryBalance,
    Item,
    LaborPool,
    Lineage,
    Machine,
    MaterialRequirement,
    OperationOption,
    OrderOperation,
    OutsideProcess,
    Overtime,
    OvertimeWindow,
    ProductionOrder,
    PurchaseLine,
    Receipt,
    Routing,
    RoutingOperation,
    SalesLink,
    SalesOrderLine,
    SourceInfo,
    TimeWindow,
    Vendor,
    WeekdayOvertime,
    WeekdayWindows,
    WorkCenter,
)

PLANT_TZ = "America/Chicago"
SMALL_SHOP_JSON = Path(__file__).with_name("small_shop.json")
SMALL_AS_OF_DATE = dt.date(2026, 10, 12)  # a Monday
DST_AS_OF_DATE = dt.date(2026, 10, 26)  # a Monday; as_of + 14 days crosses 2026-11-01
AS_OF_TIME = dt.time(5, 0)

PLATING_LEAD_SECONDS = 3 * 86_400.0
PLATING_VENDOR = "VEND-PLATE"
INSPECTORS = "inspectors"
BOX_ITEM = "BOX-STD"
SITE = "MAIN"
OVEN_BATCH = BatchSpec(capacity=500.0, unit="ea")

FAMILIES = {"music_wire": "MW", "stainless_302": "SS", "chrome_silicon": "CS"}
DIAMETERS = (0.8, 1.6, 3.2)
PRIORITY_CLASSES = ("A-KEY", "B-CORE", "C-SPOT")
UNMAPPED_CLASS = "D-NEW"
STATUS_SHARES = (  # closed takes the rest; 30 orders give 8/8/7/2/3/2
    ("planned", 8 / 30),
    ("released", 8 / 30),
    ("in_process", 7 / 30),
    ("on_hold", 2 / 30),
    ("complete", 3 / 30),
)
MIN_ORDERS = 20  # enough orders to give every scenario role its own order

SATURDAY = 5
WEEKDAYS = range(5)
DAY_SHIFT = TimeWindow(start_minute=420, end_minute=930)  # 07:00-15:30
NIGHT_SHIFT = TimeWindow(start_minute=1320, end_minute=360)  # 22:00-06:00 next day
SATURDAY_SHIFT = TimeWindow(start_minute=420, end_minute=720)  # 07:00-12:00
FULL_DAY = TimeWindow(start_minute=0, end_minute=1440)
HOLIDAY_OFFSET_DAYS = 11  # the Friday of the second week
SHORT_DAY_OFFSET_DAYS = 9  # the Wednesday of the second week

# Work center -> (calendar, base run seconds per unit, setup seconds).
CENTERS: dict[str, tuple[str, float, float]] = {
    "coil": ("two_shift", 1.6, 1_800.0),
    "form": ("two_shift", 1.1, 1_200.0),
    "grind": ("one_shift_sat", 0.9, 900.0),
    "oven": ("24x5", 0.0, 600.0),
    "inspect": ("one_shift_sat", 0.4, 0.0),
    "pack": ("one_shift_sat", 0.2, 300.0),
}
WC_LEVEL_CENTERS = {"oven", "inspect"}  # options say "any active machine of the center"
OVEN_CYCLE_SECONDS = 7_200.0
SLOW_MACHINE = "grind-2"  # efficiency 0.9
INACTIVE_MACHINE = "form-3"
OVERTIME_CENTER = "form"
OVERTIME_MACHINE = "form-2"


@dataclass(frozen=True)
class _Shape:
    """The size and dates of one generated shop."""

    name: str
    seed: int
    orders: int
    parts: int
    machines: dict[str, int]
    customers_per_class: int
    inspectors: int
    due_span_days: int
    as_of_date: dt.date


@dataclass(frozen=True)
class _Part:
    item_id: str
    wire_id: str
    wire_per_unit: float
    routing: Routing


@dataclass
class _Built:
    """Accumulated order-side records."""

    orders: list[ProductionOrder] = field(default_factory=list)
    sales_lines: list[SalesOrderLine] = field(default_factory=list)


def lineage(table: str, *key: str) -> Lineage:
    return Lineage(table=table, key=key)


# --- public entry points ------------------------------------------------------------------------


def small_shop(seed: int = 7, orders: int = 30) -> ErpSnapshot:
    """The clean 30-order small shop as of Monday 2026-10-12 05:00 America/Chicago."""
    return _generate(_small_shape("small-shop", seed, orders, SMALL_AS_OF_DATE))


def dst_variant(seed: int = 7, orders: int = 30) -> ErpSnapshot:
    """The small shop as of Monday 2026-10-26 05:00, so a 14-day horizon spans fall-back."""
    return _generate(_small_shape("small-shop-dst", seed, orders, DST_AS_OF_DATE))


def scaled_shop(orders: int = 600, seed: int = 11) -> ErpSnapshot:
    """The performance shape: about 3,000 operations on 80 machines, 8 weeks of due dates."""
    shape = _Shape(
        name="scaled-shop",
        seed=seed,
        orders=orders,
        parts=60,
        machines={"coil": 16, "form": 24, "grind": 16, "oven": 6, "inspect": 8, "pack": 10},
        customers_per_class=6,
        inspectors=4,
        due_span_days=56,
        as_of_date=SMALL_AS_OF_DATE,
    )
    return _generate(shape)


def to_json(snapshot: ErpSnapshot, indent: int | None = 1) -> str:
    """The stable text form of a snapshot (the checked-in file uses the default indent)."""
    return snapshot.model_dump_json(indent=indent) + "\n"


def main() -> None:
    """Rewrite `small_shop.json` from `small_shop()`."""
    SMALL_SHOP_JSON.write_text(to_json(small_shop()), encoding="utf-8")


def _small_shape(name: str, seed: int, orders: int, as_of_date: dt.date) -> _Shape:
    return _Shape(
        name=name,
        seed=seed,
        orders=orders,
        parts=20,
        machines={"coil": 2, "form": 3, "grind": 2, "oven": 1, "inspect": 1, "pack": 1},
        customers_per_class=3,
        inspectors=2,
        due_span_days=20,
        as_of_date=as_of_date,
    )


# --- assembly -----------------------------------------------------------------------------------


def _generate(shape: _Shape) -> ErpSnapshot:
    if shape.orders < MIN_ORDERS:
        raise ValueError(f"a generated shop needs at least {MIN_ORDERS} orders")
    rng = random.Random(shape.seed)  # noqa: S311  # deterministic test data, not crypto
    as_of = dt.datetime.combine(shape.as_of_date, AS_OF_TIME, tzinfo=ZoneInfo(PLANT_TZ))
    machines = _machines(shape)
    parts = _parts(rng, shape, machines)
    customers = _customers(shape)
    built = _orders(rng, shape, as_of, parts, machines, customers)
    inventory, purchases = _material_supply(shape, as_of, built.orders)
    snapshot = ErpSnapshot(
        schema_version="twinflow.erp/1",
        as_of=as_of,
        plant_timezone=PLANT_TZ,
        source=SourceInfo(
            system="generated",
            instance=shape.name,
            extractor="twinflow-fixture",
            extractor_version="1",
            generation=f"seed-{shape.seed}",
        ),
        source_issues=(),
        items=_items(parts),
        boms=tuple(_bom(part) for part in parts),
        routings=tuple(part.routing for part in parts),
        work_centers=_work_centers(),
        machines=machines,
        calendars=_calendars(shape.as_of_date),
        labor_pools=(
            LaborPool(
                id=INSPECTORS,
                size=shape.inspectors,
                calendar_id="one_shift_sat",
                lineage=lineage("labor_pool", INSPECTORS),
            ),
        ),
        production_orders=tuple(built.orders),
        sales_order_lines=tuple(built.sales_lines),
        customers=customers,
        vendors=_vendors(),
        purchase_lines=purchases,
        inventory=inventory,
    )
    return snapshot.model_copy(update={"digest": snapshot_digest(snapshot)})


# --- resources ----------------------------------------------------------------------------------


def _work_centers() -> tuple[WorkCenter, ...]:
    return tuple(
        WorkCenter(
            id=wc,
            description=f"{wc} work center",
            calendar_id=calendar,
            attributes={"op_kind": wc},
            overtime=_saturday_overtime(420, 900, 1.5) if wc == OVERTIME_CENTER else None,
            lineage=lineage("work_center", wc),
        )
        for wc, (calendar, _, _) in CENTERS.items()
    )


def _machines(shape: _Shape) -> tuple[Machine, ...]:
    return tuple(
        Machine(
            id=machine_id,
            work_center_id=wc,
            description=f"{wc} machine {n}",
            efficiency=0.9 if machine_id == SLOW_MACHINE else 1.0,
            active=machine_id != INACTIVE_MACHINE,
            overtime=_saturday_overtime(420, 720, 2.0) if machine_id == OVERTIME_MACHINE else None,
            lineage=lineage("machine", machine_id),
        )
        for wc, count in shape.machines.items()
        for n in range(1, count + 1)
        for machine_id in [f"{wc}-{n}"]
    )


def _saturday_overtime(start: int, end: int, multiplier: float) -> Overtime:
    window = OvertimeWindow(start_minute=start, end_minute=end, cost_multiplier=multiplier)
    return Overtime(week=(WeekdayOvertime(weekday=SATURDAY, windows=(window,)),), exceptions=())


def _calendars(as_of_date: dt.date) -> tuple[Calendar, ...]:
    holiday = CalendarException(
        date=as_of_date + dt.timedelta(days=HOLIDAY_OFFSET_DAYS),
        windows=(),
        description="plant holiday",
    )
    short_day = CalendarException(
        date=as_of_date + dt.timedelta(days=SHORT_DAY_OFFSET_DAYS),
        windows=(TimeWindow(start_minute=420, end_minute=720),),
        description="short day: day shift ends at noon, no night shift",
    )
    weekly = {
        "two_shift": {d: (DAY_SHIFT, NIGHT_SHIFT) for d in WEEKDAYS},
        "one_shift_sat": {**{d: (DAY_SHIFT,) for d in WEEKDAYS}, SATURDAY: (SATURDAY_SHIFT,)},
        "24x5": {d: (FULL_DAY,) for d in WEEKDAYS},
    }
    return tuple(
        Calendar(
            id=calendar_id,
            description=f"{calendar_id} calendar",
            week=tuple(WeekdayWindows(weekday=d, windows=w) for d, w in days.items()),
            exceptions=(short_day, holiday) if calendar_id == "two_shift" else (holiday,),
            lineage=lineage("calendar", calendar_id),
        )
        for calendar_id, days in weekly.items()
    )


def _customers(shape: _Shape) -> tuple[Customer, ...]:
    classes = [
        (code, f"CUST-{code[0]}{n:02d}")
        for code in PRIORITY_CLASSES
        for n in range(1, shape.customers_per_class + 1)
    ]
    classes.append((UNMAPPED_CLASS, "CUST-D01"))
    return tuple(
        Customer(
            id=cid,
            name=f"Customer {cid[5:]}",
            priority_class=code,
            lineage=lineage("customer", cid),
        )
        for code, cid in classes
    )


def _vendors() -> tuple[Vendor, ...]:
    names = {
        PLATING_VENDOR: "Plating vendor",
        "VEND-WIRE-A": "Wire supplier A",
        "VEND-WIRE-B": "Wire supplier B",
        "VEND-PACK": "Packaging supplier",
    }
    return tuple(
        Vendor(id=vid, name=name, lineage=lineage("vendor", vid)) for vid, name in names.items()
    )


# --- items, routings, boms ----------------------------------------------------------------------


def _wire_id(family: str, diameter: float) -> str:
    return f"WIRE-{FAMILIES[family]}-{diameter}"


def _items(parts: list[_Part]) -> tuple[Item, ...]:
    wires = [
        Item(
            id=_wire_id(family, diameter),
            description=f"{family} wire {diameter} mm",
            uom="lb",
            item_class="raw_wire",
            attributes={"wire_family": family, "wire_diameter_mm": diameter},
            lineage=lineage("item", _wire_id(family, diameter)),
        )
        for family in FAMILIES
        for diameter in DIAMETERS
    ]
    box = Item(
        id=BOX_ITEM,
        description="standard shipping box",
        uom="ea",
        item_class="packaging",
        attributes={},
        lineage=lineage("item", BOX_ITEM),
    )
    springs = [_spring_item(part) for part in parts]
    return (*wires, box, *springs)


def _spring_item(part: _Part) -> Item:
    first_op = part.routing.operations[0]
    return Item(
        id=part.item_id,
        description=f"compression spring {part.item_id}",
        uom="ea",
        item_class="spring",
        attributes=dict(first_op.setup_params),
        lineage=lineage("item", part.item_id),
    )


def _parts(rng: random.Random, shape: _Shape, machines: tuple[Machine, ...]) -> list[_Part]:
    by_center: dict[str, list[Machine]] = {}
    for machine in machines:
        by_center.setdefault(machine.work_center_id, []).append(machine)
    families = list(FAMILIES)
    parts = []
    for index in range(shape.parts):
        item_id = f"SPR-{index + 1:03d}"
        family = families[index % 3]
        diameter = DIAMETERS[(index // 3) % 3]
        wire = {"wire_family": family, "wire_diameter_mm": diameter}
        kinds = _route_kinds(rng, index)
        speed = round(rng.uniform(0.7, 1.4), 3)
        ops = _route_operations(rng, item_id, kinds, wire, speed, by_center)
        routing = Routing(
            id=f"RT-{item_id}",
            item_id=item_id,
            revision="A",
            operations=ops,
            lineage=lineage("routing", item_id, "A"),
        )
        wire_per_unit = round(rng.uniform(0.01, 0.08), 4)
        parts.append(_Part(item_id, _wire_id(family, diameter), wire_per_unit, routing))
    return parts


def _route_kinds(rng: random.Random, index: int) -> list[str]:
    """3 to 6 steps: coil, form, optional oven/grind, plating on every 4th part, a tail."""
    middle = [wc for wc in ("oven", "grind") if rng.random() < 0.5]
    plating = ["plating"] if index % 4 == 1 else []
    tail = ["inspect", "pack"] if rng.random() < 0.6 else ["pack"]
    kinds = ["coil", "form", *middle, *plating, *tail]
    if len(kinds) > 6:
        kinds.remove("grind")
    return kinds


def _route_operations(
    rng: random.Random,
    item_id: str,
    kinds: list[str],
    wire: dict[str, float | str],
    speed: float,
    by_center: dict[str, list[Machine]],
) -> tuple[RoutingOperation, ...]:
    ops: list[RoutingOperation] = []
    for position, kind in enumerate(kinds):
        op_id = f"{(position + 1) * 10:04d}"
        source = lineage("routing_operation", item_id, "A", op_id)
        timing = {
            "queue_seconds": float(rng.choice((0, 900, 1_800))),
            "move_seconds": float(rng.choice((300, 600))),
        }
        if kind == "plating":
            outside = OutsideProcess(vendor_id=PLATING_VENDOR, lead_seconds=PLATING_LEAD_SECONDS)
            ops.append(
                RoutingOperation(
                    op_id=op_id,
                    sequence=(position + 1) * 10,
                    work_center_id=ops[-1].work_center_id,
                    options=(),
                    setup_params={"finish": "zinc"},
                    outside=outside,
                    lineage=source,
                    **timing,
                )
            )
            continue
        ops.append(
            RoutingOperation(
                op_id=op_id,
                sequence=(position + 1) * 10,
                work_center_id=kind,
                options=_options(rng, item_id, op_id, kind, speed, by_center[kind]),
                setup_params=_setup_params(kind, wire),
                crew=CrewNeed(labor_pool_id=INSPECTORS, size=1) if kind == "inspect" else None,
                batch=OVEN_BATCH if kind == "oven" else None,
                lineage=source,
                **timing,
            )
        )
    return tuple(ops)


def _setup_params(kind: str, wire: dict[str, float | str]) -> dict[str, float | str]:
    if kind == "coil":
        return dict(wire)
    if kind in ("form", "grind"):
        return {"wire_diameter_mm": wire["wire_diameter_mm"]}
    if kind == "oven":
        return {"recipe": "stress_relieve_230c"}
    return {}


def _options(
    rng: random.Random,
    item_id: str,
    op_id: str,
    kind: str,
    speed: float,
    machines: list[Machine],
) -> tuple[OperationOption, ...]:
    _, base_rate, setup = CENTERS[kind]
    rate = round(base_rate * speed, 3)
    fixed = OVEN_CYCLE_SECONDS if kind == "oven" else 0.0
    if kind in WC_LEVEL_CENTERS or (kind == "form" and rng.random() < 0.5):
        return (_option(item_id, op_id, kind, None, setup, rate, fixed, 0),)
    chosen = rng.sample(machines, min(3, len(machines)))
    if not any(m.active for m in chosen):
        chosen[0] = rng.choice([m for m in machines if m.active])
    return tuple(
        _option(item_id, op_id, kind, m.id, setup, round(rate * (1 + 0.12 * rank), 3), fixed, rank)
        for rank, m in enumerate(chosen)
    )


def _option(
    item_id: str,
    op_id: str,
    kind: str,
    machine_id: str | None,
    setup: float,
    rate: float,
    fixed: float,
    preference: int,
) -> OperationOption:
    return OperationOption(
        work_center_id=kind,
        machine_id=machine_id,
        setup_seconds=setup,
        run_seconds_per_unit=rate,
        fixed_run_seconds=fixed,
        preference=preference,
        lineage=lineage("operation_option", item_id, "A", op_id, machine_id or kind),
    )


def _bom(part: _Part) -> Bom:
    ops = part.routing.operations
    return Bom(
        id=f"BOM-{part.item_id}",
        item_id=part.item_id,
        revision="A",
        lines=(
            BomLine(item_id=part.wire_id, qty_per=part.wire_per_unit, uom="lb", op_id=ops[0].op_id),
            BomLine(item_id=BOX_ITEM, qty_per=0.01, uom="ea", op_id=ops[-1].op_id),
        ),
        lineage=lineage("bom", part.item_id, "A"),
    )


# --- orders -------------------------------------------------------------------------------------


def _statuses(rng: random.Random, count: int) -> list[str]:
    statuses: list[str] = []
    for status, share in STATUS_SHARES:
        statuses += [status] * round(count * share)
    statuses += ["closed"] * (count - len(statuses))
    rng.shuffle(statuses)
    return statuses


def _roles(statuses: list[str]) -> dict[int, str]:
    """Give the first orders of some statuses a scenario role (see the module docstring)."""
    wanted = {
        "planned": ("no_due_date", "stock", "requested_only", "rush"),
        "released": ("hold_flag", "rush", "unmapped_customer"),
        "in_process": ("on_machine", "machine_inferred"),
    }
    roles: dict[int, str] = {}
    for status, names in wanted.items():
        indexes = [i for i, s in enumerate(statuses) if s == status]
        roles.update(zip(indexes, names, strict=False))
    return roles


def _orders(
    rng: random.Random,
    shape: _Shape,
    as_of: dt.datetime,
    parts: list[_Part],
    machines: tuple[Machine, ...],
    customers: tuple[Customer, ...],
) -> _Built:
    statuses = _statuses(rng, shape.orders)
    roles = _roles(statuses)
    mapped = [c.id for c in customers if c.priority_class != UNMAPPED_CLASS]
    unmapped = next(c.id for c in customers if c.priority_class == UNMAPPED_CLASS)
    built = _Built()
    for index, status in enumerate(statuses):
        role = roles.get(index, "")
        order_id = f"MO-{index + 1:04d}"
        part = rng.choice(parts)
        quantity = float(rng.randrange(200, 2_001, 50))
        due = shape.as_of_date + dt.timedelta(days=rng.randint(2, shape.due_span_days))
        links: tuple[SalesLink, ...] = ()
        if role != "stock":
            customer = unmapped if role == "unmapped_customer" else mapped[index % len(mapped)]
            line = _sales_line(rng, order_id, index, customer, part, quantity, status, due, role)
            built.sales_lines.append(line)
            links = (SalesLink(order_type="SO", order_nbr=line.order_nbr, line_nbr="1"),)
        released = status != "planned"
        start_offset = -rng.randint(1, 10) if released else rng.randint(0, 3)
        built.orders.append(
            ProductionOrder(
                id=order_id,
                order_type="MO",
                item_id=part.item_id,
                quantity=quantity,
                status=status,  # type: ignore[arg-type]  # drawn from the status literals
                rush=role == "rush",
                hold=role == "hold_flag",
                erp_priority=None if role == "stock" else rng.choice((None, 3, 5, 7)),
                release_date=shape.as_of_date + dt.timedelta(days=start_offset),
                due_date=None if role == "no_due_date" else due,
                sales_links=links,
                operations=_order_operations(
                    rng, order_id, part, quantity, status, role, as_of, machines
                ),
                materials=_materials(order_id, index, part, quantity, status),
                lineage=lineage("production_order", order_id),
            )
        )
    return built


def _sales_line(
    rng: random.Random,
    order_id: str,
    index: int,
    customer_id: str,
    part: _Part,
    quantity: float,
    status: str,
    due: dt.date,
    role: str,
) -> SalesOrderLine:
    order_nbr = f"SO-{5_001 + index}"
    requested: dt.date | None = due
    promised: dt.date | None = due + dt.timedelta(days=rng.choice((0, 0, 1, 2)))
    if role in ("requested_only", "no_due_date"):
        promised = None
    if role == "no_due_date":
        requested = None
    return SalesOrderLine(
        order_type="SO",
        order_nbr=order_nbr,
        line_nbr=1,
        customer_id=customer_id,
        item_id=part.item_id,
        qty_open=0.0 if status in ("complete", "closed") else quantity,
        requested_date=requested,
        promised_date=promised,
        lineage=lineage("sales_order_line", "SO", order_nbr, "1"),
    )


def _order_operations(
    rng: random.Random,
    order_id: str,
    part: _Part,
    quantity: float,
    status: str,
    role: str,
    as_of: dt.datetime,
    machines: tuple[Machine, ...],
) -> tuple[OrderOperation, ...]:
    ops = []
    for position, template in enumerate(part.routing.operations):
        progress = _progress(rng, quantity, status, position)
        current = None
        actual_start = None
        if progress["status"] == "in_process":
            if role != "machine_inferred":
                current = _current_machine(template, machines)
            actual_start = as_of - dt.timedelta(hours=rng.randint(1, 6))
        fields = template.model_dump(exclude={"lineage", "options", "outside", "crew", "batch"})
        ops.append(
            OrderOperation(
                **fields,
                options=template.options,
                outside=template.outside,
                crew=template.crew,
                batch=template.batch,
                current_machine_id=current,
                actual_start=actual_start,
                lineage=lineage("order_operation", order_id, template.op_id),
                **progress,
            )
        )
    return tuple(ops)


def _progress(rng: random.Random, quantity: float, status: str, position: int) -> dict:
    """Op status and quantities: done orders are complete; in_process runs its second op."""
    done = status in ("complete", "closed") or (status == "in_process" and position == 0)
    if done:
        return _quantities("complete", quantity, quantity, 0.0)
    if status == "in_process" and position == 1:
        complete = float(int(quantity * rng.uniform(0.25, 0.6)))
        return _quantities("in_process", quantity, complete, float(rng.randint(0, 5)))
    return _quantities("pending", quantity, 0.0, 0.0)


def _quantities(status: str, quantity: float, complete: float, scrapped: float) -> dict:
    return {
        "status": status,
        "qty_to_produce": quantity,
        "qty_complete": complete,
        "qty_scrapped": scrapped,
        "qty_remaining": quantity - complete - scrapped,
    }


def _current_machine(op: RoutingOperation, machines: tuple[Machine, ...]) -> str:
    """The most preferred active machine an in-process op can run on."""
    by_id = {m.id: m for m in machines}
    for option in sorted(op.options, key=lambda o: o.preference):
        if option.machine_id is not None and by_id[option.machine_id].active:
            return option.machine_id
        if option.machine_id is None:
            return next(
                m.id for m in machines if m.work_center_id == option.work_center_id and m.active
            )
    raise ValueError("an in-process operation needs an active option")


def _materials(
    order_id: str, index: int, part: _Part, quantity: float, status: str
) -> tuple[MaterialRequirement, ...]:
    ops = part.routing.operations
    wire_qty = round(quantity * part.wire_per_unit, 3)
    box_qty = round(quantity * 0.01, 3)
    done = status in ("complete", "closed")
    wire_issued = done or status == "in_process"  # coiling (the first op) is done
    return (
        MaterialRequirement(
            item_id=part.wire_id,
            op_id=ops[0].op_id if index % 2 == 0 else None,
            qty_required=wire_qty,
            qty_issued=wire_qty if wire_issued else 0.0,
            lineage=lineage("material_requirement", order_id, part.wire_id),
        ),
        MaterialRequirement(
            item_id=BOX_ITEM,
            op_id=ops[-1].op_id if index % 2 == 1 else None,
            qty_required=box_qty,
            qty_issued=box_qty if done else 0.0,
            lineage=lineage("material_requirement", order_id, BOX_ITEM),
        ),
    )


# --- material supply ----------------------------------------------------------------------------


def _open_demand(orders: list[ProductionOrder]) -> dict[str, list[float]]:
    demand: dict[str, list[float]] = {}
    for order in orders:
        if order.status in ("complete", "closed", "cancelled"):
            continue
        for req in order.materials:
            if req.qty_required > req.qty_issued:
                demand.setdefault(req.item_id, []).append(req.qty_required - req.qty_issued)
    return demand


def _material_supply(
    shape: _Shape, as_of: dt.datetime, orders: list[ProductionOrder]
) -> tuple[tuple[InventoryBalance, ...], tuple[PurchaseLine, ...]]:
    """Inventory and purchase lines: one wire short, one wire late, the rest covered."""
    demand = _open_demand(orders)
    wires = sorted(item for item in demand if item.startswith("WIRE-"))
    short_item, late_item, received_item = wires[0], wires[1], wires[-1]
    today = shape.as_of_date
    on_hand: dict[str, float] = {_wire_id(f, d): 150.0 for f in FAMILIES for d in DIAMETERS}
    on_hand[BOX_ITEM] = 50.0
    for item, qtys in demand.items():
        on_hand[item] = round(sum(qtys) * 1.25 + 50, 3)

    short_total = sum(demand[short_item])
    short_target = short_total - round(min(demand[short_item]) * 0.5, 3)
    past_due_qty = round(short_target * 0.3, 3)
    on_hand[short_item] = round(short_target - past_due_qty, 3)
    late_total = sum(demand[late_item])
    on_hand[late_item] = round(late_total * 0.5, 3)
    late_qty = round(late_total - on_hand[late_item] + 25, 3)

    def day(offset: int) -> dt.date:
        return today + dt.timedelta(days=offset)

    purchases = (
        _purchase(7001, "VEND-WIRE-A", short_item, past_due_qty, 0.0, "open", day(-3), as_of),
        _purchase(
            7002,
            "VEND-WIRE-B",
            late_item,
            late_qty,
            0.0,
            "open",
            day(shape.due_span_days + 2),
            as_of,
        ),
        _purchase(7003, "VEND-PACK", BOX_ITEM, 5_000.0, 2_000.0, "open", day(1), as_of),
        _purchase(7004, "VEND-WIRE-A", received_item, 300.0, 300.0, "closed", day(-7), as_of),
    )
    inventory = tuple(
        InventoryBalance(
            item_id=item,
            site_id=SITE,
            qty_available=qty,
            lineage=lineage("inventory_balance", item, SITE),
        )
        for item, qty in sorted(on_hand.items())
    )
    return inventory, purchases


def _purchase(
    number: int,
    vendor_id: str,
    item_id: str,
    ordered: float,
    received: float,
    status: str,
    promised: dt.date,
    as_of: dt.datetime,
) -> PurchaseLine:
    order_nbr = f"PO-{number}"
    receipts = (
        (
            Receipt(
                receipt_nbr=f"RC-{number}", qty=received, received_at=as_of - dt.timedelta(days=2)
            ),
        )
        if received > 0
        else ()
    )
    return PurchaseLine(
        order_type="PO",
        order_nbr=order_nbr,
        line_nbr="1",
        vendor_id=vendor_id,
        item_id=item_id,
        qty_ordered=ordered,
        qty_received=received,
        status=status,  # type: ignore[arg-type]  # "open" or "closed" from the caller
        promised_date=promised,
        receipts=receipts,
        lineage=lineage("purchase_line", "PO", order_nbr, "1"),
    )


if __name__ == "__main__":
    main()
