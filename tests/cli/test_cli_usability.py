"""TF-006 CLI usability fixes: every user error names the file/field, the problem and a fix
(REQ-213). One test per reported bug."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from twinflow.cli.main import main

ROOT = Path(__file__).resolve().parents[2]
PRESS = ROOT / "examples" / "molding-press"
CAPSULE = "molding-press.twin.yaml"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    shutil.copytree(PRESS, folder)
    return folder


def _edit(project: Path, old: str, new: str) -> Path:
    file = project / CAPSULE
    text = file.read_text(encoding="utf-8")
    assert old in text
    file.write_text(text.replace(old, new), encoding="utf-8")
    return file


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_scenario_validate_rejects_a_misspelled_policy(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = _edit(project, "policy: lowest_cover", "policy: lowest_covr")
    code, out, _ = _run(capsys, "scenario", "validate", str(file))
    assert code == 1
    assert "experiment.policy" in out and "lowest_cover" in out


def test_scenario_run_stops_on_a_misspelled_policy(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = _edit(project, "policy: lowest_cover", "policy: lowest_covr")
    code, _, err = _run(capsys, "scenario", "run", str(file), "--out", str(tmp_path / "out"))
    assert code == 1
    assert "experiment.policy" in err and "lowest_cover" in err


def test_scenario_validate_catches_experiment_key_typos(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = _edit(project, "  seed: 2024\n", "  seed: 2024\n  scoer: dollars\n")
    code, out, _ = _run(capsys, "scenario", "validate", str(file))
    assert code == 1
    assert "experiment.scoer" in out and "score" in out


def test_scenario_validate_accepts_a_project_folder(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "scenario", "validate", str(project))
    assert code == 0 and "Errno" not in out + err


def test_scenario_validate_explains_a_folder_without_a_capsule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "scenario", "validate", str(tmp_path))
    assert code == 1 and "Errno" not in err and ".twin.yaml" in err and "Fix:" in err
