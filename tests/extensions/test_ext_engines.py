"""T-263: engine extensions and `model.domain: custom` (COMP-164, REQ-223, D-139)."""

from __future__ import annotations

from pathlib import Path

import pytest

from twinflow.engines.contract import run_episode
from twinflow.extensions.checker import CheckLimits, check_engine, check_project
from twinflow.extensions.kinds import KINDS
from twinflow.extensions.loader import Extension, load_file
from twinflow.extensions.project import Project, ProjectError
from twinflow.scenario import ScenarioCapsule

TOY = '''"""A toy engine: two products, one machine, random demand."""

import numpy

from twinflow.state.records import Ledger, Move, Snapshot

NAME = "toy"


class Engine:
    name = "toy"

    def __init__(self, model):
        self.horizon = int(model.get("horizon", 10))
        self.products = list(model.get("products", ["A", "B"]))

    def facts(self):
        rates = {p: 1.0 for p in self.products}
        return {"products": tuple(self.products), "demand_rate": rates, "horizon": self.horizon}

    def start(self, seed, from_state=None):
        self.rng = numpy.random.default_rng(seed)
        stock = {p: 2.0 for p in self.products}
        self.state = Snapshot(t=0.0, period=0, stock=stock, setup={"m": self.products[0]})
        return self.state

    def actions(self, state):
        return (*self.products, "idle")

    def step(self, action):
        before = self.state
        stock = dict(before.stock)
        if action != "idle":
            stock[action] += 2
        demand = {p: int(self.rng.poisson(1.0)) for p in self.products}
        sold = {p: min(stock[p], demand[p]) for p in self.products}
        for p in self.products:
            stock[p] -= sold[p]
        setup = before.setup["m"] if action == "idle" else action
        after = Snapshot(t=before.t + 1, period=before.period + 1, stock=stock, setup={"m": setup})
        events = {f"demand.{p}": float(demand[p]) for p in self.products}
        self.state = after
        ledger = Ledger(revenue=float(sum(sold.values())))
        return Move(before.period, before, action, "m", "period", events, after, ledger)

    def done(self):
        return self.state.period >= self.horizon
'''

FIRST = "def decide(state):\n    return state.actions[0]\n"
IDLE = "def decide(state):\n    return 'idle'\n"


def _capsule(engine: str = "toy", **model: object) -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "required_capabilities": [],
        "model": {"domain": "custom", "engine": engine, "horizon": 10, **model},
        "snapshot": {"id": "s", "as_of": "2026-11-17T08:00:00-06:00", "model_revision": "1"},
        "experiment": {"id": "toy"},
        "assumptions": [],
        "provenance": {"source": "test"},
    }


def _project(tmp_path: Path, engine_text: str = TOY, engine: str = "toy") -> Path:
    folder = tmp_path / "toy-project"
    (folder / "engines").mkdir(parents=True)
    (folder / "policies").mkdir()
    (folder / "engines" / "toy.py").write_text(engine_text)
    (folder / "policies" / "first.py").write_text(FIRST)
    (folder / "policies" / "rest.py").write_text(IDLE)
    ScenarioCapsule.from_dict(_capsule(engine)).dump(folder / "toy.twin.yaml")
    return folder


def _engine_ext(tmp_path: Path, text: str) -> Extension:
    path = tmp_path / "engines" / "e.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    ext = load_file(path, KINDS["engine"])
    assert isinstance(ext, Extension), ext
    return ext


def test_a_custom_project_opens_with_its_engine(tmp_path: Path) -> None:
    project = Project.open(_project(tmp_path))
    assert project.domain == "custom" and project.model is None
    assert project.engine_extension is not None and project.engine_extension.name == "toy"
    assert project.facts.products == ("A", "B") and project.facts.horizon == 10
    assert project.actions == ("A", "B", "idle")
    assert project.ok and project.problems == ()


def test_the_toy_engine_runs_through_run_episode(tmp_path: Path) -> None:
    project = Project.open(_project(tmp_path))
    engine = project.engine()
    decide = project.decider(project.extensions.get("policy", "first"), engine)
    moves = run_episode(engine, decide, 3)
    assert len(moves) == 10 and {m.action for m in moves} == {"A"}
    state = project.policy_state(moves[0].after)
    assert state.setup == "A" and state.days_left == 9


def test_every_policy_sees_the_same_dice(tmp_path: Path) -> None:
    project = Project.open(_project(tmp_path))
    plan = project.episodes(4, seed=5)
    assert len(plan.seeds) == 4 and len(set(plan.seeds)) == 4
    assert plan.seeds == project.episodes(4, seed=5).seeds
    assert plan.seeds != project.episodes(4, seed=777).seeds  # tuning dice differ (D-143)

    def demand(policy: str) -> list[list[float]]:
        decide = project.decider(project.extensions.get("policy", policy), plan.engine)
        runs = [run_episode(plan.engine, decide, seed) for seed in plan.seeds]
        return [[m.events["demand.A"] for m in run] for run in runs]

    assert demand("first") == demand("rest")


def test_the_toy_engine_draws_markov_maps(tmp_path: Path) -> None:
    folder = _project(tmp_path)
    (folder / "views").mkdir()
    (folder / "views" / "setup.py").write_text("def label(state):\n    return state.setup\n")
    project = Project.open(folder)
    engine = project.engine()
    moves = run_episode(
        engine, project.decider(project.extensions.get("policy", "first"), engine), 0
    )
    assert project.markov(moves, "setup").states == ("A",)
    assert project.markov(moves, "full").moves == 10
    assert {"full", "family", "setup"} <= set(project.view_names)


def test_the_project_check_runs_on_engine_states(tmp_path: Path) -> None:
    folder = _project(tmp_path)
    (folder / "policies" / "bad.py").write_text("def decide(state):\n    return 'C'\n")
    problems = check_project(Project.open(folder))
    assert [(p.code, p.name) for p in problems] == [("bad_action", "bad")]


def test_a_class_missing_step_is_a_problem_with_file_and_line(tmp_path: Path) -> None:
    text = TOY.replace("    def step(self, action):", "    def stop(self, action):")
    ext = _engine_ext(tmp_path, text)
    (problem,) = check_engine(ext, {"horizon": 3})
    assert (problem.code, problem.file, problem.line) == ("bad_engine", ext.file, 10)
    assert "step" in problem.message and "def step" in problem.fix
    with pytest.raises(ProjectError) as caught:
        Project.open(_project(tmp_path / "p", text))
    assert [p.code for p in caught.value.problems] == ["bad_engine"]


RAISE_IN_START = "        raise RuntimeError('no dice')\n        self.rng = numpy"
STEP_RETURNS_5 = "        return 5\n        return Move(before.period"
START_RETURNS_TEXT = "        return 'day 0'\n\n    def actions"
BREAKS = [
    ("        self.rng = numpy", RAISE_IN_START, "raised", "start raised RuntimeError"),
    ('        return (*self.products, "idle")', "        return ()", "bad_engine", "actions"),
    ("        return Move(before.period", STEP_RETURNS_5, "bad_engine", "step returned"),
    ("        return self.state\n\n    def actions", START_RETURNS_TEXT, "bad_engine", "start"),
]


@pytest.mark.parametrize(("old", "new", "code", "words"), BREAKS)
def test_engine_contract_breaks_are_plain_problems(
    tmp_path: Path, old: str, new: str, code: str, words: str
) -> None:
    assert old in TOY
    ext = _engine_ext(tmp_path, TOY.replace(old, new, 1))
    (problem,) = check_engine(ext, {"horizon": 3}, limits=CheckLimits())
    assert problem.code == code and words in problem.message, problem
    assert problem.file == ext.file and problem.line is not None


def test_an_unknown_engine_name_is_a_project_error(tmp_path: Path) -> None:
    with pytest.raises(ProjectError) as caught:
        Project.open(_project(tmp_path, engine="tyo"))
    (problem,) = caught.value.problems
    assert problem.field == "model.engine" and "toy" in problem.fix


def test_a_custom_capsule_on_the_service_path_says_where_it_runs(tmp_path: Path) -> None:
    capsule = ScenarioCapsule.from_dict(_capsule())
    (issue,) = capsule.validate()
    assert issue.path == "$.model.domain"
    assert "custom engines run from the command line or Python" in issue.message
    from twinflow.domain import registry

    with pytest.raises(KeyError, match="custom engines run from the command line or Python"):
        registry.get("custom")
    with pytest.raises(KeyError, match="command line or Python"):
        capsule.evaluate(seed=0)


def test_engine_files_in_a_make_to_stock_project_are_only_loaded(make_project) -> None:  # type: ignore[no-untyped-def]
    folder = make_project(files={"engines/broken.py": "class Engine:\n    pass\n"})
    assert check_project(Project.open(folder)) == []
