"""T-237: the checker runs every extension kind on sample inputs (COMP-156, REQ-203,
REQ-213, D-147)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from twinflow.engines.step.model import StepModel
from twinflow.extensions import checker
from twinflow.extensions.checker import (
    CheckLimits,
    Samples,
    check_extension,
    check_extensions,
    step_samples,
)
from twinflow.extensions.kinds import KINDS
from twinflow.extensions.loader import Extension, Problem, load_file, load_project
from twinflow.extensions.view import PolicyState


def _load(tmp_path: Path, kind: str, text: str, stem: str = "case") -> Extension:
    folder = tmp_path / KINDS[kind].folder
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.py"
    path.write_text(text)
    ext = load_file(path, KINDS[kind])
    assert isinstance(ext, Extension), ext
    return ext


@pytest.fixture(scope="module")
def samples(press_model: StepModel) -> Samples:
    return step_samples(press_model, count=200, seed=0)


def test_samples_hold_an_episode_and_the_edge_states(
    samples: Samples, press_model: StepModel
) -> None:
    states = samples.states
    assert 60 < len(states) <= 200
    assert all(isinstance(s, PolicyState) for s in states)
    assert any(all(v == 0 for v in s.stock.values()) for s in states)  # empty stock
    assert any(all(s.stock[p] == s.max_storage[p] for p in s.products) for s in states)  # full
    assert any(s.days_left == 1 for s in states)  # last day
    assert len(samples.moves) == press_model.horizon
    assert samples.periods == press_model.horizon


def test_good_files_give_no_problems(samples: Samples) -> None:
    found = load_project(None)
    assert check_extensions(found, samples, CheckLimits()) == []


POLICY_CASES = {
    "raised": (
        "def decide(state):\n"
        "    if state.days_left < 5:\n"
        "        raise ValueError('late in the run')\n"
        "    return 'idle'\n",
        3,
    ),
    "bad_action": ("def decide(state):\n    return 'press-it'\n", 1),  # the decide line
    "slow": (
        "import twinflow_test_clock\n\n\n"
        "def decide(state):\n    twinflow_test_clock.now += 0.004\n    return 'idle'\n",
        4,  # the decide line
    ),
}


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The slow check reads a clock only test policies move: 4 ms per call for the slow
    ones, 0 ms for the rest. No test here depends on machine speed."""
    fake = SimpleNamespace(now=0.0)
    monkeypatch.setitem(sys.modules, "twinflow_test_clock", fake)
    monkeypatch.setattr(checker, "CLOCK", lambda: fake.now)
    return fake


@pytest.mark.parametrize("code", sorted(POLICY_CASES))
def test_each_policy_problem_names_file_line_and_fix(
    tmp_path: Path, samples: Samples, code: str
) -> None:
    text, line = POLICY_CASES[code]
    ext = _load(tmp_path, "policy", text)
    problems = check_extension(ext, samples, CheckLimits(time_limit_ms=2.0))
    assert [p.code for p in problems] == [code]
    (problem,) = problems
    assert problem.kind == "policy" and problem.name == "case"
    assert problem.file == ext.file and problem.line == line
    assert problem.fix.endswith(".") and len(problem.fix.split()) >= 4
    assert "case.py" in str(problem)


def test_raised_names_the_user_line_not_the_checker(tmp_path: Path, samples: Samples) -> None:
    text = "def helper(state):\n    return state.stock['NOPE']\n\n\ndef decide(state):\n" \
        "    return helper(state)\n"  # fmt: skip
    (problem,) = check_extension(_load(tmp_path, "policy", text), samples, CheckLimits())
    assert (problem.code, problem.line) == ("raised", 2)
    assert "KeyError" in problem.message


def test_bad_action_lists_what_is_allowed(tmp_path: Path, samples: Samples) -> None:
    ext = _load(tmp_path, "policy", "def decide(state):\n    return 'press-it'\n")
    (problem,) = check_extension(ext, samples, CheckLimits())
    assert "'press-it'" in problem.message and "GRY-S" in problem.message


def test_a_setting_from_the_grid_that_breaks_is_reported(tmp_path: Path, samples: Samples) -> None:
    text = (
        "SETTINGS = {'step': [1, 0]}\n\n\n"
        "def decide(state, step=1):\n"
        "    return state.actions[10 // step % len(state.actions)]\n"
    )
    (problem,) = check_extension(_load(tmp_path, "policy", text), samples, CheckLimits())
    assert problem.code == "bad_settings" and problem.line == 5
    assert "step=0" in problem.message


def test_slow_uses_the_limit(tmp_path: Path, samples: Samples) -> None:
    text = (
        "import twinflow_test_clock\n\n\n"
        "def decide(state):\n    twinflow_test_clock.now += 0.004\n    return 'idle'\n"
    )
    ext = _load(tmp_path, "policy", text)
    assert check_extension(ext, samples, CheckLimits(time_limit_ms=5.0)) == []
    assert [p.code for p in check_extension(ext, samples, CheckLimits(time_limit_ms=3.0))] == [
        "slow"
    ]


def test_dispatch_policies_run_on_dispatch_states(tmp_path: Path, samples: Samples) -> None:
    good = "DECISION = 'dispatch'\n\n\ndef decide(state):\n    return 'edd'\n"
    bad = "DECISION = 'dispatch'\n\n\ndef decide(state):\n    return 'idle'\n"
    assert check_extension(_load(tmp_path, "policy", good, "g"), samples, CheckLimits()) == []
    (problem,) = check_extension(_load(tmp_path, "policy", bad, "b"), samples, CheckLimits())
    assert problem.code == "bad_action" and "fifo" in problem.message


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("reward", "def reward(move):\n    return float('nan')\n"),
        ("reward", "def reward(move):\n    return 'lots'\n"),
        ("view", "def label(state):\n    return ''\n"),
        ("view", "def label(state):\n    return state.period\n"),
        ("feature", "def features(state):\n    return {'cover': float('inf')}\n"),
        ("feature", "def features(state):\n    return [1.0]\n"),
        ("demand", "def demand(period, rng, model):\n    return {p: -1 for p in model.products}\n"),
        (
            "demand",
            "def demand(period, rng, model):\n    return {p: 0.5 for p in model.products}\n",
        ),
        ("demand", "def demand(period, rng, model):\n    return {'GRY-S': 1}\n"),
        ("observation", "def observe(state):\n    return state.stock\n"),
    ],
)
def test_bad_values_of_every_kind(tmp_path: Path, samples: Samples, kind: str, text: str) -> None:
    ext = _load(tmp_path, kind, text)
    (problem,) = check_extension(ext, samples, CheckLimits())
    assert (problem.kind, problem.code, problem.file) == (kind, "bad_value", ext.file)
    assert problem.message.endswith(".") and problem.fix


def test_a_raise_in_any_kind_is_reported_with_its_line(tmp_path: Path, samples: Samples) -> None:
    text = "def reward(move):\n    x = 1\n    return move.ledger.total / 0\n"
    (problem,) = check_extension(_load(tmp_path, "reward", text), samples, CheckLimits())
    assert (problem.code, problem.line) == ("raised", 3)
    assert "ZeroDivisionError" in problem.message


def test_good_files_of_every_kind(tmp_path: Path, samples: Samples) -> None:
    cases = {
        "reward": "def reward(move):\n    return move.ledger.total\n",
        "view": "def label(state):\n    return f'day {state.period}'\n",
        "feature": "def features(state):\n    return {'low': float(min(state.stock.values()))}\n",
        "demand": "def demand(period, rng, model):\n    return {p: 2 for p in model.products}\n",
        "observation": "def observe(state):\n    return state.replace(setup=state.products[0])\n",
    }
    for kind, text in cases.items():
        assert check_extension(_load(tmp_path, kind, text), samples, CheckLimits()) == [], kind


def test_loader_problems_are_part_of_the_check(tmp_path: Path, samples: Samples) -> None:
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "broken.py").write_text("def decide(state)\n")
    problems = check_extensions(load_project(tmp_path), samples, CheckLimits())
    assert [(p.code, p.line) for p in problems] == [("import_error", 1)]
    assert all(isinstance(p, Problem) for p in problems)
