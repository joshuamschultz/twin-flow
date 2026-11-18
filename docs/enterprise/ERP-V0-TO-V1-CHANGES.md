# Canonical ERP v0 to v1: what the Western Spring mapper must change

**Source of v0:** `arc-western-spring-spec003`, branch `feat/SPEC-003-acumatica-mirror`, `connectors/acumatica/mapping/src/acumatica_mapping/canonical.py` (`SCHEMA_VERSION = "canonical-erp/0"`), `mapper.py`, `_operations.py`, `_resources.py`; SPEC-003 SDD COMP-066, COMP-067 and "Where shop facts live".
**Target:** `twinflow.erp/1` (`twinflow.erp.schema.ERP_SCHEMA_VERSION`; every model and field is listed in [ERP-SCHEMA.md](ERP-SCHEMA.md)). TwinFlow refuses `canonical-erp/0` with the hard issue `unsupported_schema_version`; there is no reader for v0 (no shim).
**Owner of the change:** the Western Spring instance (R-10). TwinFlow ships no Acumatica code.

## Summary

1. Import the models from `twinflow.erp` and delete `acumatica_mapping/canonical.py` in the same change (SPEC-003 D-103 already plans this).
2. Put each production order's operations inside the order (from `AMProdOper`). Stop emitting a `Routing` per `AMProdItem` and stop joining `OrderOperation` to routings by `op_id`.
3. Move times to seconds and onto options: `setup_minutes` becomes `setup_seconds` on each option; alternates become options with a preference rank.
4. Replace the one-window-per-day calendar with window lists per weekday and per exception date (two- and three-shift calendars).
5. Add the facts v0 lacked: overtime windows with a cost multiplier (the multiplier comes from an attribute, see the overtime row), order quantity, due date, material requirements, purchase receipts and received quantity, inventory, queue and move time, plant timezone, item attributes.
6. Replace `SourceRef` with `Lineage` (table + key tuple); move `generation` to the envelope `source`.
7. Carry mapping issues inside the snapshot as `source_issues` with a severity instead of returning them beside it.
8. Move the priority-class-to-weight mapping out of code into the planning profile `[erp.class_weights]`.
9. Add `order_type` to production orders, sales lines and purchase lines; keep line numbers as the types below say (`SalesLink.line_nbr` is text, `SalesOrderLine.line_nbr` is an integer).

## Field table

| v0 | v1 | Change | Western Spring action |
|---|---|---|---|
| `ErpSnapshot.schema_version` | `ErpSnapshot.schema_version` | "canonical-erp/0" to "twinflow.erp/1"; there is no default, so the field is required | Pass `"twinflow.erp/1"` explicitly (use `twinflow.erp.ERP_SCHEMA_VERSION`) |
| `ErpSnapshot.source_label` | `ErpSnapshot.source` | String to `SourceInfo{system, instance, extractor, extractor_version, generation}` | `system="acumatica"`, `instance`=tenant or "mirror", `extractor="acumatica_mapping"`, its version |
| `ErpSnapshot.generation` | `SourceInfo.generation` | int on envelope and every row to one text field on `source`. It is required-nullable: the key must be present, and its value may be `None` | Always write the key; give the mirror generation as text, or `None` if there is none. Leaving it out is a schema error |
| `ErpSnapshot.as_of` | `ErpSnapshot.as_of` | Must be timezone-aware | Pass an aware datetime |
| _(none)_ | `ErpSnapshot.plant_timezone` | New, required, a valid IANA name | Read from site settings (Western Spring: `America/Chicago` unless the site says otherwise) |
| _(none)_ | `ErpSnapshot.digest` | New, optional SHA-256 as 64 lowercase hex characters (uppercase or other lengths are refused) | Optional; compute with `twinflow.erp.digest.snapshot_digest` if you want end-to-end checking. The digest is the SHA-256 of canonical JSON with each top-level collection sorted by its records' canonical text, so row order in your output does not change it. A wrong value is refused with `digest_mismatch` |
| `MappingIssue` (returned beside the snapshot) | `ErpSnapshot.source_issues` (`SourceIssue`) | Carried inside, with `severity` and `lineage` | Put every issue in the snapshot; Suggested severities: an orphan operation, an unknown work center or a missing run time on an open order is `hard`, the same on a closed order `soft`; an unknown status is `hard`. These are your codes (the snapshot validator copies them with the severity you give) |
| `SourceRef{table, key, generation}` | `Lineage{table, key}` | `key` is always a tuple of text; no generation per row | String keys become 1-tuples |
| `Item{id, description}` | `Item{id, description, uom, item_class, attributes}` | Adds unit, class and attributes | `uom`=`BaseUnit`; `item_class`=`ItemClassCD` (the class code, a string; do not use `ItemClassID`, which is an integer); `attributes` from `CSAnswers` (`WIREFAM` as `wire_family`, `WIREDIA` as number `wire_diameter_mm`, `WIREGRADE` as `wire_grade`) |
| `Bom{id, revision, item, lines}` | `Bom{id, item_id, revision, lines}` | `item` renamed `item_id`; `revision` is required (unlike `Routing.revision`) | Rename; always write the BOM revision |
| `BomLine{item, qty, operation_id: int}` | `BomLine{item_id, qty_per, uom, op_id: str}` | `qty_per` must be > 0; the optional op link is the op id text, not the int | `qty_per`=`AMBomMatl.QtyReq`; `op_id`=`OperationCD` of the line's `OperationID` |
| `Routing{id, item, operations}` | `Routing{id, item_id, revision, operations}` | Standard templates only; `revision` is optional | Emit from `AMBomItem`/`AMBomOper` only; drop the routings built from `AMProdItem` |
| `RoutingOperation.op_id`, `.sequence`, `.work_center` | `RoutingOperation.op_id`, `.sequence`, `.work_center_id` | `work_center` renamed | Rename |
| `RoutingOperation.alternates: tuple[str]` | `RoutingOperation.options: tuple[OperationOption]` | Alternates gain times and a preference rank | Every option has its own required `work_center_id`. Option 0 = `WcID` with the op's times, `preference=0`; one option per `AMWCSubstitute` row with `preference` 1, 2, ... in substitute order, each with `work_center_id` set to its substitute center (a cross-center alternate), same times unless the substitute has its own |
| `RoutingOperation.setup_minutes` | `OperationOption.setup_seconds` | Minutes to seconds, moved onto each option | `SetupTime * 60` |
| `RoutingOperation.run_seconds_per_unit` | `OperationOption.run_seconds_per_unit` | Moved onto each option | Same rule (`RunUnitTime * 60 / RunUnits`) |
| _(none)_ | `OperationOption.machine_id` | New, optional | Leave `None` (work-center level) unless the shop records per-machine rates (`MachineUnits`/`MachineUnitTime`); then one option per machine |
| _(none)_ | `OperationOption.fixed_run_seconds` | New, default 0 | Leave 0 unless a fixed run time exists |
| `RoutingOperation.setup_key: dict[str, str]` | `RoutingOperation.setup_params: dict[str, float \| str]` | Numbers stay numbers | Required, default none: write `{}` when there are no setup parameters. `wire_diameter_mm` as a float; `wire_family`, `tooling` as text |
| `RoutingOperation.kind` | `WorkCenter.attributes["op_kind"]` | Moved to the work center (the fact lives on `AMWC`) | Write `OPKIND` on the work center, not the operation |
| `RoutingOperation.batch_size: int >= 0` | `RoutingOperation.batch: BatchSpec{capacity, unit}` | `capacity` must be > 0 (0 is no longer allowed); `unit` required. All open batch operations on one work center must share one capacity (`batch_capacity_conflict`, hard). An open batch operation whose `qty_remaining` is above the capacity is planned alone, for its full time, with the soft issue `batch_lot_exceeds_capacity` (DEV-3) | `OVENBATCH` > 0 gives `BatchSpec(capacity, unit="piece")`; blank or 0 gives `None`. Split a lot larger than the oven batch in the ERP, or accept the longer run. For an oven operation write the cycle as `fixed_run_seconds` and set `run_seconds_per_unit` to 0 (TF-002 refuses a batch operation that has a per-unit time). The builder gives a lot above `capacity` no batch family: it runs alone, and its time is still its options' times (setup plus `fixed_run_seconds` plus `run_seconds_per_unit` times the quantity, so with a per-unit time of 0 that is one oven cycle). Only a nonzero per-unit time makes a larger lot take longer (DEV-3) |
| `RoutingOperation.outside_process`, `.vendor_lead_days` | `RoutingOperation.outside: OutsideProcess{vendor_id, lead_seconds}` | One object; days to seconds; vendor required | `vendor_id`=`AMProdOper.VendorID`, `lead_seconds`=`VLeadTime * 86400`; an outside operation needs no options (any other operation needs at least one) |
| _(none)_ | `RoutingOperation.queue_seconds`, `.move_seconds` | New; both are required (write 0 when the ERP has none) | `QueueTime * 60`, `MoveTime * 60` from `AMBomOper`/`AMProdOper` |
| _(none)_ | `RoutingOperation.crew: CrewNeed{labor_pool_id, size}` | New, optional; `size` >= 1 | From `AMShift.CrewSize` when a crew pool is modelled; else `None` |
| `OrderOperation{order_id, op_id}` (separate list) | `ProductionOrder.operations: tuple[OrderOperation]` | Inline in the order (at least one, unique `op_id`); no `order_id` field | Build each order's ops from its `AMProdOper` rows, sorted by `OperationID` |
| _(none)_ | `OrderOperation.sequence`, `.work_center_id`, `.options`, `.setup_params`, `.queue_seconds`, `.move_seconds`, `.outside`, `.crew`, `.batch`, `.lineage` | An order operation carries its own times and resources (it extends the routing operation); no join to a routing | Fill them from the order's `AMProdOper` row, with the same rules as the routing rows above |
| `OrderOperation.status` (shared `Status`) | `OrderOperation.status` | Own enum: `pending`, `in_process`, `on_hold`, `complete`, `cancelled` | `P`,`R` to `pending`; `I` to `in_process`; `H` to `on_hold`; `M`,`C`,`L` to `complete`; `X` to `cancelled` |
| `OrderOperation.qty_to_produce`, `.qty_complete`, `.qty_scrapped`, `.qty_remaining` | `OrderOperation.qty_to_produce`, `.qty_complete`, `.qty_scrapped`, `.qty_remaining` | `qty_remaining <= qty_to_produce` enforced | Keep the existing computation |
| `OrderOperation.planned_start`, `.planned_end` | _(removed)_ | ERP's own schedule is not an input | Drop |
| _(none)_ | `OrderOperation.current_machine_id`, `.actual_start` | New, optional | `actual_start` (timezone-aware) from the earliest `AMMTran` labor transaction on the op, when present. `AMMTran` has no machine column, so `current_machine_id` is normally absent: leave it out. TwinFlow then infers a machine for an in-process operation (soft issue `in_process_machine_inferred`, or `in_progress_machine_unknown` when none is free; DEV-11). Write `current_machine_id` only if the shop records a real machine for the operation elsewhere |
| `ProductionOrder.order_id`, `.item` | `ProductionOrder.id`, `.item_id` | Renamed | Rename |
| `ProductionOrder.status` (shared `Status`) | `ProductionOrder.status` | Own enum: `planned`, `released`, `in_process`, `on_hold`, `complete`, `closed`, `cancelled` | `L` (locked) maps to `closed`; there is no `locked` |
| _(none)_ | `ProductionOrder.order_type` | New, required text | `AMProdItem.OrderType` |
| _(none)_ | `ProductionOrder.quantity` | New, required, must be > 0 | `AMProdItem.QtytoProd` |
| `ProductionOrder.end` | `ProductionOrder.due_date` | The ERP due date (a plant-local date), not the planned end | `AMProdItem.DueDate` (2026 R2 field) |
| `ProductionOrder.start` | `ProductionOrder.release_date` | Only a firm release date, else `None` | Leave `None` unless the order type uses a firm release; `StartDate` is the ERP's plan, not a constraint |
| `ProductionOrder.priority` | `ProductionOrder.erp_priority` | Informational only | `SchPriority` |
| `ProductionOrder.rush`, `.hold` | `ProductionOrder.rush`, `.hold` | Unchanged meaning; both are required strict booleans: only JSON `true` or `false` is accepted, not 0 or 1 | Keep `SchPriority == 1` and `StatusID == 'H' or Hold == 1` |
| `ProductionOrder.sales_link` | `ProductionOrder.sales_links: tuple[SalesLink]` | Tuple (an order can serve several lines); `SalesLink{order_type, order_nbr, line_nbr}` with `line_nbr` as text | Wrap the single link in a tuple; write the line number as text |
| _(none)_ | `ProductionOrder.materials: tuple[MaterialRequirement]`, `MaterialRequirement{item_id, op_id, qty_required, qty_issued}` | New; `op_id` is optional (none = needed at release) | From `AMProdMatl`: `item_id`, `op_id` (its `OperationCD`), `qty_required`=`TotalQtyRequired`, `qty_issued`=`QtyActual` |
| `SalesOrderLine.line` | `SalesOrderLine.line_nbr` | Renamed. `order_type`, `order_nbr`, `line_nbr` and `qty_open` are all required; `line_nbr` is an integer >= 0 here, unlike `SalesLink.line_nbr` and `PurchaseLine.line_nbr`, which are text. Twinflow compares them through text | Rename; keep the integer |
| `SalesOrderLine.customer`, `.item` | `SalesOrderLine.customer_id`, `.item_id` | Required now | Rename; a line without a customer is a hard source issue |
| `SalesOrderLine.requested`, `.promised` | `SalesOrderLine.requested_date`, `.promised_date` | Renamed. Each linked line is due on `promised_date`, else `requested_date`; an order is due on the earliest of these across all its linked lines, else its own `due_date`, and is promised on the earliest `promised_date` | `RequestDate`, `ShipDate` (assumption AS-2 unchanged) |
| `SalesOrderLine.priority` | _(removed)_ | Rush lives on the production order; weight comes from the customer class | Drop |
| `Customer.priority_class` | `Customer.priority_class` | Unchanged; the weight mapping moves to the planning profile | Keep `CustomerClassID`; put `A-KEY = 4`, `B-CORE = 2`, `C-SPOT = 1` in the instance's planning profile `[erp.class_weights]` |
| `Vendor{id, name}` | `Vendor{id, name}` | Unchanged | None |
| `PurchaseLine{order_type, order_nbr, line, item, vendor, qty_open, promised}` | `PurchaseLine{order_type, order_nbr, line_nbr, vendor_id, item_id, qty_ordered, qty_received, status, promised_date, receipts}`, `Receipt{receipt_nbr, qty, received_at}` | Ordered and received replace open; `status` is `open` or `closed`; receipts added (`qty` > 0, `received_at` timezone-aware); vendor required; `line_nbr` is text. An open line promised before the plant-local date of `as_of` is the soft issue `po_past_due`; one with no promised date is not counted as supply | `OrderQty`, `ReceivedQty`, `Completed`/`Cancelled` to `closed`; `receipts` from `POReceiptLine` (`ReceiptNbr` to `receipt_nbr`, `ReceiptQty` to `qty`, `ReceiptDate` to `received_at`, localized to plant tz) |
| _(none)_ | `ErpSnapshot.inventory: tuple[InventoryBalance]`, `InventoryBalance{item_id, site_id, qty_available}` | New; `site_id` optional | From `INSiteStatus.QtyAvail` per item and site; the C-2 mirror must include `INSiteStatus` (or `INLocationStatus`) if it does not yet |
| `WorkCenter{id, description, basis, shifts, machines, calendar}` | `WorkCenter{id, description, calendar_id, attributes}` | `basis`, `shifts`, `machines` removed (machines point to their work center) | Drop those fields; `calendar_id` from the work center's primary `AMShift.CalendarID`; `attributes` from `CSAnswers` on `AMWC.NoteID` (`op_kind`) |
| `Shift{id, crew_size, efficiency, calendar}` | `LaborPool{id, size, calendar_id}` | Shift entity removed; a crew becomes an optional labor pool | Emit a `LaborPool` only where crew limits matter (e.g. inspection); shift efficiency is not used |
| `Machine{id, description, work_center, efficiency, active}` | `Machine{id, work_center_id, description, calendar_id, efficiency, active}` | `work_center` required; `efficiency` must be > 0 | `AMWCMach.WcID`; `MachEff` as it is (it is already a fraction: 1.0 means 100%, so no `/ 100`); a machine in no work center is a hard source issue |
| `Calendar{id, description, week: CalendarDay{day, working, start, end, unpaid_minutes}}` | `Calendar{id, description, week: WeekdayWindows{weekday, windows: TimeWindow{start_minute, end_minute}}, exceptions}` | Several windows per day; weekday is 0 (Mon) to 6; minutes are plant-local after midnight (`start_minute` 0 to 1439, `end_minute` 0 to 1440; an end at or before the start runs into the next day); breaks are gaps between windows, not unpaid minutes; exception dates must be unique | One window per shift from `CSCalendar` day start/end plus each `AMShift` on that calendar; split a window at its break if the break time is known, else keep it whole |
| `CalendarException{date, working, description}` | `CalendarException{date, windows, description}` | `working: bool` becomes a window list (empty = closed); `date` is a plant-local date | Closed day: `windows=()`; a short day: its windows |
| _(none)_ | `WorkCenter.overtime`, `Machine.overtime` (`Overtime{week: WeekdayOvertime{weekday, windows: OvertimeWindow{start_minute, end_minute, cost_multiplier}}, exceptions: OvertimeException{date, windows}}`) | New, optional; a machine's overtime replaces its work center's; `cost_multiplier` >= 1; exception dates must be unique (a duplicate date is a schema error, DEV-2) | Overtime windows come from the weekday windows of the overtime shifts on the work center's calendar. `AMShiftMst` and `ShftDiff` do not exist in Acumatica 2026 R2, so do not map them. Until a real source is confirmed, read the `cost_multiplier` from an attribute (`CSAnswers`) answered on the shift or the work center (for example an `OTMULT` attribute, a number >= 1). If no such attribute exists, emit no overtime. Date-specific overtime has no confirmed source yet either. Confirm any DAC field against the C-2 manifest before coding; no invented columns (SPEC-003 D-094) |
| `ErpSnapshotSource` protocol | _(not in TwinFlow)_ | Transport is the instance's concern | Keep your own protocol in the instance, returning `twinflow.erp.ErpSnapshot` |
| `IssueCode` literal | `SourceIssue{code, severity, lineage, message}` | Open code text plus severity (`hard` or `soft`), lineage (or null) and a message | Keep your codes; add `severity`, `lineage` and `message` |

## Field requirements

Every field of every `twinflow.erp` model, with whether it must be present and its type. `required` means the key must be present. `required-nullable` means it must be present and may be `None`. `optional (default X)` means you may leave it out. A test checks this table against the models, so it cannot drift.

| Field | Req | Type |
|---|---|---|
| `Lineage.table` | required | str |
| `Lineage.key` | required | tuple[str] |
| `SourceInfo.system` | required | str |
| `SourceInfo.instance` | required | str |
| `SourceInfo.extractor` | required | str |
| `SourceInfo.extractor_version` | required | str |
| `SourceInfo.generation` | required-nullable | str |
| `SourceIssue.code` | required | str |
| `SourceIssue.severity` | required | Literal[hard, soft] |
| `SourceIssue.lineage` | required-nullable | Lineage |
| `SourceIssue.message` | required | str |
| `Item.id` | required | str |
| `Item.description` | optional (default None) | str |
| `Item.uom` | required | str |
| `Item.item_class` | optional (default None) | str |
| `Item.attributes` | required | dict[str, float \| str] |
| `Item.lineage` | required | Lineage |
| `BomLine.item_id` | required | str |
| `BomLine.qty_per` | required | float |
| `BomLine.uom` | required | str |
| `BomLine.op_id` | optional (default None) | str |
| `Bom.id` | required | str |
| `Bom.item_id` | required | str |
| `Bom.revision` | required | str |
| `Bom.lines` | required | tuple[BomLine] |
| `Bom.lineage` | required | Lineage |
| `OperationOption.work_center_id` | required | str |
| `OperationOption.machine_id` | optional (default None) | str |
| `OperationOption.setup_seconds` | required | float |
| `OperationOption.run_seconds_per_unit` | required | float |
| `OperationOption.fixed_run_seconds` | optional (default 0.0) | float |
| `OperationOption.preference` | required | int |
| `OperationOption.lineage` | required | Lineage |
| `OutsideProcess.vendor_id` | required | str |
| `OutsideProcess.lead_seconds` | required | float |
| `CrewNeed.labor_pool_id` | required | str |
| `CrewNeed.size` | required | int |
| `BatchSpec.capacity` | required | float |
| `BatchSpec.unit` | required | str |
| `RoutingOperation.op_id` | required | str |
| `RoutingOperation.sequence` | required | int |
| `RoutingOperation.work_center_id` | required | str |
| `RoutingOperation.options` | required | tuple[OperationOption] |
| `RoutingOperation.setup_params` | required | dict[str, float \| str] |
| `RoutingOperation.queue_seconds` | required | float |
| `RoutingOperation.move_seconds` | required | float |
| `RoutingOperation.outside` | optional (default None) | OutsideProcess |
| `RoutingOperation.crew` | optional (default None) | CrewNeed |
| `RoutingOperation.batch` | optional (default None) | BatchSpec |
| `RoutingOperation.lineage` | required | Lineage |
| `Routing.id` | required | str |
| `Routing.item_id` | required | str |
| `Routing.revision` | optional (default None) | str |
| `Routing.operations` | required | tuple[RoutingOperation] |
| `Routing.lineage` | required | Lineage |
| `OrderOperation.op_id` | required | str |
| `OrderOperation.sequence` | required | int |
| `OrderOperation.work_center_id` | required | str |
| `OrderOperation.options` | required | tuple[OperationOption] |
| `OrderOperation.setup_params` | required | dict[str, float \| str] |
| `OrderOperation.queue_seconds` | required | float |
| `OrderOperation.move_seconds` | required | float |
| `OrderOperation.outside` | optional (default None) | OutsideProcess |
| `OrderOperation.crew` | optional (default None) | CrewNeed |
| `OrderOperation.batch` | optional (default None) | BatchSpec |
| `OrderOperation.lineage` | required | Lineage |
| `OrderOperation.status` | required | Literal[pending, in_process, on_hold, complete, cancelled] |
| `OrderOperation.qty_to_produce` | required | float |
| `OrderOperation.qty_complete` | required | float |
| `OrderOperation.qty_scrapped` | required | float |
| `OrderOperation.qty_remaining` | required | float |
| `OrderOperation.current_machine_id` | optional (default None) | str |
| `OrderOperation.actual_start` | optional (default None) | aware datetime |
| `MaterialRequirement.item_id` | required | str |
| `MaterialRequirement.op_id` | optional (default None) | str |
| `MaterialRequirement.qty_required` | required | float |
| `MaterialRequirement.qty_issued` | required | float |
| `MaterialRequirement.lineage` | required | Lineage |
| `SalesLink.order_type` | required | str |
| `SalesLink.order_nbr` | required | str |
| `SalesLink.line_nbr` | required | str |
| `ProductionOrder.id` | required | str |
| `ProductionOrder.order_type` | required | str |
| `ProductionOrder.item_id` | required | str |
| `ProductionOrder.quantity` | required | float |
| `ProductionOrder.status` | required | Literal[planned, released, in_process, on_hold, complete, closed, cancelled] |
| `ProductionOrder.rush` | required | bool (strict) |
| `ProductionOrder.hold` | required | bool (strict) |
| `ProductionOrder.erp_priority` | optional (default None) | int |
| `ProductionOrder.release_date` | optional (default None) | date |
| `ProductionOrder.due_date` | optional (default None) | date |
| `ProductionOrder.sales_links` | required | tuple[SalesLink] |
| `ProductionOrder.operations` | required | tuple[OrderOperation] |
| `ProductionOrder.materials` | required | tuple[MaterialRequirement] |
| `ProductionOrder.lineage` | required | Lineage |
| `SalesOrderLine.order_type` | required | str |
| `SalesOrderLine.order_nbr` | required | str |
| `SalesOrderLine.line_nbr` | required | int |
| `SalesOrderLine.customer_id` | required | str |
| `SalesOrderLine.item_id` | required | str |
| `SalesOrderLine.qty_open` | required | float |
| `SalesOrderLine.requested_date` | optional (default None) | date |
| `SalesOrderLine.promised_date` | optional (default None) | date |
| `SalesOrderLine.lineage` | required | Lineage |
| `Customer.id` | required | str |
| `Customer.name` | optional (default None) | str |
| `Customer.priority_class` | optional (default None) | str |
| `Customer.lineage` | required | Lineage |
| `Vendor.id` | required | str |
| `Vendor.name` | optional (default None) | str |
| `Vendor.lineage` | required | Lineage |
| `Receipt.receipt_nbr` | required | str |
| `Receipt.qty` | required | float |
| `Receipt.received_at` | required | aware datetime |
| `PurchaseLine.order_type` | required | str |
| `PurchaseLine.order_nbr` | required | str |
| `PurchaseLine.line_nbr` | required | str |
| `PurchaseLine.vendor_id` | required | str |
| `PurchaseLine.item_id` | required | str |
| `PurchaseLine.qty_ordered` | required | float |
| `PurchaseLine.qty_received` | required | float |
| `PurchaseLine.status` | required | Literal[open, closed] |
| `PurchaseLine.promised_date` | optional (default None) | date |
| `PurchaseLine.receipts` | required | tuple[Receipt] |
| `PurchaseLine.lineage` | required | Lineage |
| `InventoryBalance.item_id` | required | str |
| `InventoryBalance.site_id` | optional (default None) | str |
| `InventoryBalance.qty_available` | required | float |
| `InventoryBalance.lineage` | required | Lineage |
| `TimeWindow.start_minute` | required | int |
| `TimeWindow.end_minute` | required | int |
| `WeekdayWindows.weekday` | required | int |
| `WeekdayWindows.windows` | required | tuple[TimeWindow] |
| `CalendarException.date` | required | date |
| `CalendarException.windows` | required | tuple[TimeWindow] |
| `CalendarException.description` | optional (default None) | str |
| `Calendar.id` | required | str |
| `Calendar.description` | optional (default None) | str |
| `Calendar.week` | required | tuple[WeekdayWindows] |
| `Calendar.exceptions` | required | tuple[CalendarException] |
| `Calendar.lineage` | required | Lineage |
| `OvertimeWindow.start_minute` | required | int |
| `OvertimeWindow.end_minute` | required | int |
| `OvertimeWindow.cost_multiplier` | required | float |
| `WeekdayOvertime.weekday` | required | int |
| `WeekdayOvertime.windows` | required | tuple[OvertimeWindow] |
| `OvertimeException.date` | required | date |
| `OvertimeException.windows` | required | tuple[OvertimeWindow] |
| `Overtime.week` | required | tuple[WeekdayOvertime] |
| `Overtime.exceptions` | required | tuple[OvertimeException] |
| `WorkCenter.id` | required | str |
| `WorkCenter.description` | optional (default None) | str |
| `WorkCenter.calendar_id` | optional (default None) | str |
| `WorkCenter.attributes` | required | dict[str, float \| str] |
| `WorkCenter.overtime` | optional (default None) | Overtime |
| `WorkCenter.lineage` | required | Lineage |
| `Machine.id` | required | str |
| `Machine.work_center_id` | required | str |
| `Machine.description` | optional (default None) | str |
| `Machine.calendar_id` | optional (default None) | str |
| `Machine.efficiency` | optional (default 1.0) | float |
| `Machine.active` | required | bool (strict) |
| `Machine.overtime` | optional (default None) | Overtime |
| `Machine.lineage` | required | Lineage |
| `LaborPool.id` | required | str |
| `LaborPool.size` | required | int |
| `LaborPool.calendar_id` | optional (default None) | str |
| `LaborPool.lineage` | required | Lineage |
| `ErpSnapshot.schema_version` | required | Literal[twinflow.erp/1] |
| `ErpSnapshot.as_of` | required | aware datetime |
| `ErpSnapshot.plant_timezone` | required | str |
| `ErpSnapshot.source` | required | SourceInfo |
| `ErpSnapshot.digest` | optional (default None) | sha256 hex |
| `ErpSnapshot.source_issues` | required | tuple[SourceIssue] |
| `ErpSnapshot.items` | required | tuple[Item] |
| `ErpSnapshot.boms` | required | tuple[Bom] |
| `ErpSnapshot.routings` | required | tuple[Routing] |
| `ErpSnapshot.work_centers` | required | tuple[WorkCenter] |
| `ErpSnapshot.machines` | required | tuple[Machine] |
| `ErpSnapshot.calendars` | required | tuple[Calendar] |
| `ErpSnapshot.labor_pools` | required | tuple[LaborPool] |
| `ErpSnapshot.production_orders` | required | tuple[ProductionOrder] |
| `ErpSnapshot.sales_order_lines` | required | tuple[SalesOrderLine] |
| `ErpSnapshot.customers` | required | tuple[Customer] |
| `ErpSnapshot.vendors` | required | tuple[Vendor] |
| `ErpSnapshot.purchase_lines` | required | tuple[PurchaseLine] |
| `ErpSnapshot.inventory` | required | tuple[InventoryBalance] |

## Rules the mapper often misses

- `lineage` is required on: `Bom`, `Calendar`, `Customer`, `InventoryBalance`, `Item`, `LaborPool`, `Machine`, `MaterialRequirement`, `OperationOption`, `OrderOperation`, `ProductionOrder`, `PurchaseLine`, `Routing`, `RoutingOperation`, `SalesOrderLine`, `Vendor`, `WorkCenter`. In `SourceIssue` it is required-nullable (the key is present; the value may be `None`).
- `SalesOrderLine` needs `order_type`, `order_nbr`, `line_nbr` and `qty_open`.
- `Bom.revision` is required. `RoutingOperation.queue_seconds` and `.move_seconds` are required. `OperationOption.work_center_id` is required on every option.
- `setup_params` (operations) and `attributes` (`Item`, `WorkCenter`) have no default: write `{}` when empty.
- `rush`, `hold` and `Machine.active` are strict booleans: `true` or `false` only, never 0 or 1.
- `OrderOperation.actual_start`, `Receipt.received_at` and `ErpSnapshot.as_of` must be timezone-aware.
- `digest` is 64 lowercase hex characters.
- `ErpPolicy` `default_class`, `stock_class` and `rush_class` live in the planning profile `[erp]` table next to `class_weights`; they are not in the snapshot.

## What does not change

- The row-source seam (`AcumaticaRowSource`) and its implementations stay in the instance.
- Status and rush rules (`SchPriority == 1`), customer classes and the "never drop silently" rule stay.
- The mirror's table names and fields (R-10, C-2) stay; only the mapper's output changes.

## Order of work for Western Spring

1. Pin the TwinFlow version that ships TF-005 in the instance.
2. Change the mapper per the field table; delete `canonical.py`; update `test_canonical.py` to import `twinflow.erp` (or delete it; TwinFlow owns the contract tests).
3. Run `twinflow.erp.validate.validate_snapshot` on the mirror's seed snapshot in the instance's tests; zero hard issues is the gate.
4. Add the planning profile `[erp]` table (class map, shortage policy) to the signed settings file (C-4).
