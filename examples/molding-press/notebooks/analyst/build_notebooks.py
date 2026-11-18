"""Build the sample notebooks from their Python sources (T-268, REQ-227).

Each notebook lives as one readable `.py` file in `src/`. A line `# %% [markdown]` starts a
text cell (its lines start with `# `). A line `# %%` starts a code cell. This script turns
every source into an `.ipynb` file with no outputs, so the notebooks stay easy to diff.

    uv run python examples/molding-press/notebooks/analyst/build_notebooks.py

The test `tests/integration/test_molding_press_notebooks.py` fails when an `.ipynb` file
is not the one its source makes.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES = HERE / "src"
MARKDOWN = "# %% [markdown]"
CODE = "# %%"


def split_cells(source: str) -> list[tuple[str, str]]:
    """The (kind, text) of each cell of a source file; text before the first marker is dropped."""
    cells: list[tuple[str, list[str]]] = []
    for line in source.splitlines():
        if line.rstrip() == MARKDOWN:
            cells.append(("markdown", []))
        elif line.rstrip() == CODE:
            cells.append(("code", []))
        elif cells:
            cells[-1][1].append(line)
    out = []
    for kind, lines in cells:
        if kind == "markdown":
            lines = [line[2:] if line.startswith("# ") else line.removeprefix("#") for line in lines]
        text = "\n".join(lines).strip("\n")
        if text.strip():
            out.append((kind, text))
    return out


def _lines(text: str) -> list[str]:
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + [parts[-1]]


def notebook(source: str) -> dict[str, object]:
    """The notebook (nbformat 4.4, no outputs) a source file makes."""
    cells: list[dict[str, object]] = []
    for kind, text in split_cells(source):
        cell: dict[str, object] = {"cell_type": kind, "metadata": {}, "source": _lines(text)}
        if kind == "code":
            cell.update(execution_count=None, outputs=[])
        cells.append(cell)
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def notebook_text(source: str) -> str:
    return json.dumps(notebook(source), indent=1, ensure_ascii=False) + "\n"


def sources() -> list[Path]:
    return sorted(SOURCES.glob("*.py"))


def target(source: Path) -> Path:
    return HERE / f"{source.stem}.ipynb"


def build() -> list[Path]:
    written = []
    for source in sources():
        path = target(source)
        path.write_text(notebook_text(source.read_text(encoding="utf-8")), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for written_path in build():
        print(f"wrote {written_path.relative_to(HERE.parent.parent.parent)}")
