"""T-251: `twinflow markov <run_dir>` (COMP-159, REQ-210, REQ-213, D-145)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from twinflow.cli.main import main


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_markov_prints_and_writes_both_files(
    step_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "markov", str(step_run))
    assert code == 0
    assert "View: family" in out
    assert "Moves: 120" in out
    assert "Coverage:" in out
    chain = json.loads((step_run / "markov.json").read_text())
    assert chain["view"] == "family" and chain["moves"] == 120
    assert (step_run / "markov.mmd").read_text().startswith("flowchart LR")


def test_markov_top_limits_the_states(step_run: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = _run(capsys, "markov", str(step_run), "--view", "full", "--top", "4")
    assert code == 0
    chain = json.loads((step_run / "markov.json").read_text())
    assert chain["view"] == "full"
    assert len(chain["states"]) == 4


def test_markov_min_prob_hides_small_arrows(
    step_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(capsys, "markov", str(step_run), "--min-prob", "0.05")
    loose = len(json.loads((step_run / "markov.json").read_text())["edges"])
    _run(capsys, "markov", str(step_run), "--min-prob", "0.9")
    strict = json.loads((step_run / "markov.json").read_text())["edges"]
    assert len(strict) < loose
    assert all(edge["prob"] >= 0.9 for edge in strict)


def test_markov_out_folder(
    step_run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "maps"
    code, _, _ = _run(capsys, "markov", str(step_run), "--out", str(out))
    assert code == 0
    assert (out / "markov.json").is_file() and (out / "markov.mmd").is_file()


def test_markov_uses_a_capsule_view(step_run: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _run(capsys, "markov", str(step_run), "--view", "simple")
    assert code == 0
    assert "View: simple" in out


def test_markov_on_a_des_run_full_view_works(
    des_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "markov", str(des_run), "--view", "full")
    assert code == 0
    assert "no product facts" not in out
    assert json.loads((des_run / "markov.json").read_text())["view"] == "full"


def test_markov_on_a_des_run_family_view_falls_back_with_a_note(
    des_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "markov", str(des_run), "--view", "family")
    assert code == 0
    assert "no product facts" in out
    assert json.loads((des_run / "markov.json").read_text())["view"] == "full"


def test_markov_bad_numbers_are_plain_errors(
    step_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "markov", str(step_run), "--top", "0")
    assert code == 1 and "--top" in err
    code, _, err = _run(capsys, "markov", str(step_run), "--min-prob", "2")
    assert code == 1 and "--min-prob" in err


def test_markov_with_no_moves_says_how_to_turn_capture_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "markov", str(tmp_path))
    assert code == 1
    assert f"No moves found in {tmp_path}" in err and "experiment.state_capture" in err
