"""Strict boundary parsing from JSON-shaped mappings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal, cast

from twinflow.domain.supply_chain.contracts import (
    Allocation,
    BomLine,
    BomRevision,
    DelayRisk,
    EvidenceDocument,
    GateRule,
    Item,
    NetworkModel,
    NetworkSnapshot,
    Order,
    Process,
    Qualification,
    Site,
    Supplier,
    Supply,
)


class SupplyChainInputError(ValueError):
    """Input cannot be parsed into the public supply-chain contract."""


def parse_model(value: Mapping[str, object]) -> NetworkModel:
    """Parse a model after structural boundary checks."""
    return NetworkModel(
        sites=tuple(Site(_text(row, "id")) for row in _rows(value, "sites")),
        suppliers=tuple(_supplier(row) for row in _rows(value, "suppliers")),
        items=tuple(
            Item(
                _text(row, "id"),
                cast(
                    Literal["purchased", "part", "assembly"],
                    _literal(row, "kind", ("purchased", "part", "assembly")),
                ),
                _text(row, "uom"),
            )
            for row in _rows(value, "items")
        ),
        boms=tuple(_bom(row) for row in _rows(value, "bom_revisions")),
        processes=tuple(
            Process(_text(row, "id"), _text(row, "item_id"), _number(row, "lead_time_days"))
            for row in _rows(value, "processes")
        ),
        qualifications=tuple(_qualification(row) for row in _rows(value, "qualifications")),
        gates=tuple(
            GateRule(
                _text(row, "id"),
                _text(row, "item_id"),
                _text(row, "evidence_type"),
                _text(row, "rule_revision"),
            )
            for row in _rows(value, "gates")
        ),
    )


def parse_snapshot(value: Mapping[str, object]) -> NetworkSnapshot:
    """Parse an operational snapshot after structural boundary checks."""
    supplies = tuple(_supply(row, "inventory") for row in _rows(value, "inventory")) + tuple(
        _supply(row, "receipt") for row in _rows(value, "receipts")
    )
    return NetworkSnapshot(
        as_of=_date(value, "as_of"),
        supplies=supplies,
        allocations=tuple(
            Allocation(_text(row, "supply_id"), _text(row, "order_id"), _number(row, "quantity"))
            for row in _rows(value, "allocations")
        ),
        documents=tuple(_document(row) for row in _rows(value, "documents")),
        orders=tuple(_order(row) for row in _rows(value, "orders")),
    )


def _supplier(row: Mapping[str, object]) -> Supplier:
    risk_value = row.get("delay_risk")
    risk = None
    if risk_value is not None:
        risk_row = _mapping(risk_value, "delay_risk")
        risk = DelayRisk(
            _number(risk_row, "minimum_days"),
            _number(risk_row, "maximum_days"),
            _text(risk_row, "common_risk_group"),
        )
    return Supplier(_text(row, "id"), _optional_text(row, "site_id"), risk)


def _bom(row: Mapping[str, object]) -> BomRevision:
    return BomRevision(
        _text(row, "id"),
        _text(row, "assembly_item_id"),
        _text(row, "revision"),
        _date(row, "effective_from"),
        _optional_date(row, "effective_to"),
        tuple(
            BomLine(_text(line, "item_id"), _number(line, "quantity"), _text(line, "uom"))
            for line in _rows(row, "lines")
        ),
    )


def _qualification(row: Mapping[str, object]) -> Qualification:
    return Qualification(
        _text(row, "supplier_id"),
        _text(row, "process_id"),
        _date(row, "valid_from"),
        _optional_date(row, "valid_to"),
    )


def _supply(row: Mapping[str, object], supply_type: Literal["inventory", "receipt"]) -> Supply:
    status = cast(
        Literal["released", "hold"],
        _literal(row, "quality_status", ("released", "hold"), default="released"),
    )
    return Supply(
        _text(row, "id"),
        supply_type,
        _text(row, "item_id"),
        _text(row, "site_id"),
        _number(row, "quantity"),
        _text(row, "uom"),
        _date(row, "available_at"),
        _optional_text(row, "supplier_id"),
        status,
    )


def _document(row: Mapping[str, object]) -> EvidenceDocument:
    return EvidenceDocument(
        _text(row, "id"),
        _text(row, "evidence_type"),
        _text(row, "subject_id"),
        _date(row, "valid_from"),
        _optional_date(row, "expires_at"),
    )


def _order(row: Mapping[str, object]) -> Order:
    priority = row.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise SupplyChainInputError("priority must be an integer")
    return Order(
        _text(row, "id"),
        _text(row, "item_id"),
        _text(row, "site_id"),
        _number(row, "quantity"),
        _text(row, "uom"),
        _date(row, "due_at"),
        priority,
    )


def _rows(parent: Mapping[str, object], name: str) -> tuple[Mapping[str, object], ...]:
    value = parent.get(name, [])
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SupplyChainInputError(f"{name} must be an array")
    return tuple(_mapping(row, name) for row in value)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise SupplyChainInputError(f"{name} entries must be objects")
    return cast(Mapping[str, object], value)


def _text(parent: Mapping[str, object], name: str) -> str:
    value = parent.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise SupplyChainInputError(f"{name} must be a non-empty string of at most 256 characters")
    return value


def _optional_text(parent: Mapping[str, object], name: str) -> str | None:
    return None if parent.get(name) is None else _text(parent, name)


def _number(parent: Mapping[str, object], name: str) -> float:
    value = parent.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SupplyChainInputError(f"{name} must be numeric")
    return float(value)


def _date(parent: Mapping[str, object], name: str) -> datetime:
    value = _text(parent, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupplyChainInputError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SupplyChainInputError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _optional_date(parent: Mapping[str, object], name: str) -> datetime | None:
    return None if parent.get(name) is None else _date(parent, name)


def _literal(
    parent: Mapping[str, object],
    name: str,
    choices: tuple[Literal["purchased", "part", "assembly"], ...]
    | tuple[Literal["released", "hold"], ...],
    *,
    default: str | None = None,
) -> Literal["purchased", "part", "assembly", "released", "hold"]:
    value = parent.get(name, default)
    if value not in choices:
        raise SupplyChainInputError(f"{name} must be one of {', '.join(choices)}")
    return cast(Literal["purchased", "part", "assembly", "released", "hold"], value)
