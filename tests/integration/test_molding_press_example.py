"""T-233: the molding-press example runs as a capsule (COMP-160, REQ-211, REQ-196).

`examples/molding-press/` holds the RL repo's 12-product press as a make-to-stock capsule,
two readable `decide(state)` policy files and a plain-words README. The capsule is run
through the scenario command and the registry, and its profit is tied to the RL repo's
golden numbers. The policy files are loaded here with a hand-built stand-in state; the real
loader and view arrive in TF-006 Phase 3.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from twinflow.cli.main import main
from twinflow.engines.step.model import parse_step_model
from twinflow.scenario import ScenarioCapsule
from twinflow.state.records import Snapshot

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "molding-press"
CAPSULE = EXAMPLE / "molding-press.twin.yaml"
GOLDEN = json.loads((ROOT / "tests" / "engines" / "step" / "golden.json").read_text())


def _capsule() -> ScenarioCapsule:
    return ScenarioCapsule.load(CAPSULE)


def test_capsule_validates() -> None:
    assert _capsule().validate() == []


def test_capsule_holds_the_rl_sku_table() -> None:
    model, issues = parse_step_model(_capsule().model)
    assert model is not None and [i for i in issues if i.severity == "error"] == []
    assert len(model.products) == 12
    for product, row in zip(model.products, GOLDEN["sku_table"], strict=True):
        assert (product.id, product.family, product.abc) == (row["sku"], row["family"], row["abc"])
        assert (product.demand_rate, product.run_rate) == (row["demand"], row["run_rate"])
        assert (product.price, product.unit_cost) == (row["price"], row["unit_cost"])
        assert product.max_storage == row["max_storage"]
    assert model.horizon == 60 and model.start_cover_days == 8.0


def test_simple_view_gives_short_readable_labels() -> None:
    model, _ = parse_step_model(_capsule().model)
    assert model is not None
    start = Snapshot(
        t=0.0,
        period=0,
        stock={p.id: float(round(8 * p.demand_rate)) for p in model.products},
        setup={"press": "GRY-S"},
    )
    rates = {p.id: p.demand_rate for p in model.products}
    families = {p.id: p.family for p in model.products}
    label = model.views["simple"].label(start, rates, families)
    assert label == "grey:ok black:ok natural:ok | grey"


def test_default_run_reproduces_the_rl_lowest_cover_profit() -> None:
    capsule = _capsule()
    assert capsule.experiment["policy"] == "lowest_cover"
    result = capsule.evaluate(seed=capsule.experiment["seed"])
    assert isinstance(result, dict) and result["status"] == "completed"
    assert result["replications"] == 50
    assert result["metrics"]["mean_profit"] == pytest.approx(
        GOLDEN["policies"]["lowest_cover"]["mean_profit"], abs=1e-6
    )


def test_a_cheaper_changeover_rule_earns_more() -> None:
    capsule = ScenarioCapsule.from_dict(
        {
            **_capsule().to_dict(),
            "experiment": {**_capsule().experiment, "policy": "family_campaign"},
        }
    )
    result = capsule.evaluate(seed=2024)
    assert isinstance(result, dict)
    assert result["metrics"]["mean_profit"] == pytest.approx(
        GOLDEN["policies"]["family_campaign"]["mean_profit"], abs=1e-6
    )


def test_scenario_commands_work_on_the_example(tmp_path: Path) -> None:
    assert main(["scenario", "validate", str(CAPSULE)]) == 0
    out = tmp_path / "run"
    assert (
        main(["scenario", "run", str(CAPSULE), "--reps", "2", "--seed", "5", "--out", str(out)])
        == 0
    )
    result = json.loads((out / "result.json").read_text())
    assert result["domain"] == "make_to_stock" and result["replications"] == 2
    assert (out / "rep-0000" / "moves.parquet").exists()


# --- the policy files ---------------------------------------------------------------------------


def _load(name: str) -> ModuleType:
    path = EXAMPLE / "policies" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"example_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _view(setup: str = "GRY-S", days_left: int = 30, **stock: float) -> Any:
    """A stand-in for the policy state, with the REQ-202 names."""
    model, _ = parse_step_model(_capsule().model)
    assert model is not None
    ids = model.product_ids
    held = {p: 10_000.0 for p in ids}
    held.update({k.replace("_", "-"): v for k, v in stock.items()})
    rate = {p.id: p.demand_rate for p in model.products}
    return SimpleNamespace(
        products=ids,
        stock=held,
        days_of_cover={p: held[p] / rate[p] for p in ids},
        demand_rate=rate,
        family={p.id: p.family for p in model.products},
        max_storage={p.id: p.max_storage for p in model.products},
        setup=setup,
        period=60 - days_left,
        days_left=days_left,
        actions=(*ids, "idle"),
    )


@pytest.mark.parametrize("name", ["lowest_stock_first", "family_campaign"])
def test_policy_files_are_short_documented_functions(name: str) -> None:
    module = _load(name)
    assert module.__doc__ and module.__doc__.strip()
    assert module.NAME == name
    assert callable(module.decide)
    assert set(module.SETTINGS) <= {"stop_days", "target_days"}
    lines = (EXAMPLE / "policies" / f"{name}.py").read_text().splitlines()
    assert len(lines) <= 45


def test_lowest_stock_first_runs_out_first_and_can_rest() -> None:
    decide = _load("lowest_stock_first").decide
    state = _view(**{"NAT_XL": 4.0})  # 1 day of stock
    assert decide(state) == "NAT-XL"
    assert decide(state, stop_days=0.5) == "idle"
    assert decide(_view()) in _view().actions
    # GRY-S sells the most, so equal stock gives it the least cover.
    assert decide(_view()) == "GRY-S"


def test_family_campaign_stays_in_the_family_then_moves() -> None:
    decide = _load("family_campaign").decide
    assert decide(_view()) == "idle"  # everything is stocked
    state = _view(**{"GRY_M": 5.0, "BLK_S": 1.0})
    assert decide(state) == "GRY-M"  # same family as the setup beats the lower BLK-S
    moved = _view(setup="GRY-S", **{"BLK_S": 1.0})
    assert decide(moved) == "BLK-S"  # the grey family is full, so move to the neediest
    winding_down = _view(days_left=0, **{"GRY_M": 5.0})
    assert decide(winding_down) == "idle"  # no point making stock on the last day


def test_readme_is_plain_and_linked() -> None:
    readme = (EXAMPLE / "README.md").read_text()
    for text in [
        readme,
        CAPSULE.read_text(),
        *(p.read_text() for p in (EXAMPLE / "policies").glob("*.py")),
    ]:
        assert "—" not in text and "–" not in text  # no em or en dashes
    assert "twinflow scenario run examples/molding-press/molding-press.twin.yaml" in readme
    index = (ROOT / "examples" / "README.md").read_text()
    assert "molding-press/" in index
