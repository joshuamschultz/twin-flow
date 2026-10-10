"""Minimal valid payloads for every canonical ERP v1 model (TF-005 SDD COMP-110, COMP-126).

Each factory returns a fresh plain `dict` so a test can mutate one field and assert the
model rejects it. Nothing here imports `twinflow.erp`: the payloads are the contract as
written in the SDD Data Model table, independent of the implementation.

Import it as `from _factories import ...` (pytest's default `prepend` import mode puts
`tests/erp/` on `sys.path`, the same pattern as `tests/scheduling/oracle.py`).

Lineage rule used here (SDD Data Model: "Every record has `lineage`", and the table header
"Fields (besides `lineage`)"): every model that has its own row in that table carries
`lineage`, and so does `MaterialRequirement` (listed with `lineage` explicitly). Value
objects written inline in braces (`BomLine`, `SalesLink`, `Receipt`, `TimeWindow`,
`WeekdayWindows`, `CalendarException`, `OutsideProcess`, `CrewNeed`, `BatchSpec`, the
overtime types) carry none.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Data = dict[str, Any]

AS_OF = "2026-10-05T05:00:00-05:00"
PLANT_TZ = "America/Chicago"

# Every collection on the snapshot envelope, in Data Model order.
COLLECTIONS: tuple[str, ...] = (
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


def lineage(table: str = "T", *key: str) -> Data:
    return {"table": table, "key": list(key or ("k1",))}


def source_info() -> Data:
    return {
        "system": "acme-erp",
        "instance": "mirror",
        "extractor": "acme_mapping",
        "extractor_version": "1.0.0",
        "generation": None,
    }


def source_issue() -> Data:
    return {"code": "orphan_operation", "severity": "soft", "lineage": None, "message": "x"}


def item() -> Data:
    return {"id": "i1", "uom": "piece", "attributes": {}, "lineage": lineage("Item")}


def bom_line() -> Data:
    return {"item_id": "i2", "qty_per": 1.0, "uom": "piece"}


def bom() -> Data:
    return {
        "id": "b1",
        "item_id": "i1",
        "revision": "A",
        "lines": [bom_line()],
        "lineage": lineage("Bom"),
    }


def option() -> Data:
    return {
        "work_center_id": "wc1",
        "setup_seconds": 0.0,
        "run_seconds_per_unit": 1.0,
        "fixed_run_seconds": 0.0,
        "preference": 0,
        "lineage": lineage("Option"),
    }


def outside_process() -> Data:
    return {"vendor_id": "v1", "lead_seconds": 86_400.0}


def crew_need() -> Data:
    return {"labor_pool_id": "lp1", "size": 1}


def batch_spec() -> Data:
    return {"capacity": 10.0, "unit": "piece"}


def routing_operation(op_id: str = "10", sequence: int = 10) -> Data:
    return {
        "op_id": op_id,
        "sequence": sequence,
        "work_center_id": "wc1",
        "options": [option()],
        "setup_params": {},
        "queue_seconds": 0.0,
        "move_seconds": 0.0,
        "lineage": lineage("RoutingOp", "r1", op_id),
    }


def routing() -> Data:
    return {
        "id": "r1",
        "item_id": "i1",
        "operations": [routing_operation()],
        "lineage": lineage("Routing"),
    }


def order_operation(op_id: str = "10", sequence: int = 10) -> Data:
    return {
        **routing_operation(op_id, sequence),
        "status": "pending",
        "qty_to_produce": 5.0,
        "qty_complete": 0.0,
        "qty_scrapped": 0.0,
        "qty_remaining": 5.0,
        "lineage": lineage("OrderOp", "po1", op_id),
    }


def material_requirement() -> Data:
    return {
        "item_id": "i2",
        "qty_required": 5.0,
        "qty_issued": 0.0,
        "lineage": lineage("Material"),
    }


def sales_link() -> Data:
    return {"order_type": "SO", "order_nbr": "S1", "line_nbr": "1"}


def production_order() -> Data:
    return {
        "id": "po1",
        "order_type": "RO",
        "item_id": "i1",
        "quantity": 5.0,
        "status": "released",
        "rush": False,
        "hold": False,
        "sales_links": [],
        "operations": [order_operation()],
        "materials": [],
        "lineage": lineage("ProdOrder"),
    }


def sales_order_line() -> Data:
    return {
        "order_type": "SO",
        "order_nbr": "S1",
        "line_nbr": 1,
        "customer_id": "c1",
        "item_id": "i1",
        "qty_open": 5.0,
        "lineage": lineage("SOLine"),
    }


def customer() -> Data:
    return {"id": "c1", "lineage": lineage("Customer")}


def vendor() -> Data:
    return {"id": "v1", "lineage": lineage("Vendor")}


def receipt() -> Data:
    return {"receipt_nbr": "R1", "qty": 1.0, "received_at": AS_OF}


def purchase_line() -> Data:
    return {
        "order_type": "PO",
        "order_nbr": "P1",
        "line_nbr": "1",
        "vendor_id": "v1",
        "item_id": "i2",
        "qty_ordered": 10.0,
        "qty_received": 0.0,
        "status": "open",
        "receipts": [],
        "lineage": lineage("POLine"),
    }


def inventory_balance() -> Data:
    return {"item_id": "i2", "qty_available": 0.0, "lineage": lineage("Inventory")}


def overtime_window() -> Data:
    return {"start_minute": 900, "end_minute": 1020, "cost_multiplier": 1.5}


def weekday_overtime() -> Data:
    return {"weekday": 0, "windows": [overtime_window()]}


def overtime_exception(date: str = "2026-10-10") -> Data:
    return {"date": date, "windows": [overtime_window()]}


def overtime() -> Data:
    return {"week": [weekday_overtime()], "exceptions": [overtime_exception()]}


def work_center() -> Data:
    return {"id": "wc1", "attributes": {}, "lineage": lineage("WorkCenter")}


def machine() -> Data:
    return {
        "id": "m1",
        "work_center_id": "wc1",
        "efficiency": 1.0,
        "active": True,
        "lineage": lineage("Machine"),
    }


def time_window() -> Data:
    return {"start_minute": 360, "end_minute": 840}


def weekday_windows() -> Data:
    return {"weekday": 0, "windows": [time_window()]}


def calendar_exception(date: str = "2026-12-25") -> Data:
    return {"date": date, "windows": []}


def calendar() -> Data:
    return {
        "id": "cal1",
        "week": [weekday_windows()],
        "exceptions": [calendar_exception()],
        "lineage": lineage("Calendar"),
    }


def labor_pool() -> Data:
    return {"id": "lp1", "size": 1, "lineage": lineage("LaborPool")}


def snapshot() -> Data:
    """The smallest valid envelope: every collection present and empty, no digest."""
    return {
        "schema_version": "twinflow.erp/1",
        "as_of": AS_OF,
        "plant_timezone": PLANT_TZ,
        "source": source_info(),
        "source_issues": [],
        **{name: [] for name in COLLECTIONS},
    }


def full_snapshot() -> Data:
    """One record of every kind with most optional fields filled (digest and round trips)."""
    order = production_order()
    order["erp_priority"] = 3
    order["release_date"] = "2026-10-05"
    order["due_date"] = "2026-10-20"
    order["sales_links"] = [sales_link()]
    order["materials"] = [{**material_requirement(), "op_id": "10"}]
    in_process = order_operation("20", 20)
    in_process.update(
        status="in_process",
        qty_complete=2.0,
        qty_remaining=3.0,
        current_machine_id="m1",
        actual_start="2026-10-05T03:00:00-05:00",
        setup_params={"wire_diameter_mm": 1.2, "wire_family": "music"},
        crew=crew_need(),
        batch=batch_spec(),
    )
    outside = order_operation("30", 30)
    outside.update(options=[], outside=outside_process())
    order["operations"] = [order_operation(), in_process, outside]

    work = work_center()
    work.update(
        description="Coiling",
        calendar_id="cal1",
        attributes={"op_kind": "coil"},
        overtime=overtime(),
    )
    mach = machine()
    mach.update(description="Coiler 1", calendar_id="cal1", efficiency=0.9, overtime=overtime())
    part = item()
    part.update(
        description="Spring",
        item_class="FG",
        attributes={"wire_family": "music", "wire_diameter_mm": 1.2},
    )
    purchase = purchase_line()
    purchase.update(promised_date="2026-10-07", qty_received=1.0, receipts=[receipt()])
    sales = sales_order_line()
    sales.update(requested_date="2026-10-18", promised_date="2026-10-19")

    data = snapshot()
    data["source"]["generation"] = "42"
    data["source_issues"] = [{**source_issue(), "lineage": lineage("ProdOrder")}]
    data.update(
        items=[part, {**item(), "id": "i2"}],
        boms=[bom()],
        routings=[{**routing(), "revision": "A"}],
        work_centers=[work],
        machines=[mach],
        calendars=[{**calendar(), "description": "Two shift"}],
        labor_pools=[{**labor_pool(), "calendar_id": "cal1"}],
        production_orders=[order],
        sales_order_lines=[sales],
        customers=[{**customer(), "name": "Acme", "priority_class": "A-KEY"}],
        vendors=[{**vendor(), "name": "Platers"}],
        purchase_lines=[purchase],
        inventory=[{**inventory_balance(), "site_id": "MAIN"}],
    )
    return data


# Model name -> minimal payload factory. One row per public model in SDD COMP-110 and the
# COMP-126 overtime types. `test_schema.py` maps the names to the imported classes.
MINIMAL: dict[str, Callable[[], Data]] = {
    "ErpSnapshot": snapshot,
    "SourceInfo": source_info,
    "SourceIssue": source_issue,
    "Lineage": lineage,
    "Item": item,
    "Bom": bom,
    "BomLine": bom_line,
    "Routing": routing,
    "RoutingOperation": routing_operation,
    "OrderOperation": order_operation,
    "OperationOption": option,
    "OutsideProcess": outside_process,
    "CrewNeed": crew_need,
    "BatchSpec": batch_spec,
    "WorkCenter": work_center,
    "Machine": machine,
    "Calendar": calendar,
    "WeekdayWindows": weekday_windows,
    "TimeWindow": time_window,
    "CalendarException": calendar_exception,
    "LaborPool": labor_pool,
    "ProductionOrder": production_order,
    "MaterialRequirement": material_requirement,
    "SalesLink": sales_link,
    "SalesOrderLine": sales_order_line,
    "Customer": customer,
    "Vendor": vendor,
    "PurchaseLine": purchase_line,
    "Receipt": receipt,
    "InventoryBalance": inventory_balance,
    "Overtime": overtime,
    "WeekdayOvertime": weekday_overtime,
    "OvertimeWindow": overtime_window,
    "OvertimeException": overtime_exception,
}

# Models that must carry `lineage` (see module docstring).
LINEAGE_BEARING: tuple[str, ...] = (
    "Item",
    "Bom",
    "Routing",
    "RoutingOperation",
    "OrderOperation",
    "OperationOption",
    "WorkCenter",
    "Machine",
    "Calendar",
    "LaborPool",
    "ProductionOrder",
    "MaterialRequirement",
    "SalesOrderLine",
    "Customer",
    "Vendor",
    "PurchaseLine",
    "InventoryBalance",
)


def with_(base: Data, **changes: Any) -> Data:
    """A copy of a payload with top-level fields replaced."""
    return {**base, **changes}
