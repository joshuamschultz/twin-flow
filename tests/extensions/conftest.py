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


GOLDEN = Path(__file__).with_name("race_golden.json")
MASKS = ["full", "no_setup", "bucketed", "own_product", "no_days_left", "bucketed+no_days_left"]


@pytest.fixture(scope="session")
def race_golden() -> dict:  # type: ignore[type-arg]
    """The RL repo's race and analysis numbers (written once by make_race_golden.py)."""
    import json

    return json.loads(GOLDEN.read_text())  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def golden_race(shared_project, race_golden):  # type: ignore[no-untyped-def]
    """RL notebook 2's profit-by-view race: (s,S), family campaign and lowest cover tuned
    on 30 episodes of seed 777, then raced on 150 episodes of seed 11 under every mask."""
    from twinflow.extensions.project import Project, RaceSettings
    from twinflow.extensions.race import race

    table = race_golden["observation_table"]
    tuning = race_golden["tuning"]
    project = Project.open(shared_project())
    settings = RaceSettings(
        episodes=table["episodes"],
        seed=table["seed"],
        tune_episodes=tuning["episodes"],
        tune_seed=tuning["seed"],
    )
    return project, race(
        project,
        ["s_big_s", "family_campaign", "lowest_cover"],
        settings=settings,
        observations=MASKS,
    )
