"""T-240: the race tunes each policy's SETTINGS on separate seeds (COMP-157, REQ-206, D-143).

Every grid point is tried on the tuning draws (`experiment.race.tune_episodes` on
`tune_seed`), never on the scoring draws; the best one (the first, on a tie) is used for the
race and recorded in race.json; a policy without SETTINGS runs once, untuned.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from itertools import product
from pathlib import Path

import numpy as np
import pytest

from twinflow.extensions.project import Project
from twinflow.extensions.race import RaceResult, race, run_policy

MakeProject = Callable[..., Path]

RACE = {"episodes": 4, "seed": 3, "tune_episodes": 3, "tune_seed": 41, "top_policies": 2}

PICKY = '''
"""Lowest cover, but setting 99 breaks it."""
NAME = "picky"
SETTINGS = {"stop_days": [5.0, 99.0, 15.0]}


def decide(state, stop_days=10.0):
    if stop_days == 99.0:
        raise ValueError("no")
    worst = min(state.products, key=lambda p: state.days_of_cover[p])
    return "idle" if state.days_of_cover[worst] >= stop_days else worst
'''


@pytest.fixture(scope="module")
def tuned(shared_project: MakeProject) -> tuple[Project, RaceResult]:
    project = Project.open(shared_project({"race": RACE}, files={"policies/picky.py": PICKY}))
    return project, race(project, ["fixed_wheel", "idle", "picky"])


def _mean_score(project: Project, name: str, settings: dict, episodes: int, seed: int) -> float:
    plan = project.episodes(episodes, seed)
    policy = project.extensions.get("policy", name)
    runs = run_policy(project, policy, plan, settings=settings)
    return float(np.mean([sum(project.score(m) for m in moves) for moves in runs]))


def test_every_grid_point_is_tried_on_the_tuning_seeds(tuned: tuple[Project, RaceResult]) -> None:
    project, result = tuned
    grid = project.extensions.get("policy", "fixed_wheel").grid
    trials = [t for t in result.trials if t.policy == "fixed_wheel"]
    keys = list(grid)
    assert [dict(t.settings) for t in trials] == [
        dict(zip(keys, values, strict=True)) for values in product(*grid.values())
    ]
    for trial in trials:
        expected = _mean_score(project, "fixed_wheel", dict(trial.settings), 3, 41)
        assert trial.score == pytest.approx(expected)
        on_race_seeds = _mean_score(project, "fixed_wheel", dict(trial.settings), 3, 3)
        assert trial.score != pytest.approx(on_race_seeds)  # tuning never sees race dice


def test_the_best_setting_is_raced_and_saved(
    tuned: tuple[Project, RaceResult], tmp_path: Path
) -> None:
    project, result = tuned
    trials = [t for t in result.trials if t.policy == "fixed_wheel"]
    best = max(trials, key=lambda t: t.score)  # max keeps the first on a tie
    row = result.result("fixed_wheel")
    assert dict(row.settings) == dict(best.settings)
    raced = _mean_score(project, "fixed_wheel", dict(best.settings), 4, 3)
    assert row.score.mean == pytest.approx(raced)
    result.write(tmp_path)
    saved = json.loads((tmp_path / "race.json").read_text())
    by_policy = {r["policy"]: r for r in saved["results"]}
    assert by_policy["fixed_wheel"]["settings"] == dict(best.settings)
    assert len([t for t in saved["tuning"] if t["policy"] == "fixed_wheel"]) == len(trials)


def test_a_policy_without_settings_runs_once(tuned: tuple[Project, RaceResult]) -> None:
    _, result = tuned
    assert [t for t in result.trials if t.policy == "idle"] == []
    assert dict(result.result("idle").settings) == {}


def test_a_grid_point_that_breaks_is_recorded_and_left_out(
    tuned: tuple[Project, RaceResult],
) -> None:
    _, result = tuned
    trials = {t.settings["stop_days"]: t for t in result.trials if t.policy == "picky"}
    assert set(trials) == {5.0, 99.0, 15.0}
    broken = trials[99.0]
    assert broken.problem is not None and broken.problem.code == "raised"
    assert broken.score is None
    assert result.result("picky").settings["stop_days"] in (5.0, 15.0)


def test_fixed_settings_skip_tuning(make_project: MakeProject) -> None:
    project = Project.open(make_project({"race": RACE}))
    result = race(project, ["fixed_wheel"], fixed={"fixed_wheel": {"days_per_slot": 4}})
    assert result.trials == ()
    assert dict(result.result("fixed_wheel").settings) == {"days_per_slot": 4}


def test_a_policy_broken_at_every_setting_is_skipped(make_project: MakeProject) -> None:
    always = PICKY.replace("if stop_days == 99.0:\n        raise", "if True:\n        raise")
    project = Project.open(make_project({"race": RACE}, files={"policies/picky.py": always}))
    result = race(project, ["picky", "idle"])
    assert [r.policy for r in result.ranking()] == ["idle"]
    assert [p.name for p in result.skipped] == ["picky"]
