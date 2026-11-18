"""T-260: demand extensions, drawn once per seed (COMP-162, COMP-163, REQ-220, D-135, D-153)."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from twinflow.engines.contract import run_episode
from twinflow.engines.step.engine import demand_draws
from twinflow.extensions.checker import CheckLimits, check_extension, step_samples
from twinflow.extensions.loader import load_project
from twinflow.extensions.project import Project, ProjectError

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "molding-press"
GOLDEN = json.loads((ROOT / "tests" / "engines" / "step" / "golden.json").read_text())
MakeProject = Callable[..., Path]


@pytest.fixture(scope="module")
def project() -> Project:
    return Project.open(EXAMPLE)


def test_builtin_demand_files() -> None:
    assert set(load_project(None).names("demand")) >= {"poisson", "seasonal", "promotion"}


def test_poisson_is_the_default_and_equals_the_d135_draws(project: Project) -> None:
    assert project.config.demand == "poisson"
    draws = project.demand_draws(GOLDEN["episodes"], GOLDEN["seed"])
    assert draws.dtype == np.int64
    assert draws.shape == (GOLDEN["episodes"], project.model.horizon, len(project.model.products))
    assert np.array_equal(draws, demand_draws(project.model, GOLDEN["episodes"], GOLDEN["seed"]))


def test_the_golden_profit_holds_on_extension_draws(project: Project) -> None:
    draws = project.demand_draws(GOLDEN["episodes"], GOLDEN["seed"])
    engine = project.engine(draws)
    policy = project.extensions.get("policy", "lowest_cover")

    def decide(snapshot):  # type: ignore[no-untyped-def]
        return policy(project.policy_state(snapshot))

    totals = [
        sum(m.ledger.total for m in run_episode(engine, decide, episode))
        for episode in range(GOLDEN["episodes"])
    ]
    golden = GOLDEN["policies"]["lowest_cover"]
    assert totals == pytest.approx(golden["episode_totals"], abs=1e-6)
    assert float(np.mean(totals)) == pytest.approx(golden["mean_profit"], abs=1e-6)


def test_draws_are_made_once_per_seed(project: Project) -> None:
    first = project.demand_draws(4, 9)
    assert np.array_equal(first, project.demand_draws(4, 9))
    assert not np.array_equal(first, project.demand_draws(4, 10))


def _seasonal_mean_check(ext, facts, settings, periods=20_000):  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(5)
    got = np.array(
        [[ext(t, rng, facts, **settings)[p] for p in facts.products] for t in range(periods)],
        dtype=float,
    )
    weekly = settings.get("weekly", ext.settings["weekly"])
    amplitude = settings.get("yearly_amplitude", ext.settings["yearly_amplitude"])
    peak = settings.get("peak_day", ext.settings["peak_day"])
    rates = np.array([facts.demand_rate[p] for p in facts.products])
    for day in range(7):
        rows = np.arange(day, periods, 7)
        lift = np.array(
            [weekly[day] * (1 + amplitude * math.cos(2 * math.pi * (t - peak) / 365)) for t in rows]
        )
        expected = rates * lift.mean()
        assert got[rows].mean(axis=0) == pytest.approx(expected, rel=0.02), day


def test_seasonal_mean_matches_its_multipliers(project: Project) -> None:
    ext = load_project(None).get("demand", "seasonal")
    _seasonal_mean_check(ext, project.facts, {})
    _seasonal_mean_check(
        ext,
        project.facts,
        {"weekly": [1.5, 1.0, 1.0, 1.0, 1.0, 0.5, 0.0], "yearly_amplitude": 0.0},
    )


def test_promotion_lifts_only_its_periods(project: Project) -> None:
    ext = load_project(None).get("demand", "promotion")
    rng = np.random.default_rng(1)
    facts = project.facts
    settings = {"periods": [3], "lift": 3.0, "products": ["GRY-S"]}
    lifted = [ext(3, rng, facts, **settings)["GRY-S"] for _ in range(4000)]
    plain = [ext(4, rng, facts, **settings)["GRY-S"] for _ in range(4000)]
    other = [ext(3, rng, facts, **settings)["BLK-S"] for _ in range(4000)]
    assert np.mean(lifted) == pytest.approx(180.0, rel=0.02)
    assert np.mean(plain) == pytest.approx(60.0, rel=0.02)
    assert np.mean(other) == pytest.approx(45.0, rel=0.02)


def test_experiment_demand_picks_the_file(make_project: MakeProject) -> None:
    flat = "def demand(period, rng, model):\n    return {p: 7 for p in model.products}\n"
    project = Project.open(make_project({"demand": "flat"}, {"demand/flat.py": flat}))
    draws = project.demand_draws(2, 0)
    assert (draws == 7).all()
    engine = project.engine(draws)
    move = engine.step("idle") if engine.start(1) else None
    assert move is not None and move.events["demand.GRY-S"] == 7.0


def test_an_unknown_demand_is_a_problem(make_project: MakeProject) -> None:
    project = Project.open(make_project({"demand": "seasonl"}))
    (problem,) = project.problems
    assert problem.field == "experiment.demand" and "seasonal" in problem.fix


@pytest.mark.parametrize(
    ("body", "words"),
    [
        ("return {p: -2 for p in model.products}", "0 or more"),
        ("return {p: 1.5 for p in model.products}", "whole number"),
        ("return {p: int(rng.poisson(model.demand_rate[p])) for p in model.products[1:]}", "GRY-S"),
    ],
)
def test_bad_demand_is_a_checker_problem_and_stops_the_draws(
    make_project: MakeProject, body: str, words: str
) -> None:
    text = f"def demand(period, rng, model):\n    {body}\n"
    project = Project.open(make_project({"demand": "bad"}, {"demand/bad.py": text}))
    ext = project.extensions.get("demand", "bad")
    (problem,) = check_extension(ext, step_samples(project.model), CheckLimits())
    assert (problem.code, problem.line) == ("bad_value", 1) and words in problem.message
    with pytest.raises(ProjectError) as caught:
        project.demand_draws(1, 0)
    (stop,) = caught.value.problems
    assert stop.code == "bad_value" and stop.file == ext.file and words in stop.message


def test_a_demand_file_that_raises_stops_the_draws_with_its_line(
    make_project: MakeProject,
) -> None:
    text = "def demand(period, rng, model):\n    x = 1\n    return {}[period]\n"
    project = Project.open(make_project({"demand": "bad"}, {"demand/bad.py": text}))
    with pytest.raises(ProjectError) as caught:
        project.demand_draws(1, 0)
    (stop,) = caught.value.problems
    assert (stop.code, stop.line) == ("raised", 3) and "period 0" in stop.message
