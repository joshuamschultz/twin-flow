"""T-236: the eleven built-in policy files equal their `engines/step/rules.py` twins
(COMP-155, REQ-204, D-142)."""

from __future__ import annotations

import tomllib
from importlib import resources
from itertools import product
from pathlib import Path

import pytest

from twinflow.engines.step import rules
from twinflow.engines.step.engine import StepEngine
from twinflow.engines.step.model import StepModel
from twinflow.extensions.loader import BUILTIN, Extension, load_project
from twinflow.extensions.view import ModelFacts, policy_state
from twinflow.state.records import Snapshot

ROOT = Path(__file__).resolve().parents[2]
NAMES = rules.RULE_NAMES
PURE = [name for name in NAMES if name != "random"]


@pytest.fixture(scope="module")
def builtins() -> dict[str, Extension]:
    found = load_project(None)
    assert found.problems == ()
    return {ext.name: ext for ext in found.of("policy")}


def _decide(ext: Extension, model: StepModel, snapshot: Snapshot, **settings: object) -> str:
    view = policy_state(
        snapshot, ModelFacts.from_step_model(model), StepEngine(model).actions(snapshot)
    )
    return str(ext(view, **settings))


def test_there_are_eleven_and_only_eleven(builtins: dict[str, Extension]) -> None:
    assert sorted(builtins) == sorted(NAMES)
    assert all(ext.source == "builtin" and ext.decision == "make_next" for ext in builtins.values())
    assert all(ext.summary for ext in builtins.values())


def test_summaries_say_what_the_rule_does(builtins: dict[str, Extension]) -> None:
    """Each summary is a plain sentence about the rule, not a fragment (TF-006 copy pass)."""
    for name, ext in builtins.items():
        first = ext.summary.split(". ")[0]
        assert len(first.split()) >= 4, f"{name}: {ext.summary!r} opens with a fragment"
        assert "Skip tomorrow" not in ext.summary and "come round" not in ext.summary


@pytest.mark.parametrize("name", PURE)
def test_equals_its_rule_on_500_states(
    name: str, builtins: dict[str, Extension], press_model: StepModel, sample_states: list[Snapshot]
) -> None:
    ext = builtins[name]
    mismatches = [
        (s.period, dict(s.stock), dict(s.setup))
        for s in sample_states
        if _decide(ext, press_model, s) != rules.choose(name, s, press_model)
    ]
    assert mismatches == []


@pytest.mark.parametrize("name", sorted(rules.GRIDS))
def test_grid_matches_and_every_grid_point_equals_its_rule(
    name: str, builtins: dict[str, Extension], press_model: StepModel, sample_states: list[Snapshot]
) -> None:
    ext = builtins[name]
    assert {k: list(v) for k, v in ext.grid.items()} == rules.GRIDS[name]
    keys = list(ext.grid)
    for values in product(*(ext.grid[k] for k in keys)):
        settings = dict(zip(keys, values, strict=True))
        for s in sample_states[:40]:
            assert _decide(ext, press_model, s, **settings) == rules.choose(
                name, s, press_model, **settings
            ), (settings, s.period)


@pytest.mark.parametrize("name", PURE)
def test_defaults_equal_the_rule_defaults(name: str, builtins: dict[str, Extension]) -> None:
    import inspect

    params = inspect.signature(builtins[name].target).parameters
    defaults = {k: p.default for k, p in params.items() if k != "state"}
    assert defaults == dict(rules.DEFAULT_SETTINGS[name])


def test_random_follows_the_same_dice(
    press_model: StepModel, sample_states: list[Snapshot]
) -> None:
    ext = {e.name: e for e in load_project(None).of("policy")}["random"]  # a fresh module
    twin = rules.make_decider("random", press_model, seed=0)
    assert [_decide(ext, press_model, s) for s in sample_states] == [twin(s) for s in sample_states]


def test_builtin_files_are_readable_package_data() -> None:
    folder = resources.files("twinflow.extensions").joinpath("builtin", "policies")
    texts = {
        p.name: p.read_text(encoding="utf-8") for p in folder.iterdir() if p.name.endswith(".py")
    }
    assert sorted(texts) == sorted(f"{n}.py" for n in NAMES)
    assert all('"""' in text for text in texts.values())
    assert (BUILTIN / "policies").is_dir()
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "extensions/builtin/*/*.py" in config["tool"]["setuptools"]["package-data"]["twinflow"]


def test_builtin_files_read_like_a_planner_wrote_them() -> None:
    for path in sorted((BUILTIN / "policies").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        code = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        assert len(text.splitlines()) <= 90, path.name
        assert "#" in text, f"{path.name} has no comments"
        assert "twinflow" not in text, f"{path.name} reaches into twinflow"
        assert "->" not in text and ": float" not in text, f"{path.name} uses type hints"
        assert len(code) <= 60, path.name
