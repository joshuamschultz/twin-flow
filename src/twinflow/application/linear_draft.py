"""Strict, deterministic compiler for one-product linear piece flow."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Generic, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field

from twinflow.application.scenarios import validate_content
from twinflow.scenario import CapsuleValidationError

COMPILER_VERSION = "linear-piece-1"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
T = TypeVar("T")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Fact(StrictModel, Generic[T]):
    status: Literal["supplied", "confirmed", "inferred", "unknown"]
    value: T | None = None
    unit: str | None = None
    source: str | None = None
    evidence_ref: str | None = None


class Stage(StrictModel):
    id: str
    seconds_per_piece: Fact[float]
    machine_capacity: Fact[int]


class RouteEdge(StrictModel):
    source: str
    target: str


class Order(StrictModel):
    id: str
    quantity: Fact[int]
    release_second: Fact[float]
    due_second: Fact[float]


class UnsupportedFeature(StrictModel):
    kind: Literal[
        "calendar",
        "shift",
        "setup",
        "batch",
        "material_constraint",
        "yield",
        "scrap",
        "stochastic_time",
        "shared_resource",
        "other",
    ]
    description: str


class LinearDraft(StrictModel):
    product: Fact[str]
    input_part: Fact[str]
    as_of: Fact[str]
    operators: Fact[int]
    simple_flow_confirmed: Fact[bool]
    stages: list[Stage] = Field(max_length=30)
    route_edges: list[RouteEdge] = Field(default_factory=list, max_length=30)
    orders: list[Order] = Field(max_length=1000)
    unsupported_features: list[UnsupportedFeature] = Field(default_factory=list)
    source_evidence: list[str] = Field(default_factory=list)


def _digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def compile_draft(draft: LinearDraft, draft_id: str, revision: int) -> dict[str, Any]:
    """Return issues without guessing unknown or inferred operational values."""
    normalized = draft.model_dump(mode="json")
    issues: list[dict[str, str]] = []

    def issue(path: str, code: str, message: str, resolution: str) -> None:
        issues.append(
            {
                "path": path,
                "code": code,
                "severity": "error",
                "message": message,
                "needed_fact": path,
                "supported_resolution": resolution,
            }
        )

    def fact(path: str, item: Fact[Any], unit: str | None = None) -> Any:
        if item.status not in ("supplied", "confirmed") or item.value is None:
            issue(
                path,
                "missing",
                "A supplied or confirmed value is required",
                "Supply and confirm this fact",
            )
            return None
        if item.unit != unit:
            issue(path + ".unit", "invalid_unit", f"Expected unit {unit!r}", f"Use {unit!r}")
            return None
        if not item.source and not item.evidence_ref:
            issue(
                path + ".source",
                "missing_source",
                "Source or evidence reference is required",
                "Identify the source",
            )
        return item.value

    product = fact("product", draft.product)
    input_part = fact("input_part", draft.input_part)
    as_of = fact("as_of", draft.as_of)
    operators = fact("operators", draft.operators, "person")
    simple_flow = fact("simple_flow_confirmed", draft.simple_flow_confirmed)
    if simple_flow is False:
        issue(
            "simple_flow_confirmed",
            "unsupported",
            "This compiler requires explicit confirmation that no shifts, setups, batches, "
            "material constraints, yield loss, scrap, stochastic times, or shared resources apply",
            "Describe those constraints for a future supported compiler",
        )
    if not draft.stages:
        issue("stages", "missing", "At least one stage is required", "Add a process stage")
    if not draft.orders:
        issue("orders", "missing", "At least one order is required", "Add an order")
    for path, value in (("product", product), ("input_part", input_part)):
        if value is not None and not IDENTIFIER.fullmatch(value):
            issue(
                path,
                "invalid_identifier",
                "Use lowercase letters, digits and underscores",
                "Rename the part",
            )
    if product is not None and product == input_part:
        issue(
            "input_part", "duplicate", "Input part and product must differ", "Use distinct part IDs"
        )
    if as_of is not None:
        from datetime import datetime

        try:
            parsed = datetime.fromisoformat(as_of)
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
        except ValueError:
            issue(
                "as_of",
                "invalid_time",
                "Use an ISO timestamp with timezone",
                "Supply a timezone-aware as_of",
            )
    if operators is not None and not 1 <= operators <= 1000:
        issue(
            "operators",
            "invalid_capacity",
            "Operators must be 1..1000",
            "Supply explicit headcount",
        )

    stage_ids = [stage.id for stage in draft.stages]
    if len(stage_ids) != len(set(stage_ids)):
        issue("stages", "duplicate", "Stage IDs must be unique", "Rename duplicate stages")
    for index, stage in enumerate(draft.stages):
        path = f"stages.{index}"
        if not IDENTIFIER.fullmatch(stage.id):
            issue(
                path + ".id", "invalid_identifier", "Invalid stage ID", "Use a lowercase identifier"
            )
        seconds = fact(path + ".seconds_per_piece", stage.seconds_per_piece, "second/piece")
        capacity = fact(path + ".machine_capacity", stage.machine_capacity, "machine")
        if seconds is not None and not 0 < seconds <= 86400:
            issue(
                path + ".seconds_per_piece",
                "invalid_duration",
                "Duration must be >0 and <=86400",
                "Supply seconds per piece",
            )
        if capacity is not None and not 1 <= capacity <= 100:
            issue(
                path + ".machine_capacity",
                "invalid_capacity",
                "Machines must be 1..100",
                "Supply machine count",
            )
    expected_edges = list(zip(stage_ids, stage_ids[1:], strict=False))
    if (
        draft.route_edges
        and [(edge.source, edge.target) for edge in draft.route_edges] != expected_edges
    ):
        issue(
            "route_edges",
            "unsupported",
            "Only the listed linear stage order is supported",
            "Use consecutive edges without branches",
        )
    order_ids = [order.id for order in draft.orders]
    if len(order_ids) != len(set(order_ids)):
        issue("orders", "duplicate", "Order IDs must be unique", "Rename duplicate orders")
    for index, order in enumerate(draft.orders):
        path = f"orders.{index}"
        qty = fact(path + ".quantity", order.quantity, "piece")
        release = fact(path + ".release_second", order.release_second, "second")
        due = fact(path + ".due_second", order.due_second, "second")
        if qty is not None and not 1 <= qty <= 1_000_000:
            issue(
                path + ".quantity",
                "invalid_quantity",
                "Quantity must be 1..1000000",
                "Supply a whole-piece quantity",
            )
        if release is not None and not 0 <= release <= 31_536_000:
            issue(
                path + ".release_second",
                "invalid_time",
                "Release out of range",
                "Use seconds from scenario origin",
            )
        if due is not None and not 0 < due <= 31_536_000:
            issue(
                path + ".due_second",
                "invalid_time",
                "Due out of range",
                "Use seconds from scenario origin",
            )
        if release is not None and due is not None and due <= release:
            issue(
                path + ".due_second",
                "invalid_time",
                "Due must follow release",
                "Supply a later due second",
            )
    for index, feature in enumerate(draft.unsupported_features):
        issue(
            f"unsupported_features.{index}",
            "unsupported",
            f"{feature.kind} is not modeled",
            "Remove only after confirming it does not apply",
        )

    status = (
        "unsupported"
        if any(i["code"] == "unsupported" for i in issues)
        else ("incomplete" if any(i["code"].startswith("missing") for i in issues) else "invalid")
    )
    response: dict[str, Any] = {
        "draft_id": draft_id,
        "draft_revision": revision,
        "status": status if issues else "compiled",
        "issues": issues,
        "normalized_draft": normalized,
        "compiler_version": COMPILER_VERSION,
        "capsule_schema_version": "0.1",
    }
    if issues:
        return response

    intermediate = [f"{name}_output" for name in stage_ids[:-1]]
    things = [input_part, *intermediate, product]
    if len(things) != len(set(things)):
        issue("stages", "name_collision", "Generated part IDs collide", "Rename a stage or part")
        response["status"] = "invalid"
        return response
    fingerprint = _digest(normalized)
    locations = []
    for index, stage in enumerate(draft.stages):
        locations.append(
            {
                "name": stage.id,
                "consumes": [{"thing": things[index], "qty": 1, "uom": "piece"}],
                "emits": [{"thing": things[index + 1], "qty": 1, "uom": "piece"}],
                "setup_key": f"setup_{stage.id}",
                "time_model": {
                    "kind": "rate_based",
                    "rate": 1 / cast(float, stage.seconds_per_piece.value),
                },
                "machine": f"machine_{stage.id}",
                "capacity": stage.machine_capacity.value,
                "labor_skill": "operator",
            }
        )
    capsule = {
        "schema_version": "0.1",
        "model": {
            "defaults": {"cycle_time_cv": 0.0},
            "stocks": [],
            "part_types": [{"name": thing, "uom": "piece", "attributes": {}} for thing in things],
            "machines": [{"name": f"machine_{name}"} for name in stage_ids],
            "labor": {
                "pools": [{"name": "operators", "headcount": operators, "skills": ["operator"]}]
            },
            "locations": locations,
            "routing": [{"part": product, "steps": stage_ids}],
        },
        "snapshot": {
            "id": "draft-" + fingerprint[:24],
            "as_of": as_of,
            "model_revision": "1",
            "production_plan": [
                {
                    "work_order_id": order.id,
                    "part": product,
                    "qty": order.quantity.value,
                    "start_date": str(order.release_second.value),
                    "due_date": str(order.due_second.value),
                }
                for order in draft.orders
            ],
        },
        "experiment": {"id": "draft-" + fingerprint[:24], "replications": 1, "seed": 42},
        "assumptions": [
            "One-product linear one-piece flow; no yield loss or batch behavior.",
            "Deterministic seconds per piece; no setup, material shortages or shifts modeled.",
            "Release and due times are seconds from the supplied as_of origin.",
        ],
        "provenance": {
            "source": "typed_model_draft",
            "draft_id": draft_id,
            "draft_revision": revision,
            "compiler_version": COMPILER_VERSION,
            "facts": normalized,
        },
        "required_capabilities": ["manufacturing.basic"],
    }
    try:
        validated = validate_content(json.dumps(capsule, allow_nan=False))
    except CapsuleValidationError as exc:
        for entry in exc.issues:
            issue(
                entry.path, "invalid_capsule", entry.message, entry.suggestion or "Revise the draft"
            )
        response["status"] = "invalid"
        return response
    except ValueError as exc:
        issue("$", "invalid_capsule", str(exc), "Revise the draft")
        response["status"] = "invalid"
        return response
    response["capsule_preview"] = validated.to_dict()
    response["digest"] = validated.digest
    return response
