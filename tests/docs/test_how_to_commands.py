"""Every `twinflow` command in a how-to guide runs as written (TF-006 T-254).

Each guide runs in its own fresh folder, with a copy of `examples/` beside it, so the
commands that name `examples/...` and the commands that name a plant the reader made
(`my-plant`, `my-shop`) both work. Commands run in the order the guide gives them.

- A run ID in a command is replaced with the ID the last `run` printed. Run IDs are not
  stable, and the guides tell the reader to use the ID their own run printed.
- A guide can name a prerequisite. The prerequisite is what an earlier guide or step makes,
  so the guide can be read on its own. Prerequisites are listed in `PREREQUISITES`.
- A command that needs an input the test cannot make goes in `SKIPS`, with the reason.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

# The guides run about 50 s in total, so the file is marked slow (see pyproject).
pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]
HOW_TO = ROOT / "docs" / "how-to"
EXAMPLES = ROOT / "examples"
RUN_ID = re.compile(r"run-\d+-\d+-[0-9a-f]+")
CALL = "import sys; from twinflow.cli.main import main; sys.exit(main(sys.argv[1:]))"

# Steps a reader does in an earlier guide. Each is a twinflow argument list, or a function
# that takes the guide's folder and makes a file.
Step = list[str] | Callable[[Path], None]
MAKE_PLANT = ["new", "my-plant", "--template", "make-to-stock"]
PRESS_RUN = [
    "scenario",
    "run",
    "examples/molding-press/molding-press.twin.yaml",
    "--reps",
    "50",
    "--seed",
    "2024",
    "--out",
    "press-out",
]


def _make_candidate(folder: Path) -> None:
    """The guide says: copy a rule from policies/, change its numbers, give it a new NAME."""
    source = next(iter(sorted((folder / "my-plant" / "policies").glob("*.py"))))
    text = source.read_text(encoding="utf-8")
    text = re.sub(r'^NAME = ".*"$', 'NAME = "my_rule"', text, count=1, flags=re.M)
    (folder / "my_rule.py").write_text(text, encoding="utf-8")


PREREQUISITES: dict[str, list[Step]] = {
    "check-your-twin.md": [MAKE_PLANT],
    "race-your-policies.md": [MAKE_PLANT],
    "write-a-policy.md": [MAKE_PLANT],
    "improve-a-policy.md": [MAKE_PLANT, _make_candidate],
    "read-the-markov-map.md": [PRESS_RUN],
}

# (guide name, command as written) -> reason. Empty: every command runs.
SKIPS: dict[tuple[str, str], str] = {}


def _commands(doc: Path) -> list[tuple[int, list[str]]]:
    """(line number, argument list) for each twinflow command inside a fenced block."""
    found: list[tuple[int, list[str]]] = []
    fenced = False
    for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced and stripped.startswith("twinflow "):
            found.append((number, shlex.split(stripped)[1:]))
    return found


def _twinflow(folder: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    # The arguments come from the guides in this repo, not from outside input.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", CALL, *args],
        cwd=folder,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _describe(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit {result.returncode}\n{result.stdout[-800:]}{result.stderr[-800:]}"


def test_every_guide_has_commands() -> None:
    guides = sorted(HOW_TO.glob("*.md"))
    assert len(guides) >= 10
    assert sum(len(_commands(guide)) for guide in guides) >= 20


@pytest.mark.parametrize("doc", sorted(HOW_TO.glob("*.md")), ids=lambda p: p.name)
def test_how_to_commands_run_as_written(doc: Path, tmp_path: Path) -> None:
    folder = tmp_path
    shutil.copytree(EXAMPLES, folder / "examples")
    for step in PREREQUISITES.get(doc.name, []):
        if callable(step):
            step(folder)
        else:
            prep = _twinflow(folder, step)
            assert prep.returncode == 0, f"prerequisite {step}: {_describe(prep)}"

    last_run_id: str | None = None
    failures: list[str] = []
    for number, args in _commands(doc):
        text = shlex.join(["twinflow", *args])
        if (doc.name, text) in SKIPS:
            continue
        if last_run_id:
            args = [RUN_ID.sub(last_run_id, arg) for arg in args]
        result = _twinflow(folder, args)
        if result.returncode != 0:
            failures.append(
                f"{doc.name}:{number} `{shlex.join(['twinflow', *args])}`\n{_describe(result)}"
            )
        if args[:1] == ["run"]:
            printed = RUN_ID.search(result.stdout)
            last_run_id = printed.group(0) if printed else last_run_id
    assert not failures, "\n\n".join(failures)
