"""Scaffold guardrails (T-001): the trust-boundary and no-eval rules enforced by grep.

These are cheap structural invariants that must hold from the first commit and forever
after (D-001, D-002). They are not tied to any one component's behaviour.
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "twinflow"

# The builtin eval()/exec() only — NOT a `.eval(` method call. simpleeval's sandboxed
# `.eval()` (the mandated evaluator API, D-006) is a method call and legitimate; the
# forbidden thing (D-002) is Python's builtin eval/exec, which never has a `.` or an
# identifier character immediately before it.
_BUILTIN_EVAL_EXEC = re.compile(r"(?<![\w.])(?:eval|exec)\s*\(")


def _py_files() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def test_pyyaml_has_exactly_one_importer() -> None:
    importers = [p for p in _py_files() if "import yaml" in p.read_text()]
    assert [p.name for p in importers] == ["loader.py"], importers


def test_simpleeval_has_exactly_one_importer() -> None:
    importers = [p for p in _py_files() if "import simpleeval" in p.read_text()]
    assert [p.name for p in importers] == ["expressions.py"], importers


def test_no_eval_or_exec_in_src() -> None:
    offenders = []
    for p in _py_files():
        text = p.read_text()
        if _BUILTIN_EVAL_EXEC.search(text):
            offenders.append((p.name, "builtin eval/exec"))
        if "yaml.load(" in text:
            offenders.append((p.name, "yaml.load("))
    assert offenders == [], offenders


def test_package_imports() -> None:
    import twinflow

    assert twinflow.__version__
