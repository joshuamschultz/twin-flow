"""Fixture runs for the TF-006 CLI tests: a step run and a DES run with state capture."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from twinflow.application.scenarios import evaluate_capsule
from twinflow.cli.main import main
from twinflow.scenario import load

ROOT = Path(__file__).resolve().parents[2]
PRESS = ROOT / "examples" / "molding-press" / "molding-press.twin.yaml"
SHOP = ROOT / "examples" / "cnc-shop" / "cnc-shop.twin.yaml"

ECONOMICS = {
    "things": {
        "shaft": {"price": 90, "unit_cost": 40, "late_cost_per_day": 500},
        "bracket": {"price": 60, "unit_cost": 25, "late_cost_per_day": 500},
        "round_bar": {"holding_per_unit_day": 0.5},
    },
    "setup": {"cost": 75},
}


@pytest.fixture(scope="session")
def step_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two replications of the molding press; folder holds scenario.json and rep-NNNN."""
    out = tmp_path_factory.mktemp("step") / "run"
    code = main(["scenario", "run", str(PRESS), "--reps", "2", "--seed", "5", "--out", str(out)])
    assert code == 0
    return out


@pytest.fixture(scope="session")
def des_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two replications of the CNC shop with state capture on."""
    out = tmp_path_factory.mktemp("des") / "run"
    data: dict[str, Any] = copy.deepcopy(load(SHOP).to_dict())
    data["experiment"]["state_capture"] = {"every_hours": 8}
    data["model"]["economics"] = ECONOMICS
    evaluate_capsule(data, out, 2, 0, lambda: False)
    return out
