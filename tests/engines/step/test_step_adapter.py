"""T-232: the make_to_stock domain adapter through the registry and the capsule (COMP-151)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from twinflow.application.scenarios import describe, evaluate_capsule
from twinflow.domain import registry
from twinflow.engines.step.adapter import MakeToStockAdapter
from twinflow.scenario import ScenarioCapsule
from twinflow.state.recorder import read_moves
from twinflow.state.records import Snapshot, snapshot_to_dict

GOLDEN: dict[str, Any] = json.loads(Path(__file__).with_name("golden.json").read_text())


def _model() -> dict[str, Any]:
    problem = GOLDEN["problem"]
    return {
        "domain": "make_to_stock",
        "revision": "1",
        "period": "day",
        "horizon": problem["horizon"],
        "start_cover_days": problem["start_cover_days"],
        "line": {
            "id": "press",
            "changeover": {
                "within_family": {
                    "cost": problem["changeover_cost_within"],
                    "output_lost": problem["output_lost_within"],
                },
                "across_family": {
                    "cost": problem["changeover_cost_across"],
                    "output_lost": problem["output_lost_across"],
                },
            },
        },
        "products": [
            {
                "id": r["sku"],
                "family": r["family"],
                "abc": r["abc"],
                "demand": r["demand"],
                "run_rate": r["run_rate"],
                "price": r["price"],
                "unit_cost": r["unit_cost"],
                "max_storage": r["max_storage"],
            }
            for r in GOLDEN["sku_table"]
        ],  # fmt: skip
        "economics": {
            "defaults": {
                "holding_rate": problem["holding_rate"],
                "lost_sale_rate": problem["lost_sale_penalty_rate"],
            }
        },
    }


def _capsule(**experiment: Any) -> ScenarioCapsule:
    return ScenarioCapsule.from_dict(
        {
            "schema_version": "0.1",
            "required_capabilities": ["make_to_stock.step"],
            "model": _model(),
            "snapshot": {
                "id": "mts-1",
                "as_of": "2026-11-17T08:00:00-06:00",
                "model_revision": "1",
            },
            "experiment": {"id": "base", "replications": 5, "seed": 2024, **experiment},
            "assumptions": ["demand is Poisson per day"],
            "provenance": {"source": "rl-mfg-supplychain golden table"},
        }
    )


def test_registered_with_its_capability() -> None:
    assert registry.capabilities()["make_to_stock"] == frozenset({"make_to_stock.step"})
    assert isinstance(registry.get("make_to_stock"), MakeToStockAdapter)


def test_valid_capsule_has_no_issues() -> None:
    assert _capsule().validate() == []


def test_capsule_without_the_capability_is_named() -> None:
    data = _capsule().to_dict()
    data["required_capabilities"] = ["make_to_stock.nope"]
    issues = ScenarioCapsule.from_dict(data).validate()
    assert [i.path for i in issues] == ["$.required_capabilities"]


def test_model_errors_name_the_field_and_the_fix() -> None:
    data = _capsule().to_dict()
    data["model"]["products"][2]["run_rate"] = 0
    issues = ScenarioCapsule.from_dict(data).validate()
    assert [i.path for i in issues] == ["$.model.products[2].run_rate"]
    assert issues[0].severity == "error" and issues[0].suggestion


def test_missing_money_is_a_note_not_a_validation_failure() -> None:
    data = _capsule().to_dict()
    del data["model"]["economics"]
    capsule = ScenarioCapsule.from_dict(data)
    assert capsule.validate() == []  # D-133: a twin runs before finance gives numbers
    notes = describe(capsule)["description"]["notes"]
    assert any("holding_per_unit_day" in n["path"] for n in notes)
    assert all(n["severity"] == "info" for n in notes)


def test_describe_lists_products_and_the_line() -> None:
    info = describe(_capsule())
    assert info["domain"] == "make_to_stock"
    assert [n["id"] for n in info["nodes"]][:2] == ["GRY-S", "GRY-M"]
    assert info["description"]["line"] == "press"
    assert info["description"]["horizon"] == 60
    assert info["description"]["families"] == ["grey", "black", "natural"]


@pytest.mark.parametrize(
    ("experiment", "golden"),
    [({}, "lowest_cover"), ({"policy": "family_campaign"}, "family_campaign"),
     ({"policy": "idle"}, "idle")],
)  # fmt: skip
def test_evaluate_reproduces_the_rl_profit(experiment: dict[str, Any], golden: str) -> None:
    capsule = _capsule(replications=GOLDEN["episodes"], **experiment)
    result = capsule.evaluate(seed=GOLDEN["seed"])
    assert isinstance(result, dict) and result["status"] == "completed"
    assert result["policy"] == golden
    assert result["replications"] == GOLDEN["episodes"]
    assert result["metrics"]["mean_profit"] == pytest.approx(
        GOLDEN["policies"][golden]["mean_profit"], abs=1e-6
    )


def test_evaluate_writes_moves_per_episode_and_metrics(tmp_path: Path) -> None:
    capsule = _capsule(replications=3)
    result = capsule.evaluate(seed=7, artifact_dir=str(tmp_path / "run"))
    assert isinstance(result, dict)
    moves = read_moves(tmp_path / "run")
    assert len(moves) == 3 * 60
    assert {m.actor for m in moves} == {"press"}
    metrics = result["metrics"]
    assert set(metrics) >= {"mean_profit", "fill_rate", "changeovers_per_episode"}
    assert 0.0 <= metrics["fill_rate"] <= 1.0
    total = sum(m.ledger.total for m in moves) / 3
    assert metrics["mean_profit"] == pytest.approx(total)
    assert len(result["episode_profits"]) == 3


def test_changeover_metrics_count_switches() -> None:
    result = _capsule(policy="fixed_wheel", replications=2).evaluate(seed=1)
    assert isinstance(result, dict)
    # The wheel changes product on days 1..59, and changes family every 4th day (4, 8, ..., 56).
    assert result["metrics"]["changeovers_per_episode"] == 59.0
    assert result["metrics"]["cross_family_per_episode"] == 14.0


def test_unknown_policy_is_reported_with_the_valid_names() -> None:
    result = _capsule(policy="lowest_covr").evaluate(seed=1)
    assert isinstance(result, dict) and result["status"] == "invalid"
    assert "lowest_cover" in json.dumps(result["issues"])


def test_invalid_model_is_not_evaluated() -> None:
    data = _capsule().to_dict()
    data["model"]["horizon"] = -1
    result = registry.evaluate("make_to_stock", data["model"], data["snapshot"], seed=1)
    assert result["status"] == "invalid"


def test_evaluate_can_start_from_a_saved_state() -> None:
    model = _model()
    stock = {r["sku"]: 100 for r in GOLDEN["sku_table"]}
    saved = Snapshot(t=50.0, period=50, stock=stock, setup={"press": "GRY-S"})
    snapshot = {"id": "s", "as_of": "2026-11-17T08:00:00-06:00", "model_revision": "1",
                "state": snapshot_to_dict(saved)}  # fmt: skip
    result = registry.evaluate("make_to_stock", model, snapshot, seed=1, limits={"replications": 2})
    assert result["status"] == "completed" and result["days"] == 10
    bad = copy.deepcopy(snapshot)
    bad["state"]["period"] = 99
    assert registry.evaluate("make_to_stock", model, bad, seed=1)["status"] == "invalid"


def test_runs_through_the_application_layer(tmp_path: Path) -> None:
    result = evaluate_capsule(_capsule().to_dict(), tmp_path / "out", 2, 11, lambda: False)
    assert result["domain"] == "make_to_stock"
    assert result["replications"] == 2
    assert "mean_profit" in result["metrics"]
    assert (tmp_path / "out" / "result.json").exists()
    assert len(read_moves(tmp_path / "out" / "rep-0001")) == 60


def test_adapter_module_imports_first_without_a_circular_import() -> None:
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", "import twinflow.engines.step.adapter"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


def test_only_adapters_that_opt_in_get_experiment_keys() -> None:
    experiment = {"replications": 20, "policy": "kanban", "seed": 1}
    assert registry.experiment_limits("make_to_stock", experiment) == {
        "policy": "kanban",
        "replications": 20,
    }
    assert registry.experiment_limits("office", experiment) == {}
    assert registry.experiment_limits("supply_chain", experiment) == {}
