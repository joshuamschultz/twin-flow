"""T-272: the business showcases under `examples/showcase/` (TF-006).

Each showcase is a folder a real company could keep: input files exported from its ERP, a
twin capsule, rule files, and a notebook of `!twinflow` commands with saved outputs. This
test checks that:

- each notebook is what its source in `_sources/` makes (same cells, same order),
- every file a notebook command reads is in the folder,
- every `%%writefile` cell holds exactly the committed file,
- every `!twinflow` command runs with exit code 0 from a fresh copy of the folder, in order,
- the saved outputs are there, hold no machine paths and no errors, and the text has no
  em dashes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHOWCASE = ROOT / "examples" / "showcase"
BUSINESSES = ("plastics-molding", "cnc-job-shop", "pump-supply")
FILE_LIKE = re.compile(r"^[\w./-]+\.(csv|yaml|yml|json|py)$")


def _builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("showcase_build", SHOWCASE / "build_notebooks.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cells(business: str) -> list[dict[str, Any]]:
    book = json.loads((SHOWCASE / business / f"{business}.ipynb").read_text(encoding="utf-8"))
    cells: list[dict[str, Any]] = book["cells"]
    return cells


def _text(cell: dict[str, Any]) -> str:
    source = cell["source"]
    return "".join(source) if isinstance(source, list) else str(source)


def _commands(business: str) -> list[str]:
    """Every shell line (`!...`) of the notebook's code cells, in order."""
    return [
        line[1:].strip()
        for cell in _cells(business)
        if cell["cell_type"] == "code"
        for line in _text(cell).splitlines()
        if line.startswith("!")
    ]


def _outputs(cell: dict[str, Any]) -> str:
    parts = []
    for output in cell.get("outputs", []):
        text = output.get("text", "")
        parts.append("".join(text) if isinstance(text, list) else str(text))
        for value in output.get("data", {}).values():
            parts.append("".join(value) if isinstance(value, list) else str(value))
    return "".join(parts)


@pytest.mark.parametrize("business", BUSINESSES)
def test_notebook_is_what_its_source_makes(business: str) -> None:
    built = _builder().cells(business)
    saved = _cells(business)
    assert [(c["cell_type"], _text(c)) for c in saved] == [
        (c["cell_type"], _text(c)) for c in built
    ], (
        f"{business}.ipynb does not match _sources/{business}.txt. Run: uv run --with nbclient "
        "--with ipykernel --with nbformat python examples/showcase/build_notebooks.py"
    )


@pytest.mark.parametrize("business", BUSINESSES)
def test_every_file_a_command_reads_is_in_the_folder(business: str) -> None:
    folder = SHOWCASE / business
    for command in _commands(business):
        for word in shlex.split(command)[1:]:
            if FILE_LIKE.match(word) and not word.startswith("runs/"):
                assert (folder / word).is_file(), f"{business}: `{command}` reads {word}"
    assert (folder / "README.md").is_file()


@pytest.mark.parametrize("business", BUSINESSES)
def test_every_writefile_cell_equals_the_committed_file(business: str) -> None:
    found = 0
    for cell in _cells(business):
        text = _text(cell)
        if cell["cell_type"] == "code" and text.startswith("%%writefile "):
            first, _, body = text.partition("\n")
            path = SHOWCASE / business / first.split(" ", 1)[1].strip()
            assert body + "\n" == path.read_text(encoding="utf-8"), f"{path} differs"
            found += 1
    assert found >= 1


@pytest.mark.parametrize("business", BUSINESSES)
def test_saved_outputs_are_plain_and_complete(business: str) -> None:
    for cell in _cells(business):
        text = _text(cell)
        if cell["cell_type"] == "markdown":
            assert "—" not in text, "no em dashes"
            continue
        out = _outputs(cell)
        for machine_path in ("/Users/", "/home/", "/private/", "/var/folders/", "/tmp/"):  # noqa: S108
            assert machine_path not in out, f"{business}: a machine path in the outputs"
        assert "Traceback" not in out and "error:" not in out.lower()
        command = text.lstrip("!").split("\n")[0]
        if not command.startswith(
            ("%%writefile", "twinflow scenario validate", "twinflow validate")
        ):
            assert out.strip(), f"{business}: no saved output for `{command}`"


def _twinflow() -> str:
    beside = Path(sys.executable).parent / "twinflow"
    found = str(beside) if beside.exists() else shutil.which("twinflow")
    assert found, "twinflow is not installed in this environment"
    return found


@pytest.mark.parametrize("business", BUSINESSES)
def test_every_twinflow_command_runs_from_the_folder(business: str, tmp_path: Path) -> None:
    folder = tmp_path / business
    shutil.copytree(SHOWCASE / business, folder, ignore=shutil.ignore_patterns("runs", "*.ipynb"))
    env = {
        **os.environ,
        "PATH": os.pathsep.join([str(Path(_twinflow()).parent), os.environ["PATH"]]),
    }
    commands = [c for c in _commands(business) if c.startswith("twinflow ")]
    assert commands
    for command in commands:
        done = subprocess.run(  # noqa: S603  (the notebook's own commands, no shell)
            shlex.split(command),
            cwd=folder,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert done.returncode == 0, (
            f"{business}: `{command}` failed:\n{done.stdout}\n{done.stderr}"
        )
