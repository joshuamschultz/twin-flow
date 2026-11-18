"""Shared fixtures for the TF-006 extension tests (Phase 3).

The molding-press example capsule is the RL repo's 12-product press (the golden problem),
so every extension test runs on the same model a planner sees.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from twinflow.engines.step.model import StepModel, parse_step_model
from twinflow.scenario import ScenarioCapsule
from twinflow.state.records import Snapshot

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "molding-press"
CAPSULE = EXAMPLE / "molding-press.twin.yaml"


@pytest.fixture(scope="session")
def press_model() -> StepModel:
    model, issues = parse_step_model(ScenarioCapsule.load(CAPSULE).model)
    assert model is not None, issues
    return model


def random_states(model: StepModel, count: int, seed: int) -> list[Snapshot]:
    """States spread over the whole space: any stock up to the cap, any setup, any day."""
    rng = np.random.default_rng(seed)
    states = []
    for _ in range(count):
        low = rng.random() < 0.5  # half the states near running out, to reach every branch
        stock = {
            p.id: float(rng.integers(0, (p.max_storage if not low else 4 * p.demand_rate) + 1))
            for p in model.products
        }
        setup = model.product_ids[int(rng.integers(len(model.products)))]
        day = int(rng.integers(0, model.horizon))
        states.append(Snapshot(t=float(day), period=day, stock=stock, setup={model.line_id: setup}))
    return states


@pytest.fixture(scope="session")
def sample_states(press_model: StepModel) -> list[Snapshot]:
    """500 sample states of the press (T-236: built-ins equal their rules on these)."""
    return random_states(press_model, 500, seed=11)


def write_project(
    folder: Path,
    experiment: dict | None = None,  # type: ignore[type-arg]
    files: dict | None = None,  # type: ignore[type-arg]
    model: dict | None = None,  # type: ignore[type-arg]
) -> Path:
    """Write a project folder: the molding-press capsule with experiment changes, plus files."""
    data = ScenarioCapsule.load(CAPSULE).to_dict()
    data["experiment"].update(experiment or {})
    data["model"].update(model or {})
    folder.mkdir(parents=True, exist_ok=True)
    ScenarioCapsule.from_dict(data).dump(folder / "press.twin.yaml")
    for name, text in (files or {}).items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return folder


@pytest.fixture(scope="session")
def shared_project(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """Like `make_project`, for module- and session-scoped fixtures (a fresh folder each)."""

    def make(experiment: dict | None = None, files: dict | None = None, model: dict | None = None):  # type: ignore[no-untyped-def]
        return write_project(tmp_path_factory.mktemp("project"), experiment, files, model)

    return make


@pytest.fixture
def make_project(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A project folder: the molding-press capsule with experiment changes, plus files."""

    def make(experiment: dict | None = None, files: dict | None = None, model: dict | None = None):  # type: ignore[no-untyped-def]
        return write_project(tmp_path / "project", experiment, files, model)

    return make
