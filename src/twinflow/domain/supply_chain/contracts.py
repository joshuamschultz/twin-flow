"""Typed value objects for the supply-network domain."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str
    severity: Severity = "error"

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class Site:
    site_id: str


@dataclass(frozen=True)
class DelayRisk:
    minimum_days: float
    maximum_days: float
    common_risk_group: str


@dataclass(frozen=True)
class Supplier:
    supplier_id: str
    site_id: str | None
    delay_risk: DelayRisk | None


@dataclass(frozen=True)
class Item:
    item_id: str
    kind: Literal["purchased", "part", "assembly"]
    uom: str


@dataclass(frozen=True)
class BomLine:
    item_id: str
    quantity: float
    uom: str


@dataclass(frozen=True)
class BomRevision:
    bom_id: str
    assembly_item_id: str
    revision: str
    effective_from: datetime
    effective_to: datetime | None
    lines: tuple[BomLine, ...]


@dataclass(frozen=True)
class Process:
    process_id: str
    item_id: str
    lead_time_days: float


@dataclass(frozen=True)
class Qualification:
    supplier_id: str
    process_id: str
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class GateRule:
    gate_id: str
    item_id: str
    evidence_type: str
    rule_revision: str


@dataclass(frozen=True)
class NetworkModel:
    sites: tuple[Site, ...]
    suppliers: tuple[Supplier, ...]
    items: tuple[Item, ...]
    boms: tuple[BomRevision, ...]
    processes: tuple[Process, ...]
    qualifications: tuple[Qualification, ...]
    gates: tuple[GateRule, ...]


@dataclass(frozen=True)
class Supply:
    supply_id: str
    supply_type: Literal["inventory", "receipt"]
    item_id: str
    site_id: str
    quantity: float
    uom: str
    available_at: datetime
    supplier_id: str | None
    quality_status: Literal["released", "hold"]


@dataclass(frozen=True)
class Allocation:
    supply_id: str
    order_id: str
    quantity: float


@dataclass(frozen=True)
class EvidenceDocument:
    document_id: str
    evidence_type: str
    subject_id: str
    valid_from: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class Order:
    order_id: str
    item_id: str
    site_id: str
    quantity: float
    uom: str
    due_at: datetime
    priority: int


@dataclass(frozen=True)
class NetworkSnapshot:
    as_of: datetime
    supplies: tuple[Supply, ...]
    allocations: tuple[Allocation, ...]
    documents: tuple[EvidenceDocument, ...]
    orders: tuple[Order, ...]
