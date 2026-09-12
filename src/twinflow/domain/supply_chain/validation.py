"""Referential, dimensional, temporal, and allocation validation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from twinflow.domain.supply_chain.contracts import NetworkModel, NetworkSnapshot, ValidationIssue
from twinflow.domain.supply_chain.parsing import SupplyChainInputError, parse_model, parse_snapshot


def validate(model: Mapping[str, object], snapshot: Mapping[str, object]) -> list[ValidationIssue]:
    """Return all independently detectable input problems without executing."""
    try:
        parsed_model = parse_model(model)
        parsed_snapshot = parse_snapshot(snapshot)
    except SupplyChainInputError as exc:
        return [ValidationIssue("invalid_input", "$", str(exc))]
    issues: list[ValidationIssue] = []
    _duplicates(parsed_model, parsed_snapshot, issues)
    _references(parsed_model, parsed_snapshot, issues)
    _quantities_and_dates(parsed_model, parsed_snapshot, issues)
    _bom_rules(parsed_model, parsed_snapshot, issues)
    _allocations(parsed_snapshot, issues)
    return issues


def _duplicates(
    model: NetworkModel, snapshot: NetworkSnapshot, issues: list[ValidationIssue]
) -> None:
    groups = {
        "sites": [item.site_id for item in model.sites],
        "suppliers": [item.supplier_id for item in model.suppliers],
        "items": [item.item_id for item in model.items],
        "bom_revisions": [item.bom_id for item in model.boms],
        "processes": [item.process_id for item in model.processes],
        "gates": [item.gate_id for item in model.gates],
        "supplies": [item.supply_id for item in snapshot.supplies],
        "documents": [item.document_id for item in snapshot.documents],
        "orders": [item.order_id for item in snapshot.orders],
    }
    for path, values in groups.items():
        seen: set[str] = set()
        for value in values:
            if value in seen:
                issues.append(ValidationIssue("duplicate_id", path, f"duplicate ID {value!r}"))
            seen.add(value)


def _references(
    model: NetworkModel, snapshot: NetworkSnapshot, issues: list[ValidationIssue]
) -> None:
    sites = {item.site_id for item in model.sites}
    suppliers = {item.supplier_id for item in model.suppliers}
    items = {item.item_id: item for item in model.items}
    processes = {item.process_id for item in model.processes}
    for supplier in model.suppliers:
        _reference(supplier.site_id, sites, "suppliers.site_id", issues)
    for bom in model.boms:
        _reference(bom.assembly_item_id, set(items), "bom_revisions.assembly_item_id", issues)
        for line in bom.lines:
            _reference(line.item_id, set(items), "bom_revisions.lines.item_id", issues)
            item = items.get(line.item_id)
            if item is not None and line.uom != item.uom:
                issues.append(
                    ValidationIssue(
                        "unit_mismatch",
                        "bom_revisions.lines.uom",
                        f"{line.item_id} requires {item.uom}, got {line.uom}",
                    )
                )
    for process in model.processes:
        _reference(process.item_id, set(items), "processes.item_id", issues)
    for qualification in model.qualifications:
        _reference(qualification.supplier_id, suppliers, "qualifications.supplier_id", issues)
        _reference(qualification.process_id, processes, "qualifications.process_id", issues)
    for gate in model.gates:
        _reference(gate.item_id, set(items), "gates.item_id", issues)
    for supply in snapshot.supplies:
        _reference(supply.item_id, set(items), "supply.item_id", issues)
        _reference(supply.site_id, sites, "supply.site_id", issues)
        _reference(supply.supplier_id, suppliers, "supply.supplier_id", issues)
        item = items.get(supply.item_id)
        if item is not None and supply.uom != item.uom:
            issues.append(
                ValidationIssue(
                    "unit_mismatch",
                    "supply.uom",
                    f"{supply.supply_id} requires {item.uom}, got {supply.uom}",
                )
            )
    for order in snapshot.orders:
        _reference(order.item_id, set(items), "orders.item_id", issues)
        _reference(order.site_id, sites, "orders.site_id", issues)
        item = items.get(order.item_id)
        if item is not None and order.uom != item.uom:
            issues.append(
                ValidationIssue(
                    "unit_mismatch",
                    "orders.uom",
                    f"{order.order_id} requires {item.uom}, got {order.uom}",
                )
            )


def _reference(
    value: str | None, allowed: set[str], path: str, issues: list[ValidationIssue]
) -> None:
    if value is not None and value not in allowed:
        issues.append(ValidationIssue("unknown_reference", path, f"unknown reference {value!r}"))


def _quantities_and_dates(
    model: NetworkModel, snapshot: NetworkSnapshot, issues: list[ValidationIssue]
) -> None:
    for bom in model.boms:
        if not bom.lines:
            issues.append(
                ValidationIssue(
                    "empty_bom", "bom_revisions.lines", f"BOM {bom.bom_id} has no lines"
                )
            )
        if bom.effective_to is not None and bom.effective_to <= bom.effective_from:
            issues.append(
                ValidationIssue(
                    "invalid_date_range", "bom_revisions", f"BOM {bom.bom_id} ends before it starts"
                )
            )
        for line in bom.lines:
            _positive(line.quantity, "bom_revisions.lines.quantity", issues)
    for process in model.processes:
        if process.lead_time_days < 0:
            issues.append(
                ValidationIssue(
                    "negative_lead_time",
                    "processes.lead_time_days",
                    f"process {process.process_id}",
                )
            )
    for supplier in model.suppliers:
        risk = supplier.delay_risk
        if risk is not None and (risk.minimum_days < 0 or risk.maximum_days < risk.minimum_days):
            issues.append(
                ValidationIssue(
                    "invalid_delay_range",
                    "suppliers.delay_risk",
                    f"supplier {supplier.supplier_id} has an invalid delay range",
                )
            )
    for qualification in model.qualifications:
        if (
            qualification.valid_to is not None
            and qualification.valid_to <= qualification.valid_from
        ):
            issues.append(
                ValidationIssue(
                    "invalid_date_range",
                    "qualifications",
                    f"qualification for {qualification.process_id}",
                )
            )
    for supply in snapshot.supplies:
        _positive(supply.quantity, "supply.quantity", issues)
    for allocation in snapshot.allocations:
        _positive(allocation.quantity, "allocations.quantity", issues)
    for order in snapshot.orders:
        _positive(order.quantity, "orders.quantity", issues)
    for document in snapshot.documents:
        if document.expires_at is not None and document.expires_at <= document.valid_from:
            issues.append(
                ValidationIssue(
                    "invalid_date_range", "documents", f"document {document.document_id}"
                )
            )


def _positive(value: float, path: str, issues: list[ValidationIssue]) -> None:
    if value <= 0:
        issues.append(ValidationIssue("nonpositive_quantity", path, "quantity must be positive"))


def _bom_rules(
    model: NetworkModel, snapshot: NetworkSnapshot, issues: list[ValidationIssue]
) -> None:
    by_assembly: dict[str, list[object]] = defaultdict(list)
    for bom in model.boms:
        by_assembly[bom.assembly_item_id].append(bom)
    for assembly_id in by_assembly:
        active = [
            bom
            for bom in model.boms
            if bom.assembly_item_id == assembly_id
            and bom.effective_from <= snapshot.as_of
            and (bom.effective_to is None or snapshot.as_of < bom.effective_to)
        ]
        if len(active) > 1:
            issues.append(
                ValidationIssue(
                    "overlapping_bom_revision",
                    "bom_revisions",
                    f"multiple revisions effective for {assembly_id}",
                )
            )
    graph: dict[str, set[str]] = defaultdict(set)
    for bom in model.boms:
        graph[bom.assembly_item_id].update(line.item_id for line in bom.lines)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> bool:
        if item_id in visiting:
            return True
        if item_id in visited:
            return False
        visiting.add(item_id)
        cyclic = any(visit(child) for child in graph[item_id])
        visiting.remove(item_id)
        visited.add(item_id)
        return cyclic

    for item_id in sorted(graph):
        if visit(item_id):
            issues.append(
                ValidationIssue("bom_cycle", "bom_revisions", f"BOM cycle reaches {item_id}")
            )
            break


def _allocations(snapshot: NetworkSnapshot, issues: list[ValidationIssue]) -> None:
    supply_quantity = {supply.supply_id: supply.quantity for supply in snapshot.supplies}
    order_ids = {order.order_id for order in snapshot.orders}
    totals: dict[str, float] = defaultdict(float)
    for allocation in snapshot.allocations:
        if allocation.supply_id not in supply_quantity:
            issues.append(
                ValidationIssue(
                    "unknown_reference",
                    "allocations.supply_id",
                    f"unknown supply {allocation.supply_id!r}",
                )
            )
        if allocation.order_id not in order_ids:
            issues.append(
                ValidationIssue(
                    "unknown_reference",
                    "allocations.order_id",
                    f"unknown order {allocation.order_id!r}",
                )
            )
        totals[allocation.supply_id] += allocation.quantity
        supply = next(
            (item for item in snapshot.supplies if item.supply_id == allocation.supply_id), None
        )
        if supply is not None and supply.quality_status != "released":
            issues.append(
                ValidationIssue(
                    "allocated_unusable_supply",
                    "allocations.supply_id",
                    f"{allocation.supply_id} is on quality hold",
                )
            )
    for supply_id, total in totals.items():
        if total > supply_quantity.get(supply_id, 0.0) + 1e-9:
            issues.append(
                ValidationIssue(
                    "overallocation",
                    "allocations",
                    f"{supply_id} allocated {total} beyond available "
                    f"{supply_quantity.get(supply_id, 0.0)}",
                )
            )
