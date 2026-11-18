"""T-258: reward extensions and `experiment.score` (COMP-162, COMP-163, REQ-217, D-152)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from twinflow.engines.contract import run_episode
from twinflow.engines.step.rules import make_decider
from twinflow.extensions.checker import CheckLimits, check_extension, step_samples
from twinflow.extensions.loader import load_project
from twinflow.extensions.project import Project, ProjectError
from twinflow.state.records import Move

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "molding-press"
MakeProject = Callable[..., Path]


@pytest.fixture(scope="module")
def moves() -> list[Move]:
    project = Project.open(EXAMPLE)
    engine = project.engine()
    return run_episode(engine, make_decider("family_campaign", project.model), 0)


def test_three_builtin_rewards() -> None:
    found = load_project(None)
    assert set(found.names("reward")) >= {"dollars", "dollars_with_service", "margin_only"}


def test_builtin_reward_values(moves: list[Move]) -> None:
    rewards = {e.name: e for e in load_project(None).of("reward")}
    for move in moves:
        lost = sum(v for k, v in move.events.items() if k.startswith("lost."))
        assert rewards["dollars"](move) == move.ledger.total
        assert rewards["margin_only"](move) == move.ledger.revenue - move.ledger.production
        assert rewards["dollars_with_service"](move) == pytest.approx(
            move.ledger.total - 2.0 * lost
        )
        assert rewards["dollars_with_service"](move, penalty_per_lost_unit=0.0) == (
            move.ledger.total
        )
    assert any(v > 0 for m in moves for k, v in m.events.items() if k.startswith("lost."))


def test_the_default_score_is_dollars(moves: list[Move]) -> None:
    project = Project.open(EXAMPLE)
    assert project.config.score == "dollars"
    assert project.reward.name == "dollars"
    assert [project.score(m) for m in moves] == [m.ledger.total for m in moves]


def test_experiment_score_picks_a_reward(make_project: MakeProject, moves: list[Move]) -> None:
    project = Project.open(make_project({"score": "margin_only"}))
    assert project.reward.name == "margin_only" and project.problems == ()
    assert project.score(moves[3]) == moves[3].ledger.revenue - moves[3].ledger.production


def test_a_project_reward_can_be_the_score(make_project: MakeProject, moves: list[Move]) -> None:
    folder = make_project(
        {"score": "half"},
        {
            "rewards/half.py": "NAME = 'half'\n\n\ndef reward(move):\n"
            "    return move.ledger.total / 2\n"
        },
    )
    project = Project.open(folder)
    assert project.reward.source == "project"
    assert project.score(moves[0]) == moves[0].ledger.total / 2


def test_an_unknown_score_is_a_plain_problem(make_project: MakeProject) -> None:
    project = Project.open(make_project({"score": "dolars"}))
    (problem,) = [p for p in project.problems if p.severity == "error"]
    assert problem.field == "experiment.score" and problem.file is not None
    assert problem.file.name == "press.twin.yaml"
    assert "dolars" in problem.message and "dollars" in problem.fix
    assert not project.ok
    with pytest.raises(KeyError, match="dolars"):
        project.score(None)  # type: ignore[arg-type]


def test_a_score_that_is_not_text_is_a_problem(make_project: MakeProject) -> None:
    project = Project.open(make_project({"score": 3}))
    assert [p.field for p in project.problems] == ["experiment.score"]


@pytest.mark.parametrize(
    "body", ["return float('nan')", "return 'big'", "return None", "return [1.0]"]
)
def test_bad_reward_returns_name_file_and_line(
    make_project: MakeProject, body: str, press_model: object
) -> None:
    folder = make_project(files={"rewards/bad.py": f"\n\ndef reward(move):\n    {body}\n"})
    project = Project.open(folder)
    ext = project.extensions.get("reward", "bad")
    (problem,) = check_extension(ext, step_samples(project.model), CheckLimits())
    assert (problem.code, problem.line, problem.file) == ("bad_value", 3, ext.file)
    assert "real number" in problem.message


def test_a_folder_without_a_capsule_is_a_project_error(tmp_path: Path) -> None:
    with pytest.raises(ProjectError) as caught:
        Project.open(tmp_path)
    (problem,) = caught.value.problems
    assert problem.code == "no_capsule" and ".twin.yaml" in problem.fix
    assert "no_capsule" not in str(caught.value) and ".twin.yaml" in str(caught.value)


def test_a_broken_model_is_a_project_error(make_project: MakeProject) -> None:
    with pytest.raises(ProjectError) as caught:
        Project.open(make_project(model={"horizon": -1}))
    assert any(p.field == "model.horizon" for p in caught.value.problems)
