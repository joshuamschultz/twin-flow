"""T-251: `twinflow states <run_dir>` (COMP-159, REQ-210, REQ-213, D-145)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from twinflow.cli.main import main


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_states_on_a_step_parent_folder_summarises_every_replication(
    step_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "states", str(step_run))
    assert code == 0
    assert "Runs found: 2" in out
    assert "Episodes: 2" in out
    assert "Moves: 120" in out
    assert "Time span: 0 to 60" in out
    assert "Unique states:" in out
    assert "Top states (view family)" in out
    assert "Ledger by part" in out
    assert "revenue" in out and "lost_sales" in out


def test_states_on_a_single_replication_folder(
    step_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "states", str(step_run / "rep-0000"))
    assert code == 0
    assert "Runs found: 1" in out
    assert "Moves: 60" in out


def test_states_top_and_view_are_honored(
    step_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "states", str(step_run), "--view", "full", "--top", "3")
    assert code == 0
    assert "Top states (view full)" in out
    rows = [line for line in out.splitlines() if line.startswith("  ") and "%" in line]
    assert len(rows) == 3


def test_states_unknown_view_names_the_choices(
    step_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "states", str(step_run), "--view", "nope")
    assert code == 1
    assert "nope" in err and "full" in err and "family" in err


def test_states_on_a_des_run_falls_back_to_full_with_a_note(
    des_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "states", str(des_run))
    assert code == 0
    assert "Runs found: 2" in out
    assert "no product facts" in out
    assert "Top states (view full)" in out


def test_states_on_a_run_with_no_moves_says_how_to_turn_capture_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "states", str(tmp_path))
    assert code == 1
    assert f"No moves found in {tmp_path}" in err
    assert "experiment.state_capture" in err


def test_states_on_a_missing_folder_names_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gone = tmp_path / "gone"
    code, _, err = _run(capsys, "states", str(gone))
    assert code == 1
    assert str(gone) in err and "does not exist" in err


def test_states_without_facts_file_still_works_on_a_copied_rep(
    step_run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    copied = tmp_path / "copy"
    shutil.copytree(step_run / "rep-0000", copied)
    code, out, _ = _run(capsys, "states", str(copied))
    assert code == 0
    assert "no product facts" in out
