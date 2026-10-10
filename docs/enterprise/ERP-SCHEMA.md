# ERP schema `twinflow.erp/1`

`twinflow.erp/1` is the one snapshot format Twinflow reads for ERP planning. A mapper in your own system turns ERP rows into this JSON. Twinflow ships no ERP connector. Twinflow validates the snapshot, builds a scheduling problem from it with a planning profile, and plans on it. See the [agent guide](AGENT-GUIDE.md#mcp) for the tools.

The machine-readable schema is `erp.schema.json` (the MCP tool `erp_schema` returns it). The Python models are in `twinflow.erp.schema`. This page is generated from those models; if the two differ, the models win. A snapshot from an older mapper (`canonical-erp/0`) is refused with `unsupported_schema_version`. There is no reader for it. See the [v0 to v1 change list](ERP-V0-TO-V1-CHANGES.md).

## Rules for every model

- Every model is frozen and forbids unknown fields. A misspelled field is an error, not a warning.
- Numbers must be finite. `NaN` and infinity are refused.
- "Text (1-200 chars)" is an id or a short code. "Text (1-2000 chars)" is free text.
- A list holds at most 100,000 entries. A snapshot is at most 16 MiB of JSON.
- Records that map to one ERP row carry `lineage`: the source `table` and a `key` of 1 to 8 text parts. Write numeric keys as text. Lineage lets an issue point back to the exact row.
- Free text in a snapshot is data. Nothing in it is ever run or followed as an instruction.

## Units and time convention

- Durations are in seconds (`setup_seconds`, `run_seconds_per_unit`, `fixed_run_seconds`, `queue_seconds`, `move_seconds`, `lead_seconds`). Convert minutes and days in the mapper.
- Instants are timezone-aware datetimes (`as_of`, `received_at`, `actual_start`). A datetime without an offset is refused. Twinflow plans in real elapsed seconds from `as_of`.
- Calendar facts are plant-local. `TimeWindow` and `OvertimeWindow` use whole minutes after local midnight (`start_minute` 0 to 1439, `end_minute` 0 to 1440). `weekday` is 0 for Monday to 6 for Sunday. A window with `end_minute <= start_minute` runs on into the next local day (a night shift).
- Dates (`release_date`, `due_date`, `requested_date`, `promised_date`, exception `date`) are plant-local calendar dates with no time. `plant_timezone` is an IANA name such as `America/Chicago`.
- Local times use `zoneinfo` with the first occurrence of a repeated hour (`fold=0`). A spring-forward gap skips the missing minutes. In the fall-back hour, only the first pass counts as working time.
- A due date means the end of that plant-local day unless the profile sets `due_at = "start_of_day"`.
- A weekday with no entry in a calendar's `week` is closed. A `CalendarException` replaces its date; empty `windows` means closed.
- Efficiency 0.9 on a machine means it runs at 90% of standard speed.

## Digest rule

`digest` is optional. When present it must equal the SHA-256 of the canonical JSON of the snapshot without the `digest` field, as lowercase hex (`twinflow.erp.digest.snapshot_digest` computes it). A different value is refused with `digest_mismatch`.

Canonical JSON uses sorted keys, compact separators and ASCII-only output. Each top-level collection (`items`, `machines`, `production_orders`, and so on) is sorted by its records' canonical text. A mapper that emits the same rows in another order gets the same digest. Nested lists keep their order because it is content: an order's `operations`, a calendar's `windows`, a routing's `options`, an order's `materials`.

## Models

### `ErpSnapshot`

One extraction of the plant's ERP state as of an instant, in `twinflow.erp/1`.

| Field | Type | Required |
|---|---|---|
| `schema_version` | `twinflow.erp/1` | yes |
| `as_of` | aware datetime | yes |
| `plant_timezone` | text (1-200 chars) | yes |
| `source` | `SourceInfo` | yes |
| `digest` | text (64 lowercase hex) or null | no (default null) |
| `source_issues` | list of `SourceIssue` | yes |
| `items` | list of `Item` | yes |
| `boms` | list of `Bom` | yes |
| `routings` | list of `Routing` | yes |
| `work_centers` | list of `WorkCenter` | yes |
| `machines` | list of `Machine` | yes |
| `calendars` | list of `Calendar` | yes |
| `labor_pools` | list of `LaborPool` | yes |
| `production_orders` | list of `ProductionOrder` | yes |
| `sales_order_lines` | list of `SalesOrderLine` | yes |
| `customers` | list of `Customer` | yes |
| `vendors` | list of `Vendor` | yes |
| `purchase_lines` | list of `PurchaseLine` | yes |
| `inventory` | list of `InventoryBalance` | yes |

### `SourceInfo`

Which ERP instance and which extractor version produced the snapshot.

| Field | Type | Required |
|---|---|---|
| `system` | text (1-200 chars) | yes |
| `instance` | text (1-200 chars) | yes |
| `extractor` | text (1-200 chars) | yes |
| `extractor_version` | text (1-200 chars) | yes |
| `generation` | text (1-200 chars) or null | yes |

### `SourceIssue`

A problem the mapper found while extracting; copied into validation with its severity.

| Field | Type | Required |
|---|---|---|
| `code` | text (1-200 chars) | yes |
| `severity` | `hard` / `soft` | yes |
| `lineage` | `Lineage` or null | yes |
| `message` | text (1-2000 chars) | yes |

### `Lineage`

The ERP source row of a record: its table and its 1 to 8 key parts.

| Field | Type | Required |
|---|---|---|
| `table` | text (1-200 chars) | yes |
| `key` | list of text (1-200 chars) | yes |

### `Item`

A part number with its unit of measure and setup or family attributes.

| Field | Type | Required |
|---|---|---|
| `id` | text (1-200 chars) | yes |
| `description` | text (1-2000 chars) or null | no (default null) |
| `uom` | text (1-200 chars) | yes |
| `item_class` | text (1-200 chars) or null | no (default null) |
| `attributes` | map of text to (number or text) | yes |
| `lineage` | `Lineage` | yes |

### `Bom`

A bill of material revision for one item.

| Field | Type | Required |
|---|---|---|
| `id` | text (1-200 chars) | yes |
| `item_id` | text (1-200 chars) | yes |
| `revision` | text (1-200 chars) | yes |
| `lines` | list of `BomLine` | yes |
| `lineage` | `Lineage` | yes |

### `BomLine`

One component of a bill of material, optionally consumed at a named operation.

| Field | Type | Required |
|---|---|---|
| `item_id` | text (1-200 chars) | yes |
| `qty_per` | number > 0 | yes |
| `uom` | text (1-200 chars) | yes |
| `op_id` | text (1-200 chars) or null | no (default null) |

### `Routing`

The standard routing template for an item (used for new orders).

| Field | Type | Required |
|---|---|---|
| `id` | text (1-200 chars) | yes |
| `item_id` | text (1-200 chars) | yes |
| `revision` | text (1-200 chars) or null | no (default null) |
| `operations` | list of `RoutingOperation` | yes |
| `lineage` | `Lineage` | yes |

### `RoutingOperation`

A standard routing step; at least one option unless it is an outside process.

| Field | Type | Required |
|---|---|---|
| `op_id` | text (1-200 chars) | yes |
| `sequence` | integer >= 0 | yes |
| `work_center_id` | text (1-200 chars) | yes |
| `options` | list of `OperationOption` | yes |
| `setup_params` | map of text to (number or text) | yes |
| `queue_seconds` | number >= 0 | yes |
| `move_seconds` | number >= 0 | yes |
| `outside` | `OutsideProcess` or null | no (default null) |
| `crew` | `CrewNeed` or null | no (default null) |
| `batch` | `BatchSpec` or null | no (default null) |
| `lineage` | `Lineage` | yes |

### `OperationOption`

One way to run an operation; `machine_id` None means any active machine of the center.

| Field | Type | Required |
|---|---|---|
| `work_center_id` | text (1-200 chars) | yes |
| `machine_id` | text (1-200 chars) or null | no (default null) |
| `setup_seconds` | number >= 0 | yes |
| `run_seconds_per_unit` | number >= 0 | yes |
| `fixed_run_seconds` | number >= 0 | no (default `0.0`) |
| `preference` | integer >= 0 | yes |
| `lineage` | `Lineage` | yes |

### `OutsideProcess`

Work sent to a vendor; it occupies no plant machine for `lead_seconds`.

| Field | Type | Required |
|---|---|---|
| `vendor_id` | text (1-200 chars) | yes |
| `lead_seconds` | number >= 0 | yes |

### `CrewNeed`

Operators from one labor pool an operation needs while it runs.

| Field | Type | Required |
|---|---|---|
| `labor_pool_id` | text (1-200 chars) | yes |
| `size` | integer >= 1 | yes |

### `BatchSpec`

Batch capacity of an operation in `unit`.

| Field | Type | Required |
|---|---|---|
| `capacity` | number > 0 | yes |
| `unit` | text (1-200 chars) | yes |

### `WorkCenter`

A work center; its machines are listed separately and point back to it.

| Field | Type | Required |
|---|---|---|
| `id` | text (1-200 chars) | yes |
| `description` | text (1-2000 chars) or null | no (default null) |
| `calendar_id` | text (1-200 chars) or null | no (default null) |
| `attributes` | map of text to (number or text) | yes |
| `overtime` | `Overtime` or null | no (default null) |
| `lineage` | `Lineage` | yes |

### `Machine`

A machine inside a work center; `efficiency` 0.9 means it runs at 90% of standard.

| Field | Type | Required |
|---|---|---|
| `id` | text (1-200 chars) | yes |
| `work_center_id` | text (1-200 chars) | yes |
| `description` | text (1-2000 chars) or null | no (default null) |
| `calendar_id` | text (1-200 chars) or null | no (default null) |
| `efficiency` | number > 0 | no (default `1.0`) |
| `active` | boolean | yes |
| `overtime` | `Overtime` or null | no (default null) |
| `lineage` | `Lineage` | yes |

### `LaborPool`

A pool of interchangeable operators.

| Field | Type | Required |
|---|---|---|
| `id` | text (1-200 chars) | yes |
| `size` | integer >= 1 | yes |
| `calendar_id` | text (1-200 chars) or null | no (default null) |
| `lineage` | `Lineage` | yes |

### `Calendar`

A weekly working pattern plus date exceptions (unique dates); absent weekday = closed.

| Field | Type | Required |
|---|---|---|
| `id` | text (1-200 chars) | yes |
| `description` | text (1-2000 chars) or null | no (default null) |
| `week` | list of `WeekdayWindows` | yes |
| `exceptions` | list of `CalendarException` | yes |
| `lineage` | `Lineage` | yes |

### `WeekdayWindows`

The working windows of one weekday (Monday = 0).

| Field | Type | Required |
|---|---|---|
| `weekday` | integer 0..6 | yes |
| `windows` | list of `TimeWindow` | yes |

### `TimeWindow`

Plant-local working minutes of one day; `end_minute <= start_minute` crosses midnight.

| Field | Type | Required |
|---|---|---|
| `start_minute` | integer 0..1439 | yes |
| `end_minute` | integer 0..1440 | yes |

### `CalendarException`

The windows of one date, replacing its weekday; empty windows mean closed.

| Field | Type | Required |
|---|---|---|
| `date` | date | yes |
| `windows` | list of `TimeWindow` | yes |
| `description` | text (1-2000 chars) or null | no (default null) |

### `Overtime`

Overtime available on a work center or machine (a machine's replaces its center's).

| Field | Type | Required |
|---|---|---|
| `week` | list of `WeekdayOvertime` | yes |
| `exceptions` | list of `OvertimeException` | yes |

### `WeekdayOvertime`

The overtime windows of one weekday (Monday = 0).

| Field | Type | Required |
|---|---|---|
| `weekday` | integer 0..6 | yes |
| `windows` | list of `OvertimeWindow` | yes |

### `OvertimeWindow`

Plant-local overtime minutes of one day, priced at `cost_multiplier` (at least 1).

| Field | Type | Required |
|---|---|---|
| `start_minute` | integer 0..1439 | yes |
| `end_minute` | integer 0..1440 | yes |
| `cost_multiplier` | number >= 1 | yes |

### `OvertimeException`

The overtime windows of one date, replacing that weekday's overtime.

| Field | Type | Required |
|---|---|---|
| `date` | date | yes |
| `windows` | list of `OvertimeWindow` | yes |

### `ProductionOrder`

A production order with its operations (at least one, unique `op_id`) and materials.

| Field | Type | Required |
|---|---|---|
| `id` | text (1-200 chars) | yes |
| `order_type` | text (1-200 chars) | yes |
| `item_id` | text (1-200 chars) | yes |
| `quantity` | number > 0 | yes |
| `status` | `planned` / `released` / `in_process` / `on_hold` / `complete` / `closed` / `cancelled` | yes |
| `rush` | boolean | yes |
| `hold` | boolean | yes |
| `erp_priority` | integer or null | no (default null) |
| `release_date` | date or null | no (default null) |
| `due_date` | date or null | no (default null) |
| `sales_links` | list of `SalesLink` | yes |
| `operations` | list of `OrderOperation` | yes |
| `materials` | list of `MaterialRequirement` | yes |
| `lineage` | `Lineage` | yes |

### `OrderOperation`

A routing step on a released order, with its progress on the floor.

| Field | Type | Required |
|---|---|---|
| `op_id` | text (1-200 chars) | yes |
| `sequence` | integer >= 0 | yes |
| `work_center_id` | text (1-200 chars) | yes |
| `options` | list of `OperationOption` | yes |
| `setup_params` | map of text to (number or text) | yes |
| `queue_seconds` | number >= 0 | yes |
| `move_seconds` | number >= 0 | yes |
| `outside` | `OutsideProcess` or null | no (default null) |
| `crew` | `CrewNeed` or null | no (default null) |
| `batch` | `BatchSpec` or null | no (default null) |
| `lineage` | `Lineage` | yes |
| `status` | `pending` / `in_process` / `on_hold` / `complete` / `cancelled` | yes |
| `qty_to_produce` | number >= 0 | yes |
| `qty_complete` | number >= 0 | yes |
| `qty_scrapped` | number >= 0 | yes |
| `qty_remaining` | number >= 0 | yes |
| `current_machine_id` | text (1-200 chars) or null | no (default null) |
| `actual_start` | aware datetime or null | no (default null) |

### `MaterialRequirement`

Material an order consumes, optionally at a named operation.

| Field | Type | Required |
|---|---|---|
| `item_id` | text (1-200 chars) | yes |
| `op_id` | text (1-200 chars) or null | no (default null) |
| `qty_required` | number >= 0 | yes |
| `qty_issued` | number >= 0 | yes |
| `lineage` | `Lineage` | yes |

### `SalesLink`

A pointer from a production order to the sales order line it serves.

| Field | Type | Required |
|---|---|---|
| `order_type` | text (1-200 chars) | yes |
| `order_nbr` | text (1-200 chars) | yes |
| `line_nbr` | text (1-200 chars) | yes |

### `SalesOrderLine`

An open sales order line: the demand an order is due for.

| Field | Type | Required |
|---|---|---|
| `order_type` | text (1-200 chars) | yes |
| `order_nbr` | text (1-200 chars) | yes |
| `line_nbr` | integer >= 0 | yes |
| `customer_id` | text (1-200 chars) | yes |
| `item_id` | text (1-200 chars) | yes |
| `qty_open` | number >= 0 | yes |
| `requested_date` | date or null | no (default null) |
| `promised_date` | date or null | no (default null) |
| `lineage` | `Lineage` | yes |

### `Customer`

A customer and its priority class code.

| Field | Type | Required |
|---|---|---|
| `id` | text (1-200 chars) | yes |
| `name` | text (1-2000 chars) or null | no (default null) |
| `priority_class` | text (1-200 chars) or null | no (default null) |
| `lineage` | `Lineage` | yes |

### `Vendor`

A supplier of material or outside processing.

| Field | Type | Required |
|---|---|---|
| `id` | text (1-200 chars) | yes |
| `name` | text (1-2000 chars) or null | no (default null) |
| `lineage` | `Lineage` | yes |

### `PurchaseLine`

A purchase order line with its receipts so far.

| Field | Type | Required |
|---|---|---|
| `order_type` | text (1-200 chars) | yes |
| `order_nbr` | text (1-200 chars) | yes |
| `line_nbr` | text (1-200 chars) | yes |
| `vendor_id` | text (1-200 chars) | yes |
| `item_id` | text (1-200 chars) | yes |
| `qty_ordered` | number >= 0 | yes |
| `qty_received` | number >= 0 | yes |
| `status` | `open` / `closed` | yes |
| `promised_date` | date or null | no (default null) |
| `receipts` | list of `Receipt` | yes |
| `lineage` | `Lineage` | yes |

### `Receipt`

One receipt against a purchase line.

| Field | Type | Required |
|---|---|---|
| `receipt_nbr` | text (1-200 chars) | yes |
| `qty` | number > 0 | yes |
| `received_at` | aware datetime | yes |

### `InventoryBalance`

On-hand quantity of an item, optionally per site.

| Field | Type | Required |
|---|---|---|
| `item_id` | text (1-200 chars) | yes |
| `site_id` | text (1-200 chars) or null | no (default null) |
| `qty_available` | number >= 0 | yes |
| `lineage` | `Lineage` | yes |

### Notes on the models

- `ProductionOrder.operations` needs at least one operation, and each `op_id` in an order must be unique.
- A `RoutingOperation` or `OrderOperation` needs at least one `options` entry unless it has `outside`. An outside operation uses no plant machine.
- An `OperationOption` with `machine_id` null means any active machine of its work center. `preference` 0 is the primary option, 1 and up are alternates. The builder adds `alternate_penalty_seconds` from the profile when an alternate is used.
- `OrderOperation` extends `RoutingOperation` with progress on the floor. `qty_remaining` must not exceed `qty_to_produce`.
- A machine's `overtime` replaces its work center's overtime. `cost_multiplier` is at least 1 (1.5 for time and a half).
- `SalesLink.line_nbr` is text, because it is a key part. `SalesOrderLine.line_nbr` is an integer. Twinflow compares the two through text, so `"10"` links to line 10.
- `Receipt.qty` must be above 0. `PurchaseLine.status` is `open` or `closed`; only open lines count as supply.
- `Item.attributes`, `RoutingOperation.setup_params` and `WorkCenter.attributes` hold numbers or text. Keep numbers as numbers (for example `wire_diameter_mm`).

## Issue codes

The parse gate, the validator and the builder return issues. Each issue has a `code`, a `severity`, a `$`-rooted `path` into the snapshot and, when one record is to blame, its `lineage`. Messages describe the rule that failed and never repeat the rejected value.

- **hard** blocks the build. `build_problem_from_erp` returns a blocked result with the issues and no problem.
- **soft** never blocks. The build goes ahead and the issue stays in the report.

The validator returns every issue it finds, not the first one. Issues are sorted by severity (hard first), then path, then code. When the validator and the builder report the same code for the same row, the builder keeps the validator's copy.

| Code | Severity | Raised by | Meaning |
|---|---|---|---|
| `digest_mismatch` | hard | parse gate | supplied digest does not match the SHA-256 of the canonical snapshot JSON |
| `duplicate_key` | hard | validator | record repeats the key of an earlier record in this collection |
| `unknown_work_center` | hard | validator | work center id is not in the snapshot |
| `unknown_machine` | hard | validator | machine id is not in the snapshot |
| `unknown_calendar` | hard | validator | calendar id is not in the snapshot |
| `unknown_labor_pool` | hard | validator | labor pool id is not in the snapshot |
| `unknown_item` | hard | validator | item id is not in the snapshot |
| `machine_outside_work_center` | hard | validator | option machine belongs to another work center |
| `no_option` | hard | validator | open in-plant operation has no machine to run on |
| `no_active_option` | hard | validator, builder | every machine of this open operation is inactive |
| `qty_remaining_exceeds_order` | hard | validator | qty_remaining exceeds the order quantity |
| `batch_capacity_conflict` | hard | validator | batch capacity differs from the first batch capacity on this work center |
| `unknown_customer` | soft | validator | customer id is not in the snapshot |
| `unknown_vendor` | soft | validator | vendor id is not in the snapshot |
| `unknown_priority_class` | soft | validator | priority class is not in the profile class map |
| `sales_link_missing` | soft | validator | linked sales order line is not in the snapshot |
| `no_due_date` | soft | validator, builder | open order has no promised, requested or due date |
| `past_due_no_promise` | soft | validator | open order is past due and has no promised date |
| `calendar_no_working_time` | soft | validator | calendar has no working time in the horizon |
| `po_past_due` | soft | validator, builder | open purchase line was promised before the as_of date |
| `qty_inconsistent` | soft | validator | qty_complete plus qty_scrapped exceeds qty_to_produce |
| `inactive_machine_option` | soft | validator | some options of this open operation are inactive |
| `in_process_machine_inferred` | soft | builder | in-process operation names no machine; its most preferred active machine is assumed |
| `outside_process_last` | soft | builder | outside operation ends the route; its lead time is taken off the due date |
| `batch_lot_exceeds_capacity` | soft | builder | batch lot is larger than the batch capacity; it runs alone instead of batched |
| `no_routing` | hard | builder | no standard routing of this item matches the request |
| `builder_invariant` | hard | builder | the built request failed the TF-002 self-check |
| `unsupported_schema_version` | hard | parse gate | `schema_version` is not `twinflow.erp/1` (a `canonical-erp/0` snapshot gets a pointer to the change list) |
| `size_limit` | hard | parse gate | snapshot JSON is larger than 16 MiB and was not parsed |
| `schema` | hard | parse gate | one issue per model error, with a `$`-rooted path and no input value |
| `po_no_promise` | soft | builder | open purchase line has no promised date; it is not counted as supply |
| `material_short` | soft | builder | cumulative supply never covers a material requirement |

Codes in `source_issues` are the mapper's own. They are copied into the report with the severity the mapper gave. Use `hard` only for a fact the plan cannot go without, and `soft` for a fact you could default.

## Batch lots (batch-lot rule)

`BatchSpec.capacity` is the batch size in `BatchSpec.unit`. An open batch operation whose `qty_remaining` is no larger than the capacity is planned as a batch. If `qty_remaining` is larger than the capacity, the scheduling model cannot batch the lot. The builder then plans it alone, for its full time, and raises the soft issue `batch_lot_exceeds_capacity`. The work center's machines still get their batch capacity. Split such an order in the ERP, or accept the longer run time.

All open batch operations on one work center must use the same capacity. A different capacity is the hard issue `batch_capacity_conflict`.

## Mapping guidance

1. **Start from the ERP's own facts, not its schedule.** Planned start and end dates from the ERP are not inputs. Send `release_date` only for a firm release, and `due_date` as the ERP due date.
2. **Put operations inside the order.** Build each order's operations from its operation rows, sorted by operation id. Send standard `Routing` records only for new orders (rush orders use them).
3. **Use seconds.** Change setup, run, queue and move times to seconds in the mapper. Run time is seconds per unit of the order quantity.
4. **Model alternates as options.** Option 0 is the primary work center or machine. Each substitute is another option with the next `preference`.
5. **Send calendars as window lists.** One window per shift, one calendar per work center or machine. Closed days and short days are `CalendarException` records.
6. **Send overtime separately.** Put overtime windows with a `cost_multiplier` on the work center or machine. Set `use_overtime = false` in the profile to ignore them.
7. **Keep lineage.** Use the ERP table name and the key parts of the row. Without it an issue cannot point to its row.
8. **Never drop a row silently.** If a row cannot be mapped, add a `source_issues` entry with a code, a severity and the lineage, and leave the row out.
9. **Set the weights in the profile.** Customer priority classes map to weights in the planning profile (`[erp.class_weights]`). The snapshot carries only the class code.
10. **Validate before you build.** Call `validate_snapshot` (or `twinflow.erp.validate.validate_snapshot`) on the mapper's output. Zero hard issues is the gate.
