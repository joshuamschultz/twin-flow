"""T-261: every capsule setting has a documented default (COMP-163, REQ-221)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from twinflow.extensions.checker import CheckLimits, check_project
from twinflow.extensions.project import (
    DEFAULTS,
    MarkovSettings,
    Project,
    RaceSettings,
    read_config,
    render_defaults_markdown,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "molding-press"
TUNE_PAGE = ROOT / "docs" / "how-to" / "tune.md"
MakeProject = Callable[..., Path]

SDD_KEYS = {
    "experiment.score": "dollars",
    "experiment.demand": "poisson",
    "experiment.features": (),
    "experiment.race.episodes": 200,
    "experiment.race.seed": 0,
    "experiment.race.tune_episodes": 30,
    "experiment.race.tune_seed": 777,
    "experiment.race.interval": 0.95,
    "experiment.race.promote_rule": "interval_above_zero",
    "experiment.checker.time_limit_ms": 20.0,
    "experiment.checker.samples": 200,
}


def test_every_sdd_key_has_its_default() -> None:
    table = {s.key: s.default for s in DEFAULTS}
    for key, default in SDD_KEYS.items():
        assert table[key] == default, key
    assert len(table) == len(DEFAULTS)  # no key twice
    assert all(s.meaning.endswith(".") for s in DEFAULTS)


def test_an_empty_experiment_reads_every_default(tmp_path: Path) -> None:
    config, problems = read_config({}, tmp_path / "x.twin.yaml")
    assert problems == []
    assert {s.key: config[s.key] for s in DEFAULTS} == {s.key: s.default for s in DEFAULTS}
    assert config.race == RaceSettings()
    assert config.checker == CheckLimits()
    assert config.markov == MarkovSettings()


def test_every_key_is_read_from_the_capsule(tmp_path: Path) -> None:
    experiment = {
        "score": "margin_only",
        "demand": "seasonal",
        "features": ["cover_days"],
        "race": {
            "episodes": 40,
            "seed": 3,
            "tune_episodes": 5,
            "tune_seed": 9,
            "interval": 0.9,
            "promote_rule": "mean_above_zero",
            "top_policies": 2,
            "decision_timeout_s": 1.5,
        },
        "checker": {"time_limit_ms": 5, "samples": 50},
        "markov": {"view": "simple", "top_k": 8, "min_prob": 0.1},
    }
    config, problems = read_config(experiment, tmp_path / "x.twin.yaml")
    assert problems == []
    assert config.race == RaceSettings(40, 3, 5, 9, 0.9, "mean_above_zero", 2, 1.5)
    assert config.checker == CheckLimits(time_limit_ms=5.0, samples=50)
    assert config.markov == MarkovSettings("simple", 8, 0.1)
    assert (config.score, config.demand, config.features) == (
        "margin_only",
        "seasonal",
        ("cover_days",),
    )


@pytest.mark.parametrize(
    ("experiment", "field", "words"),
    [
        ({"race": {"episods": 10}}, "experiment.race.episods", "episodes"),
        ({"scor": "dollars"}, "experiment.scor", "score"),
        ({"checker": {"time_limit": 5}}, "experiment.checker.time_limit", "time_limit_ms"),
        ({"race": 5}, "experiment.race", "race:"),
        ({"race": {"episodes": 0}}, "experiment.race.episodes", "at least 1"),
        ({"race": {"episodes": 2.5}}, "experiment.race.episodes", "whole number"),
        ({"race": {"interval": 1.5}}, "experiment.race.interval", "between 0 and 1"),
        ({"race": {"promote_rule": "luck"}}, "experiment.race.promote_rule", "interval"),
        ({"checker": {"time_limit_ms": "fast"}}, "experiment.checker.time_limit_ms", "number"),
        ({"markov": {"min_prob": 2}}, "experiment.markov.min_prob", "at most 1"),
    ],
)
def test_bad_or_unknown_keys_are_plain_problems(
    tmp_path: Path, experiment: dict[str, object], field: str, words: str
) -> None:
    config, problems = read_config(experiment, tmp_path / "x.twin.yaml")
    (problem,) = problems
    assert problem.field == field and problem.kind == "config"
    assert words in problem.message + " " + problem.fix
    assert config["experiment.race.episodes"] == 200  # a bad value falls back to the default


def test_other_experiment_keys_are_known(tmp_path: Path) -> None:
    experiment = {"id": "x", "replications": 5, "seed": 1, "policy": "idle", "state_capture": {}}
    assert read_config(experiment, tmp_path / "x.twin.yaml")[1] == []


def test_the_markov_view_must_exist(make_project: MakeProject) -> None:
    project = Project.open(make_project({"markov": {"view": "simpel"}}))
    (problem,) = project.problems
    assert problem.field == "experiment.markov.view" and "simple" in problem.fix
    assert Project.open(make_project({"markov": {"view": "simple"}})).problems == ()


def test_the_tune_page_is_generated_from_the_table() -> None:
    page = render_defaults_markdown()
    for setting in DEFAULTS:
        assert f"`{setting.key}`" in page
    assert TUNE_PAGE.read_text(encoding="utf-8") == page, (
        "docs/how-to/tune.md is out of date: write render_defaults_markdown() to it"
    )


# --- the project check (REQ-203, REQ-222) ------------------------------------------------------


def test_the_example_project_checks_clean() -> None:
    assert check_project(Project.open(EXAMPLE)) == []


def test_check_project_uses_the_capsule_limits(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fake clock that only the slow policy moves, so the test never depends on machine
    # speed: each call of slow.py takes 3 ms, every other rule takes 0 ms.
    from types import SimpleNamespace

    from twinflow.extensions import checker

    fake = SimpleNamespace(now=0.0)
    monkeypatch.setitem(sys.modules, "twinflow_test_clock", fake)
    monkeypatch.setattr(checker, "CLOCK", lambda: fake.now)
    slow = (
        "import twinflow_test_clock\n\n\n"
        "def decide(state):\n    twinflow_test_clock.now += 0.003\n    return 'idle'\n"
    )
    folder = make_project(
        {"checker": {"time_limit_ms": 1, "samples": 10}}, {"policies/slow.py": slow}
    )
    problems = check_project(Project.open(folder))
    assert [p.code for p in problems] == ["slow"]
    assert "took 3.0 ms" in problems[0].message
    assert "limit is 1 ms" in problems[0].message


def test_check_project_lists_setting_problems_first(make_project: MakeProject) -> None:
    folder = make_project(
        {"score": "nope"}, {"policies/bad.py": "def decide(state):\n    return 1\n"}
    )
    codes = [p.code for p in check_project(Project.open(folder))]
    assert codes == ["unknown_name", "bad_action"]


def test_policies_are_checked_with_the_chosen_features(make_project: MakeProject) -> None:
    uses = "def decide(state):\n    return 'idle' if state.features['days_to_runout'] else 'idle'\n"
    folder = make_project({"features": ["days_to_runout"]}, {"policies/uses.py": uses})
    assert check_project(Project.open(folder)) == []


# --- what the plant does today (REQ-229, T-269) ------------------------------------------------

PLAIN = "def decide(state):\n    return 'idle'\n"


def test_the_baseline_is_the_first_policy_file_by_name(make_project: MakeProject) -> None:
    folder = make_project({"race": {}}, {"policies/zeta.py": PLAIN, "policies/alpha.py": PLAIN})
    project = Project.open(folder)
    assert project.config.race.baseline is None
    assert project.baseline == "alpha"
    assert project.baseline_source == "first policy file"


def test_the_baseline_is_lowest_cover_without_policy_files(make_project: MakeProject) -> None:
    project = Project.open(make_project({"race": {}}))
    assert (project.baseline, project.baseline_source) == ("lowest_cover", "built-in default")


def test_the_baseline_setting_wins(make_project: MakeProject) -> None:
    folder = make_project({"race": {"baseline": "kanban"}}, {"policies/alpha.py": PLAIN})
    project = Project.open(folder)
    assert project.problems == ()
    assert (project.baseline, project.baseline_source) == ("kanban", "setting")


def test_an_unknown_baseline_is_a_plain_problem(make_project: MakeProject) -> None:
    project = Project.open(make_project({"race": {"baseline": "lowest_covr"}}))
    (problem,) = project.problems
    assert problem.field == "experiment.race.baseline" and problem.code == "unknown_name"
    assert "lowest_cover" in problem.fix
    assert project.baseline == "lowest_cover"  # falls back to the default


def test_the_tune_page_explains_the_baseline_default() -> None:
    page = render_defaults_markdown()
    assert "| `experiment.race.baseline` | `first policy file, else lowest_cover` |" in page
