from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import cast

import pytest

from twinflow.domain.supply_chain import SupplyChainDomain, describe, evaluate, validate

EXAMPLES = Path("examples/supply-chain-network")


def _document(name: str) -> dict[str, object]:
    with (EXAMPLES / name).open(encoding="utf-8") as handle:
        return cast(dict[str, object], json.load(handle))


def test_manufacturing_multitier_happy_path_and_artifact(tmp_path: Path) -> None:
    model = _document("manufacturing-model.json")
    snapshot = _document("manufacturing-snapshot.json")
    result = evaluate(model, snapshot, seed=7, artifact_dir=tmp_path, limits={"replications": 10})
    assert result["status"] == "complete"
    forecast = cast(list[dict[str, object]], result["order_forecasts"])[0]
    assert forecast["status"] == "feasible"
    assert forecast["delivery_at_p90"] is not None
    gates = cast(list[dict[str, object]], result["gate_results"])
    assert gates[0]["status"] == "passed"
    assert (tmp_path / "supply-chain-evidence.json").is_file()
    assert describe(model, snapshot)["valid"] is True


def test_multitier_shortage_traces_affected_order() -> None:
    model = _document("manufacturing-model.json")
    snapshot = _document("manufacturing-snapshot.json")
    cast(list[dict[str, object]], snapshot["receipts"])[0]["quantity"] = 3
    result = evaluate(model, snapshot, seed=0)
    shortage = cast(list[dict[str, object]], result["shortages"])[0]
    assert result["status"] == "incomplete"
    assert shortage["item_id"] == "mineral"
    assert shortage["missing_quantity"] == pytest.approx(1.0)
    assert shortage["dependency_path"] == ["finished-unit", "module", "mineral"]
    assert cast(list[dict[str, object]], result["affected_orders"])[0]["order_ids"] == ["order-1"]


def test_duplicate_allocation_is_rejected_before_execution() -> None:
    model = _document("distribution-model.json")
    snapshot = _document("distribution-snapshot.json")
    snapshot["allocations"] = [
        {"supply_id": "receipt-a", "order_id": "retailer-a", "quantity": 4},
        {"supply_id": "receipt-a", "order_id": "retailer-b", "quantity": 4},
    ]
    issues = validate(model, snapshot)
    assert any(issue.code == "overallocation" for issue in issues)
    assert evaluate(model, snapshot, seed=0)["status"] == "invalid"


def test_expired_document_blocks_release_without_compliance_claim() -> None:
    model = _document("manufacturing-model.json")
    snapshot = _document("manufacturing-snapshot.json")
    cast(list[dict[str, object]], snapshot["documents"])[0]["expires_at"] = "2026-01-02T00:00:00Z"
    result = evaluate(model, snapshot, seed=0)
    blocked = [
        gate
        for gate in cast(list[dict[str, object]], result["gate_results"])
        if gate["status"] == "blocked"
    ]
    assert blocked[0]["reasons"] == ["missing_or_expired_evidence"]
    assert blocked[0]["rule_revision"] == "release-v2"


def test_disqualified_process_blocks_build() -> None:
    model = _document("manufacturing-model.json")
    snapshot = _document("manufacturing-snapshot.json")
    model["qualifications"] = []
    result = evaluate(model, snapshot, seed=0)
    gates = cast(list[dict[str, object]], result["gate_results"])
    assert result["status"] == "incomplete"
    assert any(gate["reasons"] == ["no_valid_supplier_process"] for gate in gates)


def test_common_supplier_disruption_is_correlated_and_seeded() -> None:
    model = _document("distribution-model.json")
    snapshot = _document("distribution-snapshot.json")
    first = evaluate(model, snapshot, seed=19, limits={"replications": 20})
    second = evaluate(model, snapshot, seed=19, limits={"replications": 20})
    assert first == second
    forecasts = cast(list[dict[str, object]], first["order_forecasts"])
    assert forecasts[0]["delivery_at_p50"] == forecasts[1]["delivery_at_p50"]
    assert forecasts[0]["delivery_at_p90"] == forecasts[1]["delivery_at_p90"]


def test_bom_cycle_and_overlapping_revision_fail_validation() -> None:
    model = _document("manufacturing-model.json")
    snapshot = _document("manufacturing-snapshot.json")
    cyclic = copy.deepcopy(model)
    cast(list[dict[str, object]], cyclic["bom_revisions"])[0]["lines"] = [
        {"item_id": "finished-unit", "quantity": 1, "uom": "piece"}
    ]
    assert any(issue.code == "bom_cycle" for issue in validate(cyclic, snapshot))

    overlapping = copy.deepcopy(model)
    duplicate = copy.deepcopy(cast(list[dict[str, object]], overlapping["bom_revisions"])[1])
    duplicate["id"] = "unit-r3"
    duplicate["revision"] = "3"
    cast(list[dict[str, object]], overlapping["bom_revisions"]).append(duplicate)
    assert any(
        issue.code == "overlapping_bom_revision" for issue in validate(overlapping, snapshot)
    )


def test_quality_hold_is_not_allocated_and_adapter_matches_functions() -> None:
    model = _document("distribution-model.json")
    snapshot = _document("distribution-snapshot.json")
    for receipt in cast(list[dict[str, object]], snapshot["receipts"]):
        receipt["quality_status"] = "hold"
    adapter = SupplyChainDomain()
    result = adapter.evaluate(model, snapshot, seed=0)
    assert adapter.name == "supply_chain"
    assert result["status"] == "incomplete"
    assert cast(dict[str, object], result["metrics"])["feasible_count"] == 0


@pytest.mark.parametrize("replications", [0, 1001, 1.5, True])
def test_replication_limit_is_bounded(replications: object) -> None:
    with pytest.raises(ValueError):
        evaluate(
            _document("distribution-model.json"),
            _document("distribution-snapshot.json"),
            seed=0,
            limits={"replications": replications},
        )
