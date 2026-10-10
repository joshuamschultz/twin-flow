# Canonical ERP v0 to v1: what the Western Spring mapper must change

**Source of v0:** `arc-western-spring-spec003`, branch `feat/SPEC-003-acumatica-mirror`, `connectors/acumatica/mapping/src/acumatica_mapping/canonical.py` (`SCHEMA_VERSION = "canonical-erp/0"`), `mapper.py`, `_operations.py`, `_resources.py`; SPEC-003 SDD COMP-066, COMP-067 and "Where shop facts live".
**Target:** `twinflow.erp/1` (`twinflow.erp.schema.ERP_SCHEMA_VERSION`; every model and field is listed in [ERP-SCHEMA.md](ERP-SCHEMA.md)). TwinFlow refuses `canonical-erp/0` with the hard issue `unsupported_schema_version`; there is no reader for v0 (no shim).
**Owner of the change:** the Western Spring instance (R-10). TwinFlow ships no Acumatica code.

## Summary

1. Import the models from `twinflow.erp` and delete `acumatica_mapping/canonical.py` in the same change (SPEC-003 D-103 already plans this).
2. Put each production order's operations inside the order (from `AMProdOper`). Stop emitting a `Routing` per `AMProdItem` and stop joining `OrderOperation` to routings by `op_id`.
3. Move times to seconds and onto options: `setup_minutes` becomes `setup_seconds` on each option; alternates become options with a preference rank.
4. Replace the one-window-per-day calendar with window lists per weekday and per exception date (two- and three-shift calendars).
5. Add the facts v0 lacked: overtime windows with a cost multiplier (from `AMShift`/`AMWCSchd`), order quantity, due date, material requirements, purchase receipts and received quantity, inventory, queue and move time, plant timezone, item attributes.
6. Replace `SourceRef` with `Lineage` (table + key tuple); move `generation` to the envelope `source`.
7. Carry mapping issues inside the snapshot as `source_issues` with a severity instead of returning them beside it.
8. Move the priority-class-to-weight mapping out of code into the planning profile `[erp.class_weights]`.
9. Add `order_type` to production orders, sales lines and purchase lines; keep line numbers as the types below say (`SalesLink.line_nbr` is text, `SalesOrderLine.line_nbr` is an integer).

## Field table

| v0 | v1 | Change | Western Spring action |
|---|---|---|---|
| `ErpSnapshot.schema_version` | `ErpSnapshot.schema_version` | "canonical-erp/0" to "twinflow.erp/1" | Use the default from `twinflow.erp` |
| `ErpSnapshot.source_label` | `ErpSnapshot.source` | String to `SourceInfo{system, instance, extractor, extractor_version, generation}` | `system="acumatica"`, `instance`=tenant or "mirror", `extractor="acumatica_mapping"`, its version |
| `ErpSnapshot.generation` | `SourceInfo.generation` | int on envelope and every row to one nullable text field on `source` | Write the mirror generation once, as text |
| `ErpSnapshot.as_of` | `ErpSnapshot.as_of` | Must be timezone-aware | Pass an aware datetime |
| _(none)_ | `ErpSnapshot.plant_timezone` | New, required, a valid IANA name | Read from site settings (Western Spring: `America/Chicago` unless the site says otherwise) |
| _(none)_ | `ErpSnapshot.digest` | New, optional SHA-256 | Optional; compute with `twinflow.erp.digest.snapshot_digest` if you want end-to-end checking. The digest is the SHA-256 of canonical JSON with each top-level collection sorted by its records' canonical text, so row order in your output does not change it. A wrong value is refused with `digest_mismatch` |
| `MappingIssue` (returned beside the snapshot) | `ErpSnapshot.source_issues` (`SourceIssue`) | Carried inside, with `severity` and `lineage` | Put every issue in the snapshot; Suggested severities: an orphan operation, an unknown work center or a missing run time on an open order is `hard`, the same on a closed order `soft`; an unknown status is `hard`. These are your codes (the snapshot validator copies them with the severity you give) |
| `SourceRef{table, key, generation}` | `Lineage{table, key}` | `key` is always a tuple of text; no generation per row | String keys become 1-tuples |
| `Item{id, description}` | `Item{id, description, uom, item_class, attributes}` | Adds unit, class and attributes | `uom`=`BaseUnit`; `item_class`=`ItemClassID`; `attributes` from `CSAnswers` (`WIREFAM` as `wire_family`, `WIREDIA` as number `wire_diameter_mm`, `WIREGRADE` as `wire_grade`) |
| `Bom{id, revision, item, lines}` | `Bom{id, item_id, revision, lines}` | `item` renamed `item_id` | Rename |
| `BomLine{item, qty, operation_id: int}` | `BomLine{item_id, qty_per, uom, op_id: str}` | `qty_per` must be > 0; the optional op link is the op id text, not the int | `qty_per`=`AMBomMatl.QtyReq`; `op_id`=`OperationCD` of the line's `OperationID` |
| `Routing{id, item, operations}` | `Routing{id, item_id, revision, operations}` | Standard templates only; `revision` is optional | Emit from `AMBomItem`/`AMBomOper` only; drop the routings built from `AMProdItem` |
| `RoutingOperation.op_id`, `.sequence`, `.work_center` | `RoutingOperation.op_id`, `.sequence`, `.work_center_id` | `work_center` renamed | Rename |
| `RoutingOperation.alternates: tuple[str]` | `RoutingOperation.options: tuple[OperationOption]` | Alternates gain times and a preference rank | Option 0 = `WcID` with the op's times, `preference=0`; one option per `AMWCSubstitute` row with `preference` 1, 2, ... in substitute order, same times unless the substitute has its own |
| `RoutingOperation.setup_minutes` | `OperationOption.setup_seconds` | Minutes to seconds, moved onto each option | `SetupTime * 60` |
| `RoutingOperation.run_seconds_per_unit` | `OperationOption.run_seconds_per_unit` | Moved onto each option | Same rule (`RunUnitTime * 60 / RunUnits`) |
| _(none)_ | `OperationOption.machine_id` | New, optional | Leave `None` (work-center level) unless the shop records per-machine rates (`MachineUnits`/`MachineUnitTime`); then one option per machine |
| _(none)_ | `OperationOption.fixed_run_seconds` | New, default 0 | Leave 0 unless a fixed run time exists |
| `RoutingOperation.setup_key: dict[str, str]` | `RoutingOperation.setup_params: dict[str, float \| str]` | Numbers stay numbers | `wire_diameter_mm` as a float; `wire_family`, `tooling` as text |
| `RoutingOperation.kind` | `WorkCenter.attributes["op_kind"]` | Moved to the work center (the fact lives on `AMWC`) | Write `OPKIND` on the work center, not the operation |
| `RoutingOperation.batch_size: int >= 0` | `RoutingOperation.batch: BatchSpec{capacity, unit}` | `capacity` must be > 0 (0 is no longer allowed); `unit` required. All open batch operations on one work center must share one capacity (`batch_capacity_conflict`, hard). An open batch operation whose `qty_remaining` is above the capacity is planned alone, for its full time, with the soft issue `batch_lot_exceeds_capacity` (DEV-3) | `OVENBATCH` > 0 gives `BatchSpec(capacity, unit="piece")`; blank or 0 gives `None`. Split a lot larger than the oven batch in the ERP, or accept the longer run |
| `RoutingOperation.outside_process`, `.vendor_lead_days` | `RoutingOperation.outside: OutsideProcess{vendor_id, lead_seconds}` | One object; days to seconds; vendor required | `vendor_id`=`AMProdOper.VendorID`, `lead_seconds`=`VLeadTime * 86400`; an outside operation needs no options (any other operation needs at least one) |
| _(none)_ | `RoutingOperation.queue_seconds`, `.move_seconds` | New | `QueueTime * 60`, `MoveTime * 60` from `AMBomOper`/`AMProdOper` |
| _(none)_ | `RoutingOperation.crew: CrewNeed{labor_pool_id, size}` | New, optional; `size` >= 1 | From `AMShift.CrewSize` when a crew pool is modelled; else `None` |
| `OrderOperation{order_id, op_id}` (separate list) | `ProductionOrder.operations: tuple[OrderOperation]` | Inline in the order (at least one, unique `op_id`); no `order_id` field | Build each order's ops from its `AMProdOper` rows, sorted by `OperationID` |
| _(none)_ | `OrderOperation.sequence`, `.work_center_id`, `.options`, `.setup_params`, `.queue_seconds`, `.move_seconds`, `.outside`, `.crew`, `.batch`, `.lineage` | An order operation carries its own times and resources (it extends the routing operation); no join to a routing | Fill them from the order's `AMProdOper` row, with the same rules as the routing rows above |
| `OrderOperation.status` (shared `Status`) | `OrderOperation.status` | Own enum: `pending`, `in_process`, `on_hold`, `complete`, `cancelled` | `P`,`R` to `pending`; `I` to `in_process`; `H` to `on_hold`; `M`,`C`,`L` to `complete`; `X` to `cancelled` |
| `OrderOperation.qty_to_produce`, `.qty_complete`, `.qty_scrapped`, `.qty_remaining` | `OrderOperation.qty_to_produce`, `.qty_complete`, `.qty_scrapped`, `.qty_remaining` | `qty_remaining <= qty_to_produce` enforced | Keep the existing computation |
| `OrderOperation.planned_start`, `.planned_end` | _(removed)_ | ERP's own schedule is not an input | Drop |
| _(none)_ | `OrderOperation.current_machine_id`, `.actual_start` | New, optional | From the latest `AMMTran` labor/machine transaction on the op when present |
| `ProductionOrder.order_id`, `.item` | `ProductionOrder.id`, `.item_id` | Renamed | Rename |
| `ProductionOrder.status` (shared `Status`) | `ProductionOrder.status` | Own enum: `planned`, `released`, `in_process`, `on_hold`, `complete`, `closed`, `cancelled` | `L` (locked) maps to `closed`; there is no `locked` |
| _(none)_ | `ProductionOrder.order_type` | New, required text | `AMProdItem.OrderType` |
| _(none)_ | `ProductionOrder.quantity` | New, required, must be > 0 | `AMProdItem.QtytoProd` |
| `ProductionOrder.end` | `ProductionOrder.due_date` | The ERP due date (a plant-local date), not the planned end | `AMProdItem.DueDate` (2026 R2 field) |
| `ProductionOrder.start` | `ProductionOrder.release_date` | Only a firm release date, else `None` | Leave `None` unless the order type uses a firm release; `StartDate` is the ERP's plan, not a constraint |
| `ProductionOrder.priority` | `ProductionOrder.erp_priority` | Informational only | `SchPriority` |
| `ProductionOrder.rush`, `.hold` | `ProductionOrder.rush`, `.hold` | Unchanged meaning | Keep `SchPriority == 1` and `StatusID == 'H' or Hold == 1` |
| `ProductionOrder.sales_link` | `ProductionOrder.sales_links: tuple[SalesLink]` | Tuple (an order can serve several lines); `SalesLink{order_type, order_nbr, line_nbr}` with `line_nbr` as text | Wrap the single link in a tuple; write the line number as text |
| _(none)_ | `ProductionOrder.materials: tuple[MaterialRequirement]`, `MaterialRequirement{item_id, op_id, qty_required, qty_issued}` | New; `op_id` is optional (none = needed at release) | From `AMProdMatl`: `item_id`, `op_id` (its `OperationCD`), `qty_required`=`TotalQtyRequired`, `qty_issued`=`QtyActual` |
| `SalesOrderLine.line` | `SalesOrderLine.line_nbr` | Renamed; an integer >= 0 here, unlike `SalesLink.line_nbr` and `PurchaseLine.line_nbr`, which are text. Twinflow compares them through text | Rename; keep the integer |
| `SalesOrderLine.customer`, `.item` | `SalesOrderLine.customer_id`, `.item_id` | Required now | Rename; a line without a customer is a hard source issue |
| `SalesOrderLine.requested`, `.promised` | `SalesOrderLine.requested_date`, `.promised_date` | Renamed | `RequestDate`, `ShipDate` (assumption AS-2 unchanged) |
| `SalesOrderLine.priority` | _(removed)_ | Rush lives on the production order; weight comes from the customer class | Drop |
| `Customer.priority_class` | `Customer.priority_class` | Unchanged; the weight mapping moves to the planning profile | Keep `CustomerClassID`; put `A-KEY = 4`, `B-CORE = 2`, `C-SPOT = 1` in the instance's planning profile `[erp.class_weights]` |
| `Vendor{id, name}` | `Vendor{id, name}` | Unchanged | None |
| `PurchaseLine{order_type, order_nbr, line, item, vendor, qty_open, promised}` | `PurchaseLine{order_type, order_nbr, line_nbr, vendor_id, item_id, qty_ordered, qty_received, status, promised_date, receipts}`, `Receipt{receipt_nbr, qty, received_at}` | Ordered and received replace open; `status` is `open` or `closed`; receipts added (`qty` > 0, `received_at` timezone-aware); vendor required; `line_nbr` is text. An open line promised before the plant-local date of `as_of` is the soft issue `po_past_due`; one with no promised date is not counted as supply | `OrderQty`, `ReceivedQty`, `Completed`/`Cancelled` to `closed`; `receipts` from `POReceiptLine` (`ReceiptNbr` to `receipt_nbr`, `ReceiptQty` to `qty`, `ReceiptDate` to `received_at`, localized to plant tz) |
| _(none)_ | `ErpSnapshot.inventory: tuple[InventoryBalance]`, `InventoryBalance{item_id, site_id, qty_available}` | New; `site_id` optional | From `INSiteStatus.QtyAvail` per item and site; the C-2 mirror must include `INSiteStatus` (or `INLocationStatus`) if it does not yet |
| `WorkCenter{id, description, basis, shifts, machines, calendar}` | `WorkCenter{id, description, calendar_id, attributes}` | `basis`, `shifts`, `machines` removed (machines point to their work center) | Drop those fields; `calendar_id` from the work center's primary `AMShift.CalendarID`; `attributes` from `CSAnswers` on `AMWC.NoteID` (`op_kind`) |
| `Shift{id, crew_size, efficiency, calendar}` | `LaborPool{id, size, calendar_id}` | Shift entity removed; a crew becomes an optional labor pool | Emit a `LaborPool` only where crew limits matter (e.g. inspection); shift efficiency is not used |
| `Machine{id, description, work_center, efficiency, active}` | `Machine{id, work_center_id, description, calendar_id, efficiency, active}` | `work_center` required; `efficiency` must be > 0 | `AMWCMach.WcID`; `MachEff / 100`; a machine in no work center is a hard source issue |
| `Calendar{id, description, week: CalendarDay{day, working, start, end, unpaid_minutes}}` | `Calendar{id, description, week: WeekdayWindows{weekday, windows: TimeWindow{start_minute, end_minute}}, exceptions}` | Several windows per day; weekday is 0 (Mon) to 6; minutes are plant-local after midnight (`start_minute` 0 to 1439, `end_minute` 0 to 1440; an end at or before the start runs into the next day); breaks are gaps between windows, not unpaid minutes; exception dates must be unique | One window per shift from `CSCalendar` day start/end plus each `AMShift` on that calendar; split a window at its break if the break time is known, else keep it whole |
| `CalendarException{date, working, description}` | `CalendarException{date, windows, description}` | `working: bool` becomes a window list (empty = closed); `date` is a plant-local date | Closed day: `windows=()`; a short day: its windows |
| _(none)_ | `WorkCenter.overtime`, `Machine.overtime` (`Overtime{week: WeekdayOvertime{weekday, windows: OvertimeWindow{start_minute, end_minute, cost_multiplier}}, exceptions: OvertimeException{date, windows}}`) | New, optional; a machine's overtime replaces its work center's; `cost_multiplier` >= 1; exception dates must be unique (a duplicate date is a schema error, DEV-2) | Map from Acumatica: overtime shifts in `AMShift` (shift rows on the work center marked as overtime, assumption AS-3: a `CSAttribute` `OVERTIME` answered on the shift, or the shift's differential type) with their calendar's weekday windows; `cost_multiplier` = 1 + the shift differential (`AMShiftMst` / `AMShift` `ShftDiff` as a percent, or the premium rate divided by the base labor rate); date-specific overtime from `AMWCSchd` (work-center schedule rows for a date) or the shift calendar's `CSCalendarExceptions`. Confirm the exact DAC fields against the C-2 manifest before coding; no invented columns (SPEC-003 D-094) |
| `ErpSnapshotSource` protocol | _(not in TwinFlow)_ | Transport is the instance's concern | Keep your own protocol in the instance, returning `twinflow.erp.ErpSnapshot` |
| `IssueCode` literal | `SourceIssue{code, severity, lineage, message}` | Open code text plus severity (`hard` or `soft`), lineage (or null) and a message | Keep your codes; add `severity`, `lineage` and `message` |

## What does not change

- The row-source seam (`AcumaticaRowSource`) and its implementations stay in the instance.
- Status and rush rules (`SchPriority == 1`), customer classes and the "never drop silently" rule stay.
- The mirror's table names and fields (R-10, C-2) stay; only the mapper's output changes.

## Order of work for Western Spring

1. Pin the TwinFlow version that ships TF-005 in the instance.
2. Change the mapper per the field table; delete `canonical.py`; update `test_canonical.py` to import `twinflow.erp` (or delete it; TwinFlow owns the contract tests).
3. Run `twinflow.erp.validate.validate_snapshot` on the mirror's seed snapshot in the instance's tests; zero hard issues is the gate.
4. Add the planning profile `[erp]` table (class map, shortage policy) to the signed settings file (C-4).
