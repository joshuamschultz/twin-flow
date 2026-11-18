"""T-235 / T-257: one loader for every extension kind (COMP-155, COMP-161, REQ-200, REQ-201,
REQ-216, D-146, D-150, D-151)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from twinflow.extensions.kinds import KINDS, check_return
from twinflow.extensions.loader import (
    Extension,
    ExtensionNotFound,
    Problem,
    discover,
    load_file,
    load_project,
    module_name,
)

GOOD_POLICY = '''"""Run the first product, always.

Second paragraph.
"""

NAME = "first_one"
DECISION = "make_next"
SETTINGS = {"days": [1, 2, 3]}


def decide(state, days=1):
    return state.products[0]
'''

ONE_OF_EACH = {
    "policies/keep.py": "def decide(state):\n    return 'idle'\n",
    "rewards/money.py": "def reward(move):\n    return move.ledger.total\n",
    "views/plain.py": "def label(state):\n    return 'all'\n",
    "features/risk.py": "def features(state):\n    return {'risk': 0.5}\n",
    "demand/flat.py": "def demand(period, rng, model):\n"
    "    return {p: 1 for p in model.products}\n",
    "observations/same.py": "def observe(state):\n    return state\n",
    "engines/toy.py": "NAME = 'toy'\n\n\nclass Engine:\n    def __init__(self, model):\n"
    "        self.model = model\n",
}


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _empty_builtins(tmp_path: Path) -> Path:
    path = tmp_path / "no-builtins"
    path.mkdir()
    return path


# --- the kinds table ---------------------------------------------------------------------------


def test_kinds_table_names_folder_and_function() -> None:
    table = {k.name: (k.folder, k.function) for k in KINDS.values()}
    assert table == {
        "policy": ("policies", "decide"),
        "reward": ("rewards", "reward"),
        "view": ("views", "label"),
        "feature": ("features", "features"),
        "demand": ("demand", "demand"),
        "observation": ("observations", "observe"),
        "engine": ("engines", "Engine"),
    }
    assert KINDS["policy"].settings == "grid"
    assert {k.settings for name, k in KINDS.items() if name != "policy"} == {"values"}


@pytest.mark.parametrize(
    ("kind", "value", "ok"),
    [
        ("policy", "A", True),
        ("policy", "Z", False),
        ("policy", 3, False),
        ("reward", 1.5, True),
        ("reward", 2, True),
        ("reward", float("nan"), False),
        ("reward", "1", False),
        ("reward", True, False),
        ("view", "low | grey", True),
        ("view", "", False),
        ("view", "x" * 80, False),
        ("view", 7, False),
        ("feature", {"risk": 0.5}, True),
        ("feature", {"risk": float("inf")}, False),
        ("feature", {3: 1.0}, False),
        ("feature", [1.0], False),
        ("demand", {"A": 3, "B": 0}, True),
        ("demand", {"A": -1, "B": 0}, False),
        ("demand", {"A": 1.5, "B": 0}, False),
        ("demand", {"A": 1}, False),
        ("demand", {"A": 1, "B": 1, "C": 1}, False),
    ],
)
def test_return_checks(kind: str, value: object, ok: bool) -> None:
    problem = check_return(kind, value, allowed=("A", "idle"), products=("A", "B"))
    assert (problem is None) is ok, problem
    if problem is not None:
        assert isinstance(problem, str) and problem.endswith(".")


def test_numpy_whole_numbers_are_demand() -> None:
    import numpy as np

    assert check_return("demand", {"A": np.int64(2)}, products=("A",)) is None


# --- discovery ---------------------------------------------------------------------------------


def test_discovery_is_sorted_and_skips_private_and_non_python(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "policies/b.py": "",
            "policies/a.py": "",
            "policies/_helper.py": "",
            "policies/notes.txt": "",
            "policies/sub/c.py": "",
        },
    )
    assert [p.name for p in discover(tmp_path, KINDS["policy"])] == ["a.py", "b.py"]
    assert discover(tmp_path, KINDS["reward"]) == []  # no folder: nothing, no error


# --- one file ----------------------------------------------------------------------------------


def test_load_reads_name_decision_settings_and_docstring(tmp_path: Path) -> None:
    path = _write(tmp_path, {"policies/first.py": GOOD_POLICY}) / "policies" / "first.py"
    ext = load_file(path, KINDS["policy"])
    assert isinstance(ext, Extension)
    assert (ext.kind, ext.name, ext.decision, ext.source) == (
        "policy",
        "first_one",
        "make_next",
        "project",
    )
    assert ext.grid == {"days": (1, 2, 3)}
    assert ext.description.startswith("Run the first product, always.")
    assert ext.summary == "Run the first product, always."
    assert ext.line == 11 and ext.file == path.resolve()


def test_settings_are_passed_as_keywords(tmp_path: Path) -> None:
    text = "SETTINGS = {'step': [1, 2]}\n\n\ndef decide(state, step=1):\n    return step\n"
    path = _write(tmp_path, {"policies/p.py": text}) / "policies" / "p.py"
    ext = load_file(path, KINDS["policy"])
    assert isinstance(ext, Extension)
    assert ext(None) == 1 and ext(None, step=2) == 2


def test_value_settings_are_the_defaults_for_other_kinds(tmp_path: Path) -> None:
    text = "SETTINGS = {'penalty': 5.0}\n\n\ndef reward(move, penalty=1.0):\n    return -penalty\n"
    path = _write(tmp_path, {"rewards/r.py": text}) / "rewards" / "r.py"
    ext = load_file(path, KINDS["reward"])
    assert isinstance(ext, Extension)
    assert ext.settings == {"penalty": 5.0}
    assert ext(None) == -5.0 and ext(None, penalty=2.0) == -2.0


def test_name_defaults_to_the_file_stem(tmp_path: Path) -> None:
    path = _write(tmp_path, {"rewards/plain.py": ONE_OF_EACH["rewards/money.py"]})
    ext = load_file(path / "rewards" / "plain.py", KINDS["reward"])
    assert isinstance(ext, Extension) and ext.name == "plain"


def test_same_stem_in_two_folders_does_not_collide(tmp_path: Path) -> None:
    one = _write(tmp_path / "one", {"policies/rule.py": "def decide(state):\n    return 'a'\n"})
    two = _write(tmp_path / "two", {"policies/rule.py": "def decide(state):\n    return 'b'\n"})
    first = load_file(one / "policies" / "rule.py", KINDS["policy"])
    second = load_file(two / "policies" / "rule.py", KINDS["policy"])
    assert isinstance(first, Extension) and isinstance(second, Extension)
    assert first(None) == "a" and second(None) == "b"
    names = {module_name(one / "policies" / "rule.py", "policy"),
             module_name(two / "policies" / "rule.py", "policy")}  # fmt: skip
    assert len(names) == 2
    assert all(n.startswith("twinflow_user_policy_rule_") and len(n.rsplit("_", 1)[1]) == 8
               for n in names)  # fmt: skip
    assert not any(n in sys.modules for n in names)


@pytest.mark.parametrize(
    ("text", "code", "line"),
    [
        ("def decide(state)\n    return 'a'\n", "import_error", 1),
        ("x = 1\nraise RuntimeError('boom')\n", "import_error", 2),
        ("import no_such_module_tf006\n", "import_error", 1),
        ("def choose(state):\n    return 'a'\n", "no_function", None),
        ("decide = 3\n", "no_function", None),
        ("DECISION = 'sometimes'\n\n\ndef decide(state):\n    return 'a'\n", "bad_decision", 1),
        ("NAME = 7\n\n\ndef decide(state):\n    return 'a'\n", "bad_name", 1),
        ("SETTINGS = [1, 2]\n\n\ndef decide(state):\n    return 'a'\n", "bad_settings", 1),
        ("SETTINGS = {'k': []}\n\n\ndef decide(state, k=1):\n    return 'a'\n", "bad_settings", 1),
        ("SETTINGS = {'k': 3}\n\n\ndef decide(state, k=1):\n    return 'a'\n", "bad_settings", 1),
        ("SETTINGS = {'k': [1]}\n\n\ndef decide(state):\n    return 'a'\n", "bad_settings", 1),
    ],
)
def test_a_broken_file_is_a_problem_never_an_exception(
    tmp_path: Path, text: str, code: str, line: int | None
) -> None:
    path = _write(tmp_path, {"policies/broken.py": text}) / "policies" / "broken.py"
    problem = load_file(path, KINDS["policy"])
    assert isinstance(problem, Problem)
    assert (problem.kind, problem.code, problem.line) == ("policy", code, line)
    assert problem.file == path.resolve()
    assert problem.message and problem.fix
    assert "broken.py" in str(problem) and problem.fix in str(problem)


def test_settings_may_go_to_a_catch_all(tmp_path: Path) -> None:
    text = "SETTINGS = {'k': [1, 2]}\n\n\ndef decide(state, **settings):\n    return 'a'\n"
    path = _write(tmp_path, {"policies/p.py": text}) / "policies" / "p.py"
    assert isinstance(load_file(path, KINDS["policy"]), Extension)


def test_engine_kind_wants_a_class(tmp_path: Path) -> None:
    root = _write(tmp_path, {"engines/toy.py": ONE_OF_EACH["engines/toy.py"]})
    ext = load_file(root / "engines" / "toy.py", KINDS["engine"])
    assert isinstance(ext, Extension) and ext.name == "toy" and isinstance(ext.target, type)
    assert ext.line == 4
    bad = _write(tmp_path / "bad", {"engines/fn.py": "def Engine(model):\n    return None\n"})
    problem = load_file(bad / "engines" / "fn.py", KINDS["engine"])
    assert isinstance(problem, Problem) and problem.code == "no_function"
    assert "class" in problem.fix


# --- a whole project ---------------------------------------------------------------------------


def test_load_project_finds_every_kind(tmp_path: Path) -> None:
    project = _write(tmp_path / "project", ONE_OF_EACH)
    found = load_project(project, builtin=_empty_builtins(tmp_path))
    assert found.problems == ()
    assert {kind: found.names(kind) for kind in KINDS} == {
        "policy": ("keep",),
        "reward": ("money",),
        "view": ("plain",),
        "feature": ("risk",),
        "demand": ("flat",),
        "observation": ("same",),
        "engine": ("toy",),
    }
    assert all(ext.source == "project" for ext in found.all())


def test_project_file_overrides_a_builtin_by_name(tmp_path: Path) -> None:
    builtin = _write(
        tmp_path / "builtin",
        {
            "policies/idle.py": "def decide(state):\n    return 'idle'\n",
            "policies/other.py": "def decide(state):\n    return 'x'\n",
        },
    )
    project = _write(
        tmp_path / "project",
        {"policies/my_idle.py": "NAME = 'idle'\n\n\ndef decide(state):\n    return 'mine'\n"},
    )
    found = load_project(project, builtin=builtin)
    assert found.names("policy") == ("idle", "other")
    idle = found.get("policy", "idle")
    assert (idle.source, idle.overrides_builtin, idle(None)) == ("project", True, "mine")
    assert found.get("policy", "other").source == "builtin"
    rows = {row["name"]: row for row in found.listing()}
    assert rows["idle"]["source"] == "project" and rows["idle"]["overrides"] == "built-in idle"
    assert rows["other"]["overrides"] is None
    assert rows["idle"]["file"].endswith("my_idle.py")


def test_two_project_files_with_one_name_keep_the_first(tmp_path: Path) -> None:
    project = _write(
        tmp_path / "project",
        {
            "policies/a.py": "NAME = 'same'\n\n\ndef decide(state):\n    return 'a'\n",
            "policies/b.py": "NAME = 'same'\n\n\ndef decide(state):\n    return 'b'\n",
        },
    )
    found = load_project(project, builtin=_empty_builtins(tmp_path))
    assert found.get("policy", "same")(None) == "a"
    (problem,) = found.problems
    assert problem.code == "duplicate_name" and problem.file is not None
    assert problem.file.name == "b.py" and "a.py" in problem.message


def test_broken_files_are_listed_and_skipped(tmp_path: Path) -> None:
    project = _write(
        tmp_path / "project",
        {"policies/ok.py": ONE_OF_EACH["policies/keep.py"], "policies/bad.py": "def (\n"},
    )
    found = load_project(project, builtin=_empty_builtins(tmp_path))
    assert found.names("policy") == ("ok",)
    assert [p.code for p in found.problems] == ["import_error"]
    rows = found.listing()
    assert {r["name"]: r["problems"] for r in rows} == {"ok": [], "bad": ["import_error"]}


def test_unknown_names_say_what_exists(tmp_path: Path) -> None:
    project = _write(tmp_path / "project", ONE_OF_EACH)
    found = load_project(project, builtin=_empty_builtins(tmp_path))
    with pytest.raises(ExtensionNotFound, match="No reward named 'mony'.*money"):
        found.get("reward", "mony")


def test_a_missing_project_folder_is_a_problem(tmp_path: Path) -> None:
    found = load_project(tmp_path / "nowhere", builtin=_empty_builtins(tmp_path))
    assert [p.code for p in found.problems] == ["no_folder"]


def test_builtins_load_without_a_project() -> None:
    found = load_project(None)
    assert found.folder is None
    assert all(ext.source == "builtin" for ext in found.all())
