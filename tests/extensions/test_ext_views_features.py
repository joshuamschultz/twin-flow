"""T-259: view and feature extensions (COMP-162, REQ-218, REQ-219)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from twinflow.engines.contract import run_episode
from twinflow.engines.step.rules import make_decider
from twinflow.extensions.loader import load_project
from twinflow.extensions.project import Project
from twinflow.extensions.view import ModelFacts, policy_state
from twinflow.state.groups import default_views
from twinflow.state.markov import estimate
from twinflow.state.records import Move, Snapshot, flatten, unflatten

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "molding-press"
MakeProject = Callable[..., Path]


@pytest.fixture(scope="module")
def project() -> Project:
    return Project.open(EXAMPLE)


@pytest.fixture(scope="module")
def moves(project: Project) -> list[Move]:
    decide = make_decider("family_campaign", project.model)
    return [m for seed in range(3) for m in run_episode(project.engine(), decide, seed)]


def _view_state(project: Project, snapshot: Snapshot):  # type: ignore[no-untyped-def]
    facts = ModelFacts.from_step_model(project.model)
    return policy_state(snapshot, facts, project.engine().actions(snapshot))


# --- views -------------------------------------------------------------------------------------


def test_builtin_views_exist() -> None:
    assert set(load_project(None).names("view")) >= {"family_cover", "abc_cover"}


def test_family_cover_equals_the_default_family_view(
    project: Project, sample_states: list[Snapshot]
) -> None:
    label = load_project(None).get("view", "family_cover")
    family = default_views()["family"]
    facts = project.facts
    for snapshot in sample_states:
        assert label(_view_state(project, snapshot)) == family.label(
            snapshot, facts.demand_rate, facts.family
        )


def test_abc_cover_groups_by_abc_class(project: Project) -> None:
    label = load_project(None).get("view", "abc_cover")
    start = project.engine().start(0)  # 8 days of cover everywhere, GRY-S set up
    assert label(_view_state(project, start)) == "A:ok B:ok C:ok | grey"


def test_project_views_join_yaml_and_files(project: Project) -> None:
    assert {"full", "family", "simple", "family_cover", "abc_cover"} <= set(project.view_names)
    assert project.view("simple") is project.model.views["simple"]
    with pytest.raises(KeyError, match="simpel"):
        project.view("simpel")


def test_a_custom_label_drives_the_markov_chain(project: Project, moves: list[Move]) -> None:
    by_file = project.markov(moves, "family_cover")
    by_yaml = project.markov(moves, "family")
    assert by_file.view == "family_cover"
    assert by_file.states == by_yaml.states
    assert np.array_equal(by_file.matrix, by_yaml.matrix)
    assert by_file.money == by_yaml.money


def test_a_project_view_file_runs_through_markov(
    make_project: MakeProject, moves: list[Move]
) -> None:
    text = (
        "NAME = 'late'\n\n\ndef label(state):\n"
        "    return 'late' if state.days_left <= 10 else 'early'\n"
    )
    project = Project.open(make_project(files={"views/late.py": text}))
    chain = project.markov(moves, "late", top_k=5)
    assert set(chain.states) == {"early", "late"}
    assert chain.share["late"] == pytest.approx(10 / 60)
    direct = estimate(moves, project.view("late"))
    assert direct.states == chain.states


def test_a_yaml_view_and_a_view_file_with_one_name_is_a_problem(make_project: MakeProject) -> None:
    text = "NAME = 'simple'\n\n\ndef label(state):\n    return 'x'\n"
    project = Project.open(make_project(files={"views/simple.py": text}))
    (problem,) = [p for p in project.problems if p.code == "view_name_clash"]
    assert problem.file is not None and problem.file.name == "simple.py"
    assert project.view("simple") is project.model.views["simple"]


# --- features ----------------------------------------------------------------------------------


def test_builtin_features(project: Project) -> None:
    found = load_project(None)
    assert set(found.names("feature")) >= {"cover_days", "days_to_runout"}
    state = _view_state(project, project.engine().start(0))
    cover = found.get("feature", "cover_days")(state)
    assert cover == {f"cover_days.{p}": state.days_of_cover[p] for p in state.products}
    runout = found.get("feature", "days_to_runout")(state)
    assert runout == {"days_to_runout": 8.0, "days_to_runout_on_line": 8.0}


def test_no_features_by_default(project: Project) -> None:
    assert project.config.features == ()
    state = project.policy_state(project.engine().start(0))
    assert state.features == {}


def test_chosen_features_appear_in_the_policy_state(make_project: MakeProject) -> None:
    risk = "def features(state):\n    return {'risk': state.days_left / 100}\n"
    project = Project.open(
        make_project({"features": ["days_to_runout", "risk"]}, {"features/risk.py": risk})
    )
    assert project.ok, project.problems
    start = project.engine().start(0)
    state = project.policy_state(start)
    assert state.features == {"days_to_runout": 8.0, "days_to_runout_on_line": 8.0, "risk": 0.6}
    assert state.actions == (*project.model.product_ids, "idle")


def test_features_are_recorded_in_snapshot_extra(make_project: MakeProject) -> None:
    project = Project.open(make_project({"features": "days_to_runout"}))
    engine = project.engine()
    move = run_episode(engine, make_decider("lowest_cover", project.model), 0)[0]
    marked = project.annotate(move)
    assert marked.before.extra == {
        "feature.days_to_runout": 8.0,
        "feature.days_to_runout_on_line": 8.0,
    }
    assert set(marked.after.extra) == set(marked.before.extra)
    assert marked.ledger == move.ledger and marked.events == move.events
    assert unflatten(flatten(marked)) == marked  # survives moves.parquet


def test_an_unknown_feature_is_a_problem(make_project: MakeProject) -> None:
    project = Project.open(make_project({"features": ["cover_dayz"]}))
    (problem,) = project.problems
    assert problem.field == "experiment.features" and "cover_days" in problem.fix
