"""T-268: the two sample notebooks of the molding-press example (REQ-227).

`examples/molding-press/notebooks/analyst/src/*.py` are the sources; `build_notebooks.py` turns each
into an `.ipynb` file with no outputs. This test checks that:

- every `.ipynb` file is exactly what its source makes (they cannot drift apart),
- no notebook is saved with outputs, and the text has no em dashes,
- every code cell runs top to bottom in fast mode (`TWINFLOW_FAST=1`) in a fresh namespace.

Jupyter is not a dependency, so the cells run through `exec`, the way a notebook kernel runs
them: the last expression of a cell is evaluated and its table or figure is rendered.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / "examples" / "molding-press" / "notebooks" / "analyst"
NAMES = ("01_long_term_policy", "02_state_information")


def _builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_notebooks", FOLDER / "build_notebooks.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cells(name: str) -> list[dict[str, Any]]:
    book = json.loads((FOLDER / f"{name}.ipynb").read_text(encoding="utf-8"))
    cells: list[dict[str, Any]] = book["cells"]
    return cells


def _text(cell: dict[str, Any]) -> str:
    return "".join(cell["source"])


def _render(value: object) -> None:
    """What a notebook does with the value of a cell: draw the figure or the table."""
    if hasattr(value, "to_plotly_json"):
        json.dumps(value.to_plotly_json(), default=str)  # type: ignore[attr-defined]
    elif hasattr(value, "_repr_html_"):
        value._repr_html_()


def run_cells(name: str) -> dict[str, Any]:
    """Run every code cell of a notebook in one fresh namespace, like a kernel."""
    namespace: dict[str, Any] = {"__name__": "__main__"}
    for number, cell in enumerate(_cells(name)):
        if cell["cell_type"] != "code":
            continue
        tree = ast.parse(_text(cell), filename=f"{name} cell {number}")
        last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        exec(compile(tree, f"{name} cell {number}", "exec"), namespace)  # noqa: S102
        if last is not None:
            expression = ast.Expression(last.value)  # type: ignore[attr-defined]
            _render(eval(compile(expression, f"{name} cell {number}", "eval"), namespace))  # noqa: S307
    return namespace


@pytest.mark.parametrize("name", NAMES)
def test_notebook_matches_its_source(name: str) -> None:
    builder = _builder()
    source = (FOLDER / "src" / f"{name}.py").read_text(encoding="utf-8")
    saved = (FOLDER / f"{name}.ipynb").read_text(encoding="utf-8")
    assert saved == builder.notebook_text(source), (
        f"{name}.ipynb is not what src/{name}.py makes. "
        "Run: uv run python examples/molding-press/notebooks/analyst/build_notebooks.py"
    )


def test_every_source_has_a_notebook() -> None:
    builder = _builder()
    assert [p.stem for p in builder.sources()] == list(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_notebook_is_saved_without_outputs(name: str) -> None:
    for cell in _cells(name):
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None


@pytest.mark.parametrize("name", NAMES)
def test_text_is_plain(name: str) -> None:
    for cell in _cells(name):
        assert "—" not in _text(cell), "no em dashes"


@pytest.mark.parametrize("name", NAMES)
def test_notebook_has_every_step(name: str) -> None:
    headings = [
        line
        for cell in _cells(name)
        if cell["cell_type"] == "markdown"
        for line in _text(cell).splitlines()
    ]
    for step in range(1, 8):
        assert any(line.startswith(f"## Step {step}") for line in headings), f"Step {step}"
    assert any(line.startswith("# Conclusion") for line in headings)


@pytest.mark.parametrize("name", NAMES)
def test_notebook_runs_top_to_bottom_in_fast_mode(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWINFLOW_FAST", "1")
    monkeypatch.chdir(FOLDER)
    namespace = run_cells(name)
    assert namespace["FAST"] is True
