"""Build and run the business showcase notebooks (TF-006 T-272).

Each showcase notebook is written as plain text in `_sources/<business>.txt`. A line
`--- markdown` starts a text cell, `--- code` starts a command cell, and
`--- writefile <path>` starts a `%%writefile <path>` cell whose body is the committed file
at that path. So a rule shown in a notebook is always the same as the file in the folder.

Build the notebooks and run them top to bottom, saving every output:

    uv run --with nbclient --with ipykernel --with nbformat \
        python examples/showcase/build_notebooks.py

Each notebook runs in a fresh copy of its folder, so the folder you commit is not changed
(the race and improve commands write `runs/` and copy files). Absolute paths of the copy
are replaced with `.` in the saved outputs. `--no-run` builds the notebooks without outputs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES = HERE / "_sources"
BUSINESSES = ("plastics-molding", "cnc-job-shop", "pump-supply")
MARK = "--- "


def cells(business: str) -> list[dict[str, object]]:
    """The notebook cells a source file makes."""
    text = (SOURCES / f"{business}.txt").read_text(encoding="utf-8")
    found: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith(MARK):
            found.append((line[len(MARK) :].strip(), []))
        elif found:
            found[-1][1].append(line)
    out: list[dict[str, object]] = []
    for kind, lines in found:
        body = "\n".join(lines).strip("\n")
        if kind.startswith("writefile "):
            path = kind.split(" ", 1)[1].strip()
            content = (HERE / business / path).read_text(encoding="utf-8")
            out.append(_cell("code", f"%%writefile {path}\n{content.rstrip(chr(10))}"))
        elif kind in ("markdown", "code"):
            out.append(_cell(kind, body))
        else:
            raise ValueError(f"{business}.txt: unknown cell marker {kind!r}")
    for number, cell in enumerate(out):
        cell["id"] = f"{business}-{number:02d}"  # stable ids keep the diff small
    return out


def _cell(kind: str, text: str) -> dict[str, object]:
    parts = text.split("\n")
    cell: dict[str, object] = {
        "cell_type": kind,
        "metadata": {},
        "source": [p + "\n" for p in parts[:-1]] + [parts[-1]],
    }
    if kind == "code":
        cell.update(execution_count=None, outputs=[])
    return cell


def notebook(business: str) -> dict[str, object]:
    return {
        "cells": cells(business),
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def target(business: str) -> Path:
    return HERE / business / f"{business}.ipynb"


def _scrub(value: object, folder: Path) -> object:
    """Replace the run copy's absolute path with `.` everywhere in the outputs."""
    names = {str(folder), str(folder.resolve()), os.path.realpath(folder)}
    if isinstance(value, str):
        for name in sorted(names, key=len, reverse=True):
            value = value.replace(name, ".")
        return value
    if isinstance(value, list):
        return [_scrub(item, folder) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item, folder) for key, item in value.items()}
    return value


def run(business: str) -> dict[str, object]:
    """Run the notebook in a fresh copy of its folder and return it with outputs."""
    import nbformat
    from nbclient import NotebookClient

    book = nbformat.reads(json.dumps(notebook(business)), as_version=4)
    twinflow = shutil.which("twinflow")
    if twinflow is None:
        raise SystemExit("twinflow is not on PATH. Run this script with uv run.")
    env_path = os.pathsep.join([str(Path(twinflow).parent), os.environ.get("PATH", "")])
    with tempfile.TemporaryDirectory(prefix=f"showcase-{business}-") as tmp:
        folder = Path(tmp) / business
        shutil.copytree(
            HERE / business, folder, ignore=shutil.ignore_patterns("runs", "*.ipynb", ".*")
        )
        os.environ["PATH"] = env_path
        client = NotebookClient(
            book, timeout=600, kernel_name="python3", resources={"metadata": {"path": str(folder)}}
        )
        client.execute()
        data = json.loads(nbformat.writes(book))
        for cell in data["cells"]:
            cell.get("metadata", {}).pop("execution", None)
            if "outputs" in cell:
                cell["outputs"] = _scrub(cell["outputs"], folder)
    return data


def write(business: str, data: dict[str, object]) -> Path:
    path = target(business)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("businesses", nargs="*", default=list(BUSINESSES))
    parser.add_argument("--no-run", action="store_true", help="build without running")
    args = parser.parse_args(argv)
    for business in args.businesses:
        data = notebook(business) if args.no_run else run(business)
        print(f"wrote {write(business, data).relative_to(HERE.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
