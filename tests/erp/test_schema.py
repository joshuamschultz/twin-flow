"""Contract tests for the canonical ERP snapshot schema v1 (T-193, SDD COMP-110, COMP-126).

These tests pin the public model names, field names and field-local bounds of
`twinflow.erp.schema`. They assert observable validation behavior only: what parses, what
is rejected (and at which field), JSON round-trips, immutability and the schema hook.
Referential rules (unknown ids, duplicates across records) belong to the validator
(COMP-112) and are not tested here.
Requirements: REQ-145, REQ-146, REQ-147, REQ-148, REQ-151, REQ-182.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable

import pytest
from _factories import (
    COLLECTIONS,
    LINEAGE_BEARING,
    MINIMAL,
    Data,
    calendar,
    calendar_exception,
    full_snapshot,
    lineage,
    machine,
    material_requirement,
    option,
    order_operation,
    overtime,
    overtime_exception,
    production_order,
    purchase_line,
    receipt,
    routing_operation,
    snapshot,
    source_issue,
    vendor,
    with_,
)
from pydantic import BaseModel, ValidationError

import twinflow.erp.schema as schema
from twinflow.erp.schema import ERP_SCHEMA_VERSION, ErpSnapshot, snapshot_json_schema

NON_FINITE = [math.nan, math.inf, -math.inf]
ID_LIMIT = 200
TEXT_LIMIT = 2000
COLLECTION_LIMIT = 100_000

MODELS: list[tuple[type[BaseModel], Callable[[], Data]]] = [
    (getattr(schema, name), factory) for name, factory in MINIMAL.items()
]
MODEL_IDS = list(MINIMAL)


# --- helpers --------------------------------------------------------------------------------


def assert_rejected(cls: type[BaseModel], data: Data, field: str | int | None = None) -> None:
    """Assert validation fails; when `field` is given, some error location must name it."""
    with pytest.raises(ValidationError) as caught:
        cls.model_validate(data)
    if field is not None:
        locations = [err["loc"] for err in caught.value.errors()]
        assert any(field in loc for loc in locations), locations


def snapshot_with(collection: str, record: Data) -> Data:
    """The minimal envelope holding exactly one record in `collection`."""
    return with_(snapshot(), **{collection: [record]})


# --- module constants and schema hook ---------------------------------------------------------


def test_erp_schema_version_constant_is_twinflow_erp_1() -> None:
    assert ERP_SCHEMA_VERSION == "twinflow.erp/1"


def test_snapshot_json_schema_describes_the_envelope() -> None:
    generated = snapshot_json_schema()
    assert isinstance(generated, dict)
    assert generated["title"] == "ErpSnapshot"
    assert set(generated["properties"]) == {
        "schema_version",
        "as_of",
        "plant_timezone",
        "source",
        "digest",
        "source_issues",
        *COLLECTIONS,
    }
    assert snapshot_json_schema() == generated


def test_snapshot_schema_version_accepts_only_v1() -> None:
    parsed = ErpSnapshot.model_validate(snapshot())
    assert parsed.schema_version == "twinflow.erp/1"
    assert_rejected(ErpSnapshot, with_(snapshot(), schema_version="twinflow.erp/2"))
    assert_rejected(ErpSnapshot, with_(snapshot(), schema_version="canonical-erp/0"))


# --- generic contract: every model ------------------------------------------------------------


@pytest.mark.parametrize(("cls", "factory"), MODELS, ids=MODEL_IDS)
def test_model_minimal_payload_validates(cls: type[BaseModel], factory: Callable[[], Data]) -> None:
    assert isinstance(cls.model_validate(factory()), cls)


@pytest.mark.parametrize(("cls", "factory"), MODELS, ids=MODEL_IDS)
def test_model_json_round_trip_is_identity(
    cls: type[BaseModel], factory: Callable[[], Data]
) -> None:
    original = cls.model_validate(factory())
    assert cls.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize(("cls", "factory"), MODELS, ids=MODEL_IDS)
def test_model_unknown_field_rejected(cls: type[BaseModel], factory: Callable[[], Data]) -> None:
    assert_rejected(cls, with_(factory(), bogus=1), "bogus")


@pytest.mark.parametrize(("cls", "factory"), MODELS, ids=MODEL_IDS)
def test_model_is_frozen(cls: type[BaseModel], factory: Callable[[], Data]) -> None:
    instance = cls.model_validate(factory())
    field = next(iter(cls.model_fields))
    with pytest.raises(ValidationError):
        setattr(instance, field, getattr(instance, field))


@pytest.mark.parametrize("name", LINEAGE_BEARING)
def test_record_without_lineage_rejected(name: str) -> None:
    payload = MINIMAL[name]()
    del payload["lineage"]
    assert_rejected(getattr(schema, name), payload, "lineage")


def test_full_snapshot_validates_and_round_trips() -> None:
    original = ErpSnapshot.model_validate(full_snapshot())
    assert ErpSnapshot.model_validate_json(original.model_dump_json()) == original


def test_snapshot_collections_are_tuples() -> None:
    parsed = ErpSnapshot.model_validate(full_snapshot())
    for name in (*COLLECTIONS, "source_issues"):
        assert isinstance(getattr(parsed, name), tuple), name
    assert isinstance(parsed.production_orders[0].operations, tuple)


def test_nested_unknown_field_rejected_through_snapshot() -> None:
    order = production_order()
    order["operations"][0]["options"][0]["bogus"] = 1
    assert_rejected(ErpSnapshot, snapshot_with("production_orders", order), "bogus")


def test_nested_field_is_frozen() -> None:
    parsed = ErpSnapshot.model_validate(full_snapshot())
    with pytest.raises(ValidationError):
        parsed.machines[0].efficiency = 2.0  # type: ignore[misc]  # frozen on purpose


# --- envelope: time, timezone, source ----------------------------------------------------------


@pytest.mark.parametrize("as_of", ["2026-10-05T05:00:00", "2026-10-05"])
def test_naive_as_of_rejected(as_of: str) -> None:
    assert_rejected(ErpSnapshot, with_(snapshot(), as_of=as_of), "as_of")


@pytest.mark.parametrize("zone", ["Mars/Base", "", "../../etc/passwd", "America/Chicago "])
def test_unknown_plant_timezone_rejected(zone: str) -> None:
    assert_rejected(ErpSnapshot, with_(snapshot(), plant_timezone=zone), "plant_timezone")


@pytest.mark.parametrize("zone", ["UTC", "America/Chicago", "Europe/Berlin"])
def test_known_plant_timezone_accepted(zone: str) -> None:
    assert ErpSnapshot.model_validate(with_(snapshot(), plant_timezone=zone)).plant_timezone == zone


def test_digest_must_be_64_hex_when_present() -> None:
    assert ErpSnapshot.model_validate(with_(snapshot(), digest="a" * 64)).digest == "a" * 64
    assert ErpSnapshot.model_validate(snapshot()).digest is None
    assert_rejected(ErpSnapshot, with_(snapshot(), digest="a" * 63), "digest")
    assert_rejected(ErpSnapshot, with_(snapshot(), digest="z" * 64), "digest")


def test_source_requires_every_identity_field() -> None:
    for field in ("system", "instance", "extractor", "extractor_version"):
        data = snapshot()
        del data["source"][field]
        assert_rejected(ErpSnapshot, data, field)


def test_source_issue_severity_is_hard_or_soft() -> None:
    assert_rejected(schema.SourceIssue, with_(source_issue(), severity="fatal"), "severity")
    assert schema.SourceIssue.model_validate(with_(source_issue(), severity="hard"))


# --- lineage ------------------------------------------------------------------------------------


def test_lineage_key_empty_rejected() -> None:
    assert_rejected(schema.Lineage, {"table": "T", "key": []}, "key")


def test_lineage_key_of_nine_parts_rejected_eight_accepted() -> None:
    eight = [f"k{n}" for n in range(8)]
    assert schema.Lineage.model_validate({"table": "T", "key": eight}).key == tuple(eight)
    assert_rejected(schema.Lineage, {"table": "T", "key": [*eight, "k8"]}, "key")


def test_lineage_table_empty_rejected() -> None:
    assert_rejected(schema.Lineage, {"table": "", "key": ["k1"]}, "table")


def test_lineage_key_part_empty_rejected() -> None:
    assert_rejected(schema.Lineage, lineage("T", ""), "key")


# --- ids, text and collection bounds -------------------------------------------------------------


def test_id_of_201_chars_rejected_200_accepted() -> None:
    assert schema.Item.model_validate(with_(schema_item(), id="x" * ID_LIMIT))
    assert_rejected(schema.Item, with_(schema_item(), id="x" * (ID_LIMIT + 1)), "id")


def test_empty_id_rejected() -> None:
    assert_rejected(schema.Machine, with_(machine(), id=""), "id")


def test_text_of_2001_chars_rejected_2000_accepted() -> None:
    assert schema.Item.model_validate(with_(schema_item(), description="t" * TEXT_LIMIT))
    assert_rejected(
        schema.Item, with_(schema_item(), description="t" * (TEXT_LIMIT + 1)), "description"
    )


def test_message_text_of_2001_chars_rejected() -> None:
    assert_rejected(
        schema.SourceIssue, with_(source_issue(), message="m" * (TEXT_LIMIT + 1)), "message"
    )


def test_attribute_key_over_id_limit_rejected() -> None:
    data = with_(schema_item(), attributes={"k" * (ID_LIMIT + 1): 1.0})
    assert_rejected(schema.Item, data)


def test_attribute_text_value_over_text_limit_rejected() -> None:
    data = with_(schema_item(), attributes={"note": "v" * (TEXT_LIMIT + 1)})
    assert_rejected(schema.Item, data)


def test_collection_of_100_001_records_rejected() -> None:
    record = vendor()
    data = with_(snapshot(), vendors=[record] * (COLLECTION_LIMIT + 1))
    assert_rejected(ErpSnapshot, data, "vendors")


def test_collection_of_100_000_records_accepted() -> None:
    record = vendor()
    data = with_(snapshot(), vendors=[record] * COLLECTION_LIMIT)
    assert len(ErpSnapshot.model_validate(data).vendors) == COLLECTION_LIMIT


# --- non-finite numbers ----------------------------------------------------------------------


SCALAR_FLOATS: list[tuple[type[BaseModel], Callable[[], Data], str]] = [
    (schema.OperationOption, option, "run_seconds_per_unit"),
    (schema.OperationOption, option, "setup_seconds"),
    (schema.Machine, machine, "efficiency"),
    (schema.OrderOperation, order_operation, "qty_remaining"),
    (schema.ProductionOrder, production_order, "quantity"),
    (schema.MaterialRequirement, material_requirement, "qty_required"),
    (schema.OvertimeWindow, lambda: MINIMAL["OvertimeWindow"](), "cost_multiplier"),
]
MAPPING_FLOATS: list[tuple[type[BaseModel], Callable[[], Data], str]] = [
    (schema.Item, lambda: MINIMAL["Item"](), "attributes"),
    (schema.WorkCenter, lambda: MINIMAL["WorkCenter"](), "attributes"),
    (schema.RoutingOperation, routing_operation, "setup_params"),
]


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
@pytest.mark.parametrize(
    ("cls", "factory", "field"),
    SCALAR_FLOATS,
    ids=[f"{cls.__name__}.{field}" for cls, _, field in SCALAR_FLOATS],
)
def test_non_finite_float_field_rejected(
    cls: type[BaseModel], factory: Callable[[], Data], field: str, value: float
) -> None:
    assert_rejected(cls, with_(factory(), **{field: value}), field)


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
@pytest.mark.parametrize(
    ("cls", "factory", "field"),
    MAPPING_FLOATS,
    ids=[f"{cls.__name__}.{field}" for cls, _, field in MAPPING_FLOATS],
)
def test_non_finite_mapping_value_rejected(
    cls: type[BaseModel], factory: Callable[[], Data], field: str, value: float
) -> None:
    assert_rejected(cls, with_(factory(), **{field: {"d": value}}), field)


def test_non_finite_rejected_through_snapshot_json() -> None:
    data = snapshot_with("machines", machine())
    text = ErpSnapshot.model_validate(data).model_dump_json()
    assert '"efficiency":1.0' in text
    text = text.replace('"efficiency":1.0', '"efficiency":NaN')
    with pytest.raises(ValidationError):
        ErpSnapshot.model_validate_json(text)


# --- non-negative quantities and seconds -------------------------------------------------------


NEGATIVE_CASES: list[tuple[type[BaseModel], Callable[[], Data], str]] = [
    (schema.OperationOption, option, "setup_seconds"),
    (schema.OperationOption, option, "run_seconds_per_unit"),
    (schema.OperationOption, option, "fixed_run_seconds"),
    (schema.RoutingOperation, routing_operation, "queue_seconds"),
    (schema.RoutingOperation, routing_operation, "move_seconds"),
    (schema.RoutingOperation, routing_operation, "sequence"),
    (schema.OutsideProcess, lambda: {"vendor_id": "v1", "lead_seconds": 0.0}, "lead_seconds"),
    (schema.OrderOperation, order_operation, "qty_to_produce"),
    (schema.OrderOperation, order_operation, "qty_complete"),
    (schema.OrderOperation, order_operation, "qty_scrapped"),
    (schema.OrderOperation, order_operation, "qty_remaining"),
    (schema.MaterialRequirement, material_requirement, "qty_required"),
    (schema.MaterialRequirement, material_requirement, "qty_issued"),
    (schema.SalesOrderLine, lambda: MINIMAL["SalesOrderLine"](), "qty_open"),
    (schema.SalesOrderLine, lambda: MINIMAL["SalesOrderLine"](), "line_nbr"),
    (schema.PurchaseLine, purchase_line, "qty_ordered"),
    (schema.PurchaseLine, purchase_line, "qty_received"),
    (schema.InventoryBalance, lambda: MINIMAL["InventoryBalance"](), "qty_available"),
]


@pytest.mark.parametrize(
    ("cls", "factory", "field"),
    NEGATIVE_CASES,
    ids=[f"{cls.__name__}.{field}" for cls, _, field in NEGATIVE_CASES],
)
def test_negative_quantity_or_seconds_rejected(
    cls: type[BaseModel], factory: Callable[[], Data], field: str
) -> None:
    assert_rejected(cls, with_(factory(), **{field: -1}), field)


@pytest.mark.parametrize(
    ("cls", "factory", "field"),
    NEGATIVE_CASES,
    ids=[f"{cls.__name__}.{field}" for cls, _, field in NEGATIVE_CASES],
)
def test_zero_quantity_or_seconds_accepted(
    cls: type[BaseModel], factory: Callable[[], Data], field: str
) -> None:
    payload = with_(factory(), **{field: 0})
    if cls is schema.OrderOperation and field == "qty_to_produce":
        payload["qty_remaining"] = 0
    assert isinstance(cls.model_validate(payload), cls)


@pytest.mark.parametrize(
    ("cls", "factory", "field"),
    [
        (schema.ProductionOrder, production_order, "quantity"),
        (schema.BomLine, lambda: MINIMAL["BomLine"](), "qty_per"),
        (schema.Receipt, receipt, "qty"),
        (schema.BatchSpec, lambda: MINIMAL["BatchSpec"](), "capacity"),
        (schema.Machine, machine, "efficiency"),
        (schema.CrewNeed, lambda: MINIMAL["CrewNeed"](), "size"),
        (schema.LaborPool, lambda: MINIMAL["LaborPool"](), "size"),
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_strictly_positive_field_rejects_zero_and_negative(
    cls: type[BaseModel], factory: Callable[[], Data], field: str, value: int
) -> None:
    assert_rejected(cls, with_(factory(), **{field: value}), field)


def test_machine_efficiency_default_is_one() -> None:
    payload = machine()
    del payload["efficiency"]
    assert schema.Machine.model_validate(payload).efficiency == 1.0


def test_option_fixed_run_seconds_default_is_zero() -> None:
    payload = option()
    del payload["fixed_run_seconds"]
    assert schema.OperationOption.model_validate(payload).fixed_run_seconds == 0


def test_option_machine_id_defaults_to_none() -> None:
    assert schema.OperationOption.model_validate(option()).machine_id is None


def test_preference_minus_one_rejected() -> None:
    assert_rejected(schema.OperationOption, with_(option(), preference=-1), "preference")


def test_batch_capacity_zero_rejected() -> None:
    assert_rejected(schema.BatchSpec, {"capacity": 0, "unit": "piece"}, "capacity")


# --- operations ---------------------------------------------------------------------------------


@pytest.mark.parametrize("cls", [schema.RoutingOperation, schema.OrderOperation])
def test_empty_option_list_without_outside_rejected(cls: type[BaseModel]) -> None:
    factory = routing_operation if cls is schema.RoutingOperation else order_operation
    assert_rejected(cls, with_(factory(), options=[]))


@pytest.mark.parametrize("cls", [schema.RoutingOperation, schema.OrderOperation])
def test_empty_option_list_with_outside_accepted(cls: type[BaseModel]) -> None:
    factory = routing_operation if cls is schema.RoutingOperation else order_operation
    payload = with_(factory(), options=[], outside={"vendor_id": "v1", "lead_seconds": 3600.0})
    assert isinstance(cls.model_validate(payload), cls)


def test_qty_remaining_above_qty_to_produce_rejected() -> None:
    payload = with_(order_operation(), qty_to_produce=5.0, qty_remaining=5.5)
    assert_rejected(schema.OrderOperation, payload)


def test_qty_remaining_equal_to_qty_to_produce_accepted() -> None:
    payload = with_(order_operation(), qty_to_produce=5.0, qty_remaining=5.0)
    assert schema.OrderOperation.model_validate(payload).qty_remaining == 5.0


def test_order_operation_has_every_routing_operation_field() -> None:
    assert set(schema.RoutingOperation.model_fields) <= set(schema.OrderOperation.model_fields)
    assert set(schema.OrderOperation.model_fields) - set(schema.RoutingOperation.model_fields) == {
        "status",
        "qty_to_produce",
        "qty_complete",
        "qty_scrapped",
        "qty_remaining",
        "current_machine_id",
        "actual_start",
    }


@pytest.mark.parametrize("status", ["pending", "in_process", "on_hold", "complete", "cancelled"])
def test_order_operation_status_values_accepted(status: str) -> None:
    assert schema.OrderOperation.model_validate(with_(order_operation(), status=status))


@pytest.mark.parametrize("status", ["released", "locked", "done", ""])
def test_order_operation_unknown_status_rejected(status: str) -> None:
    assert_rejected(schema.OrderOperation, with_(order_operation(), status=status), "status")


def test_naive_actual_start_rejected() -> None:
    payload = with_(order_operation(), actual_start="2026-10-05T03:00:00")
    assert_rejected(schema.OrderOperation, payload, "actual_start")


def test_crew_size_zero_rejected_through_operation() -> None:
    payload = with_(routing_operation(), crew={"labor_pool_id": "lp1", "size": 0})
    assert_rejected(schema.RoutingOperation, payload, "size")


# --- production orders ---------------------------------------------------------------------------


def test_production_order_field_names() -> None:
    assert set(schema.ProductionOrder.model_fields) == {
        "id",
        "order_type",
        "item_id",
        "quantity",
        "status",
        "rush",
        "hold",
        "erp_priority",
        "release_date",
        "due_date",
        "sales_links",
        "operations",
        "materials",
        "lineage",
    }


def test_production_order_without_operations_rejected() -> None:
    assert_rejected(schema.ProductionOrder, with_(production_order(), operations=[]), "operations")


def test_production_order_duplicate_op_id_rejected() -> None:
    payload = with_(
        production_order(), operations=[order_operation("10", 10), order_operation("10", 20)]
    )
    assert_rejected(schema.ProductionOrder, payload)


def test_production_order_distinct_op_ids_accepted() -> None:
    payload = with_(
        production_order(), operations=[order_operation("10", 10), order_operation("20", 20)]
    )
    parsed = schema.ProductionOrder.model_validate(payload)
    assert [op.op_id for op in parsed.operations] == ["10", "20"]


@pytest.mark.parametrize(
    "status", ["planned", "released", "in_process", "on_hold", "complete", "closed", "cancelled"]
)
def test_production_order_status_values_accepted(status: str) -> None:
    assert schema.ProductionOrder.model_validate(with_(production_order(), status=status))


@pytest.mark.parametrize("status", ["locked", "pending", "open"])
def test_production_order_unknown_status_rejected(status: str) -> None:
    assert_rejected(schema.ProductionOrder, with_(production_order(), status=status), "status")


def test_production_order_dates_are_date_only() -> None:
    payload = with_(production_order(), due_date="2026-10-20", release_date="2026-10-05")
    parsed = schema.ProductionOrder.model_validate(payload)
    assert parsed.due_date.isoformat() == "2026-10-20"
    assert_rejected(
        schema.ProductionOrder, with_(production_order(), due_date="not-a-date"), "due_date"
    )


def test_source_info_field_names() -> None:
    assert set(schema.SourceInfo.model_fields) == {
        "system",
        "instance",
        "extractor",
        "extractor_version",
        "generation",
    }


def test_lineage_field_names() -> None:
    assert set(schema.Lineage.model_fields) == {"table", "key"}


# --- purchasing -----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "cancelled", ""])
def test_purchase_line_unknown_status_rejected(status: str) -> None:
    assert_rejected(schema.PurchaseLine, with_(purchase_line(), status=status), "status")


def test_naive_receipt_time_rejected() -> None:
    assert_rejected(
        schema.Receipt, with_(receipt(), received_at="2026-10-05T05:00:00"), "received_at"
    )


# --- calendars ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"), [(1439, 1440), (0, 1440), (1320, 360), (0, 0), (360, 840)]
)
def test_time_window_in_range_accepted(start: int, end: int) -> None:
    parsed = schema.TimeWindow.model_validate({"start_minute": start, "end_minute": end})
    assert (parsed.start_minute, parsed.end_minute) == (start, end)


@pytest.mark.parametrize(
    ("start", "end", "field"),
    [
        (0, 1441, "end_minute"),
        (1440, 1440, "start_minute"),
        (-1, 60, "start_minute"),
        (0, -1, "end_minute"),
    ],
)
def test_time_window_out_of_range_rejected(start: int, end: int, field: str) -> None:
    assert_rejected(schema.TimeWindow, {"start_minute": start, "end_minute": end}, field)


@pytest.mark.parametrize("weekday", [-1, 7])
def test_weekday_out_of_range_rejected(weekday: int) -> None:
    assert_rejected(schema.WeekdayWindows, {"weekday": weekday, "windows": []}, "weekday")


def test_calendar_duplicate_exception_date_rejected() -> None:
    payload = with_(
        calendar(), exceptions=[calendar_exception("2026-12-25"), calendar_exception("2026-12-25")]
    )
    assert_rejected(schema.Calendar, payload)


def test_calendar_distinct_exception_dates_accepted() -> None:
    payload = with_(
        calendar(), exceptions=[calendar_exception("2026-12-24"), calendar_exception("2026-12-25")]
    )
    assert len(schema.Calendar.model_validate(payload).exceptions) == 2


def test_calendar_exception_empty_windows_means_closed_and_is_valid() -> None:
    parsed = schema.CalendarException.model_validate(calendar_exception())
    assert parsed.windows == ()


# --- overtime (COMP-126) ------------------------------------------------------------------------


@pytest.mark.parametrize("multiplier", [0.99, 0.0, -1.0])
def test_overtime_cost_multiplier_below_one_rejected(multiplier: float) -> None:
    payload = {"start_minute": 900, "end_minute": 1020, "cost_multiplier": multiplier}
    assert_rejected(schema.OvertimeWindow, payload, "cost_multiplier")


def test_overtime_cost_multiplier_of_one_accepted() -> None:
    payload = {"start_minute": 900, "end_minute": 1020, "cost_multiplier": 1.0}
    assert schema.OvertimeWindow.model_validate(payload).cost_multiplier == 1.0


@pytest.mark.parametrize(
    ("start", "end", "field"), [(0, 1441, "end_minute"), (1440, 1440, "start_minute")]
)
def test_overtime_window_out_of_range_rejected(start: int, end: int, field: str) -> None:
    payload = {"start_minute": start, "end_minute": end, "cost_multiplier": 1.5}
    assert_rejected(schema.OvertimeWindow, payload, field)


def test_overtime_duplicate_exception_date_rejected() -> None:
    payload = with_(
        overtime(), exceptions=[overtime_exception("2026-10-10"), overtime_exception("2026-10-10")]
    )
    assert_rejected(schema.Overtime, payload)


@pytest.mark.parametrize("cls", [schema.WorkCenter, schema.Machine])
def test_overtime_is_optional_on_work_center_and_machine(cls: type[BaseModel]) -> None:
    factory = MINIMAL[cls.__name__]
    assert cls.model_validate(factory()).overtime is None  # type: ignore[attr-defined]
    parsed = cls.model_validate(with_(factory(), overtime=overtime()))
    assert parsed.overtime.week[0].windows[0].cost_multiplier == 1.5  # type: ignore[attr-defined]


@pytest.mark.parametrize("cls", [schema.WorkCenter, schema.Machine])
def test_bad_overtime_rejected_through_owner(cls: type[BaseModel]) -> None:
    bad = copy.deepcopy(overtime())
    bad["week"][0]["windows"][0]["cost_multiplier"] = 0.5
    assert_rejected(cls, with_(MINIMAL[cls.__name__](), overtime=bad), "cost_multiplier")


# --- helpers used above (kept at the bottom so the tests read first) ---------------------------


def schema_item() -> Data:
    return MINIMAL["Item"]()
