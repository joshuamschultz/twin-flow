"""Quantity-conserving material-to-delivery forecast evaluator."""

from __future__ import annotations

import json
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import numpy as np

from twinflow.domain.supply_chain.contracts import (
    BomRevision,
    GateRule,
    NetworkModel,
    NetworkSnapshot,
    Order,
    Process,
)
from twinflow.domain.supply_chain.parsing import parse_model, parse_snapshot
from twinflow.domain.supply_chain.validation import validate

MAX_REPLICATIONS = 1_000


@dataclass(frozen=True)
class _NeedResult:
    complete: bool
    available_at: datetime
    shortages: tuple[dict[str, object], ...]
    gates: tuple[dict[str, object], ...]


def evaluate(
    model: Mapping[str, object],
    snapshot: Mapping[str, object],
    seed: int,
    artifact_dir: str | Path | None = None,
    limits: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Evaluate feasible delivery forecasts; invalid inputs return a structured result."""
    issues = validate(model, snapshot)
    if any(issue.severity == "error" for issue in issues):
        return {
            "schema_version": 1,
            "domain": "supply_chain",
            "status": "invalid",
            "seed": seed,
            "replications": 0,
            "validation_issues": [item.to_dict() for item in issues],
            "order_forecasts": [],
            "shortages": [],
            "gate_results": [],
            "affected_orders": [],
            "metrics": {},
            "assumptions": [],
            "evidence_artifacts": [],
        }
    parsed_model = parse_model(model)
    parsed_snapshot = parse_snapshot(snapshot)
    replications = _replications(limits)
    samples = [
        _evaluate_once(parsed_model, parsed_snapshot, np.random.default_rng(seed + index))
        for index in range(replications)
    ]
    result = _aggregate(samples, parsed_model, seed, replications)
    if artifact_dir is not None:
        artifact = _write_artifact(Path(artifact_dir), result)
        result["evidence_artifacts"] = [str(artifact)]
    return result


def describe(model: Mapping[str, object], snapshot: Mapping[str, object]) -> dict[str, object]:
    """Describe supported semantics and counts without executing a forecast."""
    issues = validate(model, snapshot)
    if issues:
        return {
            "domain": "supply_chain",
            "valid": False,
            "issues": [item.to_dict() for item in issues],
        }
    parsed_model = parse_model(model)
    parsed_snapshot = parse_snapshot(snapshot)
    return {
        "domain": "supply_chain",
        "valid": True,
        "capabilities": [
            "multi_tier_bom",
            "revision_effectivity",
            "quantity_conserving_allocation",
            "quality_holds",
            "document_gates",
            "supplier_process_qualification",
            "correlated_supplier_delay",
            "affected_order_trace",
        ],
        "counts": {
            "sites": len(parsed_model.sites),
            "suppliers": len(parsed_model.suppliers),
            "items": len(parsed_model.items),
            "bom_revisions": len(parsed_model.boms),
            "supplies": len(parsed_snapshot.supplies),
            "orders": len(parsed_snapshot.orders),
        },
        "as_of": parsed_snapshot.as_of.isoformat(),
    }


def _replications(limits: Mapping[str, object] | None) -> int:
    value = 1 if limits is None else limits.get("replications", 1)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_REPLICATIONS:
        raise ValueError(f"limits.replications must be an integer from 1 to {MAX_REPLICATIONS}")
    return value


def _evaluate_once(
    model: NetworkModel, snapshot: NetworkSnapshot, rng: np.random.Generator
) -> dict[str, object]:
    supplier_by_id = {supplier.supplier_id: supplier for supplier in model.suppliers}
    group_delays: dict[str, float] = {}
    delayed_supply: dict[str, datetime] = {}
    assumptions: list[str] = []
    for supply in snapshot.supplies:
        delay = 0.0
        supplier = supplier_by_id.get(supply.supplier_id or "")
        if (
            supply.supply_type == "receipt"
            and supplier is not None
            and supplier.delay_risk is not None
        ):
            risk = supplier.delay_risk
            if risk.common_risk_group not in group_delays:
                group_delays[risk.common_risk_group] = rng.uniform(
                    risk.minimum_days, risk.maximum_days
                )
            delay = group_delays[risk.common_risk_group]
            assumptions.append(f"configured delay risk {risk.common_risk_group}")
        delayed_supply[supply.supply_id] = supply.available_at + timedelta(days=delay)
    remaining = {supply.supply_id: supply.quantity for supply in snapshot.supplies}
    for allocation in snapshot.allocations:
        remaining[allocation.supply_id] -= allocation.quantity
    context = _Context(model, snapshot, remaining, delayed_supply)
    forecasts: list[dict[str, object]] = []
    shortages: list[dict[str, object]] = []
    gates: list[dict[str, object]] = []
    for order in sorted(
        snapshot.orders, key=lambda item: (-item.priority, item.due_at, item.order_id)
    ):
        allocated_quantity, allocated_at = _allocated_direct(order, snapshot, delayed_supply)
        needed = order.quantity - allocated_quantity
        result = context.fulfill(order, order.item_id, max(0.0, needed), (order.item_id,))
        available_at = max(result.available_at, allocated_at)
        shortages.extend(result.shortages)
        gates.extend(result.gates)
        forecasts.append(
            {
                "order_id": order.order_id,
                "status": "feasible" if result.complete else "blocked",
                "delivery_at": available_at.isoformat() if result.complete else None,
                "due_at": order.due_at.isoformat(),
                "on_time": result.complete and available_at <= order.due_at,
            }
        )
    return {
        "order_forecasts": forecasts,
        "shortages": shortages,
        "gate_results": gates,
        "assumptions": sorted(set(assumptions)),
    }


class _Context:
    def __init__(
        self,
        model: NetworkModel,
        snapshot: NetworkSnapshot,
        remaining: dict[str, float],
        supply_dates: dict[str, datetime],
    ) -> None:
        self.model = model
        self.snapshot = snapshot
        self.remaining = remaining
        self.supply_dates = supply_dates
        self.items = {item.item_id: item for item in model.items}

    def fulfill(
        self, order: Order, item_id: str, quantity: float, path: tuple[str, ...]
    ) -> _NeedResult:
        latest = self.snapshot.as_of
        if quantity <= 1e-9:
            return _NeedResult(True, latest, (), ())
        unmet = quantity
        candidates = sorted(
            (
                supply
                for supply in self.snapshot.supplies
                if supply.item_id == item_id
                and supply.site_id == order.site_id
                and supply.quality_status == "released"
                and self.remaining[supply.supply_id] > 0
            ),
            key=lambda supply: (self.supply_dates[supply.supply_id], supply.supply_id),
        )
        for supply in candidates:
            used = min(unmet, self.remaining[supply.supply_id])
            if used <= 0:
                continue
            self.remaining[supply.supply_id] -= used
            unmet -= used
            latest = max(latest, self.supply_dates[supply.supply_id])
            if unmet <= 1e-9:
                gates = self._evaluate_gates(order, item_id, latest)
                complete = all(gate["status"] == "passed" for gate in gates)
                return _NeedResult(complete, latest, (), tuple(gates))
        item = self.items[item_id]
        if item.kind == "purchased":
            return self._shortage(order, item_id, unmet, path, latest)
        bom = self._active_bom(item_id)
        if bom is None:
            return self._shortage(
                order, item_id, unmet, path, latest, reason="missing_effective_bom"
            )
        shortages: list[dict[str, object]] = []
        gate_results: list[dict[str, object]] = []
        complete = True
        for line in bom.lines:
            component = self.fulfill(
                order, line.item_id, unmet * line.quantity, (*path, line.item_id)
            )
            latest = max(latest, component.available_at)
            complete = complete and component.complete
            shortages.extend(component.shortages)
            gate_results.extend(component.gates)
        process = self._qualified_process(item_id, latest)
        if process is None:
            gate_results.append(
                self._blocked_gate(order, item_id, "qualified_process", "no_valid_supplier_process")
            )
            return _NeedResult(False, latest, tuple(shortages), tuple(gate_results))
        completion = latest + timedelta(days=process.lead_time_days)
        own_gates = self._evaluate_gates(order, item_id, completion)
        gate_results.extend(own_gates)
        complete = complete and all(gate["status"] == "passed" for gate in own_gates)
        return _NeedResult(complete, completion, tuple(shortages), tuple(gate_results))

    def _active_bom(self, item_id: str) -> BomRevision | None:
        return next(
            (
                bom
                for bom in self.model.boms
                if bom.assembly_item_id == item_id
                and bom.effective_from <= self.snapshot.as_of
                and (bom.effective_to is None or self.snapshot.as_of < bom.effective_to)
            ),
            None,
        )

    def _qualified_process(self, item_id: str, at: datetime) -> Process | None:
        processes = sorted(
            (process for process in self.model.processes if process.item_id == item_id),
            key=lambda process: (process.lead_time_days, process.process_id),
        )
        return next(
            (
                process
                for process in processes
                if any(
                    qualification.process_id == process.process_id
                    and qualification.valid_from <= at
                    and (qualification.valid_to is None or at < qualification.valid_to)
                    for qualification in self.model.qualifications
                )
            ),
            None,
        )

    def _evaluate_gates(self, order: Order, item_id: str, at: datetime) -> list[dict[str, object]]:
        return [
            self._evaluate_gate(order, gate, at)
            for gate in self.model.gates
            if gate.item_id == item_id
        ]

    def _evaluate_gate(self, order: Order, gate: GateRule, at: datetime) -> dict[str, object]:
        evidence = [
            document
            for document in self.snapshot.documents
            if document.subject_id == gate.item_id
            and document.evidence_type == gate.evidence_type
            and document.valid_from <= self.snapshot.as_of
            and (document.expires_at is None or at < document.expires_at)
        ]
        passed = bool(evidence)
        return {
            "gate_id": gate.gate_id,
            "subject": {"item_id": gate.item_id},
            "rule_revision": gate.rule_revision,
            "evaluated_at": at.isoformat(),
            "status": "passed" if passed else "blocked",
            "reasons": [] if passed else ["missing_or_expired_evidence"],
            "evidence_refs": [item.document_id for item in evidence],
            "affected_order_ids": [order.order_id],
        }

    def _blocked_gate(
        self, order: Order, item_id: str, gate_id: str, reason: str
    ) -> dict[str, object]:
        return {
            "gate_id": gate_id,
            "subject": {"item_id": item_id},
            "rule_revision": "configured",
            "evaluated_at": self.snapshot.as_of.isoformat(),
            "status": "blocked",
            "reasons": [reason],
            "evidence_refs": [],
            "affected_order_ids": [order.order_id],
        }

    def _shortage(
        self,
        order: Order,
        item_id: str,
        quantity: float,
        path: tuple[str, ...],
        at: datetime,
        *,
        reason: str = "insufficient_supply",
    ) -> _NeedResult:
        shortage = {
            "order_id": order.order_id,
            "item_id": item_id,
            "missing_quantity": quantity,
            "uom": self.items[item_id].uom,
            "dependency_path": list(path),
            "reason": reason,
        }
        return _NeedResult(False, at, (shortage,), ())


def _allocated_direct(
    order: Order, snapshot: NetworkSnapshot, supply_dates: Mapping[str, datetime]
) -> tuple[float, datetime]:
    supply_by_id = {supply.supply_id: supply for supply in snapshot.supplies}
    quantity = 0.0
    available_at = snapshot.as_of
    for allocation in snapshot.allocations:
        supply = supply_by_id.get(allocation.supply_id)
        if allocation.order_id == order.order_id and supply is not None:
            if supply.item_id == order.item_id:
                quantity += allocation.quantity
                available_at = max(available_at, supply_dates[supply.supply_id])
    return quantity, available_at


def _aggregate(
    samples: list[dict[str, object]], model: NetworkModel, seed: int, replications: int
) -> dict[str, object]:
    first_forecasts = cast(list[dict[str, object]], samples[0]["order_forecasts"])
    forecasts: list[dict[str, object]] = []
    for index, first in enumerate(first_forecasts):
        rows = [
            cast(list[dict[str, object]], sample["order_forecasts"])[index] for sample in samples
        ]
        dates = sorted(
            datetime.fromisoformat(cast(str, row["delivery_at"]))
            for row in rows
            if row["delivery_at"] is not None
        )
        forecast = dict(first)
        if replications > 1:
            forecast["delivery_at_p50"] = _percentile_date(dates, 0.5)
            forecast["delivery_at_p90"] = _percentile_date(dates, 0.9)
            forecast["on_time_probability"] = (
                sum(row["on_time"] is True for row in rows) / replications
            )
        forecasts.append(forecast)
    shortages = cast(list[dict[str, object]], samples[0]["shortages"])
    gates = cast(list[dict[str, object]], samples[0]["gate_results"])
    affected: dict[str, set[str]] = defaultdict(set)
    for shortage in shortages:
        affected[cast(str, shortage["item_id"])].add(cast(str, shortage["order_id"]))
    for gate in gates:
        if gate["status"] == "blocked":
            affected[cast(str, gate["gate_id"])].update(cast(list[str], gate["affected_order_ids"]))
    feasible = sum(row["status"] == "feasible" for row in forecasts)
    assumptions = sorted(
        {text for sample in samples for text in cast(list[str], sample["assumptions"])}
    )
    return {
        "schema_version": 1,
        "domain": "supply_chain",
        "status": "complete" if feasible == len(forecasts) else "incomplete",
        "seed": seed,
        "replications": replications,
        "validation_issues": [],
        "order_forecasts": forecasts,
        "shortages": shortages,
        "gate_results": gates,
        "affected_orders": [
            {"dependency_id": key, "order_ids": sorted(values)}
            for key, values in sorted(affected.items())
        ],
        "metrics": {
            "order_count": len(forecasts),
            "feasible_count": feasible,
            "blocked_count": len(forecasts) - feasible,
        },
        "assumptions": assumptions,
        "evidence_artifacts": [],
        "model_counts": {"items": len(model.items), "suppliers": len(model.suppliers)},
    }


def _percentile_date(values: list[datetime], quantile: float) -> str | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, int((len(values) - 1) * quantile + 0.999999)))
    return values[index].isoformat()


def _write_artifact(directory: Path, result: Mapping[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "supply-chain-evidence.json"
    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        temporary = Path(handle.name)
    temporary.replace(destination)
    return destination
