"""Canonical ERP snapshot contract `twinflow.erp/1` (COMP-110, COMP-126 ERP side).

Every type is a frozen Pydantic v2 model that forbids unknown fields and rejects
non-finite numbers. Collections are tuples of at most 100,000 entries. Ids and free text
reuse the TF-002 `Id` and `Text` bounds (1 to 200 and 1 to 2,000 characters). Instants
are timezone-aware; calendar facts are plant-local minutes and dates. Every record that
has its own row in the SDD Data Model carries `lineage` back to its ERP source row.

This module checks fields and rules local to one record only (bounds, statuses,
`qty_remaining <= qty_to_produce`, an option unless outside, unique keys inside one
record). Rules that span records (unknown ids, duplicates across a collection) belong to
the snapshot validator (COMP-112).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Hashable, Iterable
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)

from twinflow.scheduling.model import Id, NonNegFloat, PosFloat, Text

ERP_SCHEMA_VERSION = "twinflow.erp/1"

MAX_RECORDS = 100_000
MAX_LINEAGE_KEY_PARTS = 8
MINUTES_PER_DAY = 1440

Severity = Literal["hard", "soft"]
AttributeValue = float | Text
Attributes = dict[Id, AttributeValue]
StartMinute = Annotated[int, Field(ge=0, le=MINUTES_PER_DAY - 1)]
EndMinute = Annotated[int, Field(ge=0, le=MINUTES_PER_DAY)]
Weekday = Annotated[int, Field(ge=0, le=6)]
NonNegInt = Annotated[int, Field(ge=0)]
PosInt = Annotated[int, Field(ge=1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _known_timezone(name: str) -> str:
    """Accept only an IANA zone `zoneinfo` can load; never echo the rejected name."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # ValueError: path-like or malformed keys; OSError: a key naming a tzdata directory.
        raise ValueError("unknown IANA time zone") from None
    return name


PlantTimezone = Annotated[Id, AfterValidator(_known_timezone)]


def _require_unique(values: Iterable[Hashable], what: str) -> None:
    """Raise when `values` repeats; the message names the field, not the repeated value."""
    seen: set[Hashable] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what}")
        seen.add(value)


class _Contract(BaseModel):
    """Shared config: immutable, closed to unknown fields, finite floats only."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


# --- envelope -------------------------------------------------------------------------------


class Lineage(_Contract):
    """The ERP source row of a record: its table and its 1 to 8 key parts."""

    table: Id
    key: tuple[Id, ...] = Field(min_length=1, max_length=MAX_LINEAGE_KEY_PARTS)


class SourceInfo(_Contract):
    """Which ERP instance and which extractor version produced the snapshot."""

    system: Id
    instance: Id
    extractor: Id
    extractor_version: Id
    generation: Id | None


class SourceIssue(_Contract):
    """A problem the mapper found while extracting; copied into validation with its severity."""

    code: Id
    severity: Severity
    lineage: Lineage | None
    message: Text


# --- items and bills of material --------------------------------------------------------------


class Item(_Contract):
    """A part number with its unit of measure and setup or family attributes."""

    id: Id
    description: Text | None = None
    uom: Id
    item_class: Id | None = None
    attributes: Attributes
    lineage: Lineage


class BomLine(_Contract):
    """One component of a bill of material, optionally consumed at a named operation."""

    item_id: Id
    qty_per: PosFloat
    uom: Id
    op_id: Id | None = None


class Bom(_Contract):
    """A bill of material revision for one item."""

    id: Id
    item_id: Id
    revision: Id
    lines: tuple[BomLine, ...] = Field(max_length=MAX_RECORDS)
    lineage: Lineage


# --- operations and routings -----------------------------------------------------------------


class OperationOption(_Contract):
    """One way to run an operation; `machine_id` None means any active machine of the center."""

    work_center_id: Id
    machine_id: Id | None = None
    setup_seconds: NonNegFloat
    run_seconds_per_unit: NonNegFloat
    fixed_run_seconds: NonNegFloat = 0.0
    preference: NonNegInt
    lineage: Lineage


class OutsideProcess(_Contract):
    """Work sent to a vendor; it occupies no plant machine for `lead_seconds`."""

    vendor_id: Id
    lead_seconds: NonNegFloat


class CrewNeed(_Contract):
    """Operators from one labor pool an operation needs while it runs."""

    labor_pool_id: Id
    size: PosInt


class BatchSpec(_Contract):
    """Batch capacity of an operation in `unit`."""

    capacity: PosFloat
    unit: Id


class RoutingOperation(_Contract):
    """A standard routing step; at least one option unless it is an outside process."""

    op_id: Id
    sequence: NonNegInt
    work_center_id: Id
    options: tuple[OperationOption, ...] = Field(max_length=MAX_RECORDS)
    setup_params: Attributes
    queue_seconds: NonNegFloat
    move_seconds: NonNegFloat
    outside: OutsideProcess | None = None
    crew: CrewNeed | None = None
    batch: BatchSpec | None = None
    lineage: Lineage

    @model_validator(mode="after")
    def _option_or_outside(self) -> Self:
        if not self.options and self.outside is None:
            raise ValueError("an operation needs at least one option unless it is outside")
        return self


class Routing(_Contract):
    """The standard routing template for an item (used for new orders)."""

    id: Id
    item_id: Id
    revision: Id | None = None
    operations: tuple[RoutingOperation, ...] = Field(max_length=MAX_RECORDS)
    lineage: Lineage


class OrderOperation(RoutingOperation):
    """A routing step on a released order, with its progress on the floor."""

    status: Literal["pending", "in_process", "on_hold", "complete", "cancelled"]
    qty_to_produce: NonNegFloat
    qty_complete: NonNegFloat
    qty_scrapped: NonNegFloat
    qty_remaining: NonNegFloat
    current_machine_id: Id | None = None
    actual_start: AwareDatetime | None = None

    @model_validator(mode="after")
    def _remaining_within_order(self) -> Self:
        if self.qty_remaining > self.qty_to_produce:
            raise ValueError("qty_remaining exceeds qty_to_produce")
        return self


# --- orders ------------------------------------------------------------------------------------


class MaterialRequirement(_Contract):
    """Material an order consumes, optionally at a named operation."""

    item_id: Id
    op_id: Id | None = None
    qty_required: NonNegFloat
    qty_issued: NonNegFloat
    lineage: Lineage


class SalesLink(_Contract):
    """A pointer from a production order to the sales order line it serves."""

    order_type: Id
    order_nbr: Id
    line_nbr: Id


class ProductionOrder(_Contract):
    """A production order with its operations (at least one, unique `op_id`) and materials."""

    id: Id
    order_type: Id
    item_id: Id
    quantity: PosFloat
    status: Literal[
        "planned", "released", "in_process", "on_hold", "complete", "closed", "cancelled"
    ]
    rush: StrictBool
    hold: StrictBool
    erp_priority: int | None = None
    release_date: dt.date | None = None
    due_date: dt.date | None = None
    sales_links: tuple[SalesLink, ...] = Field(max_length=MAX_RECORDS)
    operations: tuple[OrderOperation, ...] = Field(min_length=1, max_length=MAX_RECORDS)
    materials: tuple[MaterialRequirement, ...] = Field(max_length=MAX_RECORDS)
    lineage: Lineage

    @model_validator(mode="after")
    def _unique_op_ids(self) -> Self:
        _require_unique((op.op_id for op in self.operations), "op_id in operations")
        return self


class SalesOrderLine(_Contract):
    """An open sales order line: the demand an order is due for."""

    order_type: Id
    order_nbr: Id
    line_nbr: NonNegInt
    customer_id: Id
    item_id: Id
    qty_open: NonNegFloat
    requested_date: dt.date | None = None
    promised_date: dt.date | None = None
    lineage: Lineage


class Customer(_Contract):
    """A customer and its priority class code."""

    id: Id
    name: Text | None = None
    priority_class: Id | None = None
    lineage: Lineage


class Vendor(_Contract):
    """A supplier of material or outside processing."""

    id: Id
    name: Text | None = None
    lineage: Lineage


class Receipt(_Contract):
    """One receipt against a purchase line."""

    receipt_nbr: Id
    qty: PosFloat
    received_at: AwareDatetime


class PurchaseLine(_Contract):
    """A purchase order line with its receipts so far."""

    order_type: Id
    order_nbr: Id
    line_nbr: Id
    vendor_id: Id
    item_id: Id
    qty_ordered: NonNegFloat
    qty_received: NonNegFloat
    status: Literal["open", "closed"]
    promised_date: dt.date | None = None
    receipts: tuple[Receipt, ...] = Field(max_length=MAX_RECORDS)
    lineage: Lineage


class InventoryBalance(_Contract):
    """On-hand quantity of an item, optionally per site."""

    item_id: Id
    site_id: Id | None = None
    qty_available: NonNegFloat
    lineage: Lineage


# --- calendars and overtime (COMP-126) --------------------------------------------------------


class TimeWindow(_Contract):
    """Plant-local working minutes of one day; `end_minute < start_minute` crosses midnight."""

    start_minute: StartMinute
    end_minute: EndMinute


class WeekdayWindows(_Contract):
    """The working windows of one weekday (Monday = 0)."""

    weekday: Weekday
    windows: tuple[TimeWindow, ...] = Field(max_length=MAX_RECORDS)


class CalendarException(_Contract):
    """The windows of one date, replacing its weekday; empty windows mean closed."""

    date: dt.date
    windows: tuple[TimeWindow, ...] = Field(max_length=MAX_RECORDS)
    description: Text | None = None


class Calendar(_Contract):
    """A weekly working pattern plus date exceptions (unique dates); absent weekday = closed."""

    id: Id
    description: Text | None = None
    week: tuple[WeekdayWindows, ...] = Field(max_length=MAX_RECORDS)
    exceptions: tuple[CalendarException, ...] = Field(max_length=MAX_RECORDS)
    lineage: Lineage

    @model_validator(mode="after")
    def _unique_exception_dates(self) -> Self:
        _require_unique((exc.date for exc in self.exceptions), "exception date")
        return self


class OvertimeWindow(_Contract):
    """Plant-local overtime minutes of one day, priced at `cost_multiplier` (at least 1)."""

    start_minute: StartMinute
    end_minute: EndMinute
    cost_multiplier: float = Field(ge=1)


class WeekdayOvertime(_Contract):
    """The overtime windows of one weekday (Monday = 0)."""

    weekday: Weekday
    windows: tuple[OvertimeWindow, ...] = Field(max_length=MAX_RECORDS)


class OvertimeException(_Contract):
    """The overtime windows of one date, replacing that weekday's overtime."""

    date: dt.date
    windows: tuple[OvertimeWindow, ...] = Field(max_length=MAX_RECORDS)


class Overtime(_Contract):
    """Overtime available on a work center or machine (a machine's replaces its center's)."""

    week: tuple[WeekdayOvertime, ...] = Field(max_length=MAX_RECORDS)
    exceptions: tuple[OvertimeException, ...] = Field(max_length=MAX_RECORDS)

    @model_validator(mode="after")
    def _unique_exception_dates(self) -> Self:
        _require_unique((exc.date for exc in self.exceptions), "overtime exception date")
        return self


# --- resources ---------------------------------------------------------------------------------


class WorkCenter(_Contract):
    """A work center; its machines are listed separately and point back to it."""

    id: Id
    description: Text | None = None
    calendar_id: Id | None = None
    attributes: Attributes
    overtime: Overtime | None = None
    lineage: Lineage


class Machine(_Contract):
    """A machine inside a work center; `efficiency` 0.9 means it runs at 90% of standard."""

    id: Id
    work_center_id: Id
    description: Text | None = None
    calendar_id: Id | None = None
    efficiency: PosFloat = 1.0
    active: StrictBool
    overtime: Overtime | None = None
    lineage: Lineage


class LaborPool(_Contract):
    """A pool of interchangeable operators."""

    id: Id
    size: PosInt
    calendar_id: Id | None = None
    lineage: Lineage


# --- snapshot ----------------------------------------------------------------------------------


class ErpSnapshot(_Contract):
    """One extraction of the plant's ERP state as of an instant, in `twinflow.erp/1`."""

    schema_version: Literal["twinflow.erp/1"]
    as_of: AwareDatetime
    plant_timezone: PlantTimezone
    source: SourceInfo
    digest: Digest | None = None
    source_issues: tuple[SourceIssue, ...] = Field(max_length=MAX_RECORDS)
    items: tuple[Item, ...] = Field(max_length=MAX_RECORDS)
    boms: tuple[Bom, ...] = Field(max_length=MAX_RECORDS)
    routings: tuple[Routing, ...] = Field(max_length=MAX_RECORDS)
    work_centers: tuple[WorkCenter, ...] = Field(max_length=MAX_RECORDS)
    machines: tuple[Machine, ...] = Field(max_length=MAX_RECORDS)
    calendars: tuple[Calendar, ...] = Field(max_length=MAX_RECORDS)
    labor_pools: tuple[LaborPool, ...] = Field(max_length=MAX_RECORDS)
    production_orders: tuple[ProductionOrder, ...] = Field(max_length=MAX_RECORDS)
    sales_order_lines: tuple[SalesOrderLine, ...] = Field(max_length=MAX_RECORDS)
    customers: tuple[Customer, ...] = Field(max_length=MAX_RECORDS)
    vendors: tuple[Vendor, ...] = Field(max_length=MAX_RECORDS)
    purchase_lines: tuple[PurchaseLine, ...] = Field(max_length=MAX_RECORDS)
    inventory: tuple[InventoryBalance, ...] = Field(max_length=MAX_RECORDS)


def snapshot_json_schema() -> dict[str, Any]:
    """JSON Schema of `ErpSnapshot`, the source of the shipped `erp.schema.json`."""
    return ErpSnapshot.model_json_schema()
