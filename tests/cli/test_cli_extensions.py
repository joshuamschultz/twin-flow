"""T-262: `twinflow extensions list|check <project>` (COMP-159, REQ-222, REQ-213, D-145)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from twinflow.cli.main import main

PRESS = Path(__file__).resolve().parents[2] / "examples" / "molding-press"

GOOD_POLICY = "NAME = 'keep'\n\n\ndef decide(state):\n    return 'idle'\n"
OVERRIDE = "NAME = 'lowest_cover'\n\n\ndef decide(state):\n    return 'idle'\n"
BROKEN = "def decide(state)\n    return 'idle'\n"
BAD_ACTION = "NAME = 'wild'\n\n\ndef decide(state):\n    return 'no-such-product'\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    shutil.copytree(PRESS, folder)
    return folder


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _add(project: Path, name: str, text: str) -> None:
    (project / "policies").mkdir(exist_ok=True)
    (project / "policies" / name).write_text(text)


def test_list_prints_a_table_with_built_ins_and_project_files(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(project, "keep.py", GOOD_POLICY)
    code, out, _ = _run(capsys, "extensions", "list", str(project))
    assert code == 0
    header = out.splitlines()[0]
    for column in ("kind", "name", "source", "overrides", "problems"):
        assert column in header
    assert "policy" in out and "builtin" in out
    keep = next(line for line in out.splitlines() if " keep " in line)
    assert "project" in keep


def test_list_shows_an_override(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _add(project, "lowest.py", OVERRIDE)
    _, out, _ = _run(capsys, "extensions", "list", str(project))
    line = next(ln for ln in out.splitlines() if "lowest_cover" in ln and "project" in ln)
    assert "built-in lowest_cover" in line


def test_list_json_is_machine_readable(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _add(project, "keep.py", GOOD_POLICY)
    code, out, _ = _run(capsys, "extensions", "list", str(project), "--json")
    assert code == 0
    rows = json.loads(out)
    assert {"kind", "name", "source", "overrides", "problems"} <= set(rows[0])
    assert any(r["name"] == "keep" and r["source"] == "project" for r in rows)


def test_list_counts_problems_of_a_broken_file(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(project, "broken.py", BROKEN)
    code, out, _ = _run(capsys, "extensions", "list", str(project))
    assert code == 0
    assert any("broken" in ln and ln.rstrip().endswith("1") for ln in out.splitlines())


def test_check_good_project_exits_zero(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _add(project, "keep.py", GOOD_POLICY)
    code, out, _ = _run(capsys, "extensions", "check", str(project))
    assert code == 0
    assert "No problems" in out


def test_check_broken_file_exits_one_and_prints_the_problem(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(project, "broken.py", BROKEN)
    code, out, _ = _run(capsys, "extensions", "check", str(project))
    assert code == 1
    assert "policies/broken.py" in out and "Fix:" in out


def test_check_bad_action_exits_one(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _add(project, "wild.py", BAD_ACTION)
    code, out, _ = _run(capsys, "extensions", "check", str(project))
    assert code == 1
    assert "wild.py" in out


def test_missing_project_names_the_path_and_a_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gone = tmp_path / "gone"
    for verb in ("list", "check"):
        code, _, err = _run(capsys, "extensions", verb, str(gone))
        assert code == 1
        assert str(gone) in err and "Fix:" in err


def test_folder_without_a_capsule_says_what_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "extensions", "list", str(tmp_path))
    assert code == 1
    assert str(tmp_path) in err and "Fix:" in err


def test_unknown_verb_is_a_parse_failure_not_a_crash(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["extensions", "frobnicate", str(project)]) == 2
