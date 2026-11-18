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


def test_improve_says_a_missing_candidate_was_not_found(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.py"
    code, _, err = _run(capsys, "improve", str(project), "--candidate", str(missing))
    assert code == 1
    assert "not found" in err and str(missing) in err and "not a policy file" not in err


def test_improve_still_rejects_a_candidate_that_is_not_a_py_file(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = tmp_path / "notes.txt"
    text.write_text("hi", encoding="utf-8")
    code, _, err = _run(capsys, "improve", str(project), "--candidate", str(text))
    assert code == 1 and "not a policy file" in err


def test_validate_points_a_capsule_to_scenario_validate(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "validate", str(project / CAPSULE))
    assert code == 1
    assert "part_types" not in out + err
    assert "model.yaml" in err and "twinflow scenario validate" in err


def test_report_prints_the_path_it_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    run = tmp_path / "runs" / "abc"
    run.mkdir(parents=True)
    (run / "kpis.json").write_text("{}", encoding="utf-8")
    (run / "report.html").write_text("<html></html>", encoding="utf-8")
    code, out, _ = _run(capsys, "report", "abc", "--out", "html")
    assert code == 0 and str(Path("runs") / "abc" / "report.html") in out


def test_race_with_a_misspelled_score_is_a_user_error_that_lists_the_names(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit(project, "  seed: 2024\n", "  seed: 2024\n  score: dolars\n")
    code, _, err = _run(capsys, "policies", "race", str(project), "--episodes", "2")
    assert code == 1
    assert "experiment.score" in err and "dollars" in err and "internal error" not in err


@pytest.fixture
def bakery(tmp_path: Path) -> Path:
    folder = tmp_path / "bakery"
    assert main(["new", str(folder), "--template", "custom-engine"]) == 0
    return folder


def test_custom_engine_race_leaves_built_in_rules_out(
    bakery: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "policies", "race", str(bakery), "--episodes", "2")
    assert code == 0
    assert "bake_the_lowest" in out
    assert "abc_wheel" not in out and "Skipped policies" not in out
    assert "make-to-stock" in out


def test_policies_list_counts_the_same_problems_as_check(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "policies" / "bad_action.py").write_text(
        "NAME = 'wild'\n\n\ndef decide(state):\n    return 'no-such-product'\n", encoding="utf-8"
    )
    code, check_out, _ = _run(capsys, "policies", "check", str(project))
    assert code == 1
    _, out, _ = _run(capsys, "policies", "list", str(project))
    row = next(line for line in out.splitlines() if line.startswith("wild"))
    assert row.split()[-1] == "1"


def test_job_shop_template_records_a_useful_number_of_moves_per_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shop = tmp_path / "shop"
    assert main(["new", str(shop), "--template", "job-shop"]) == 0
    out = tmp_path / "out"
    assert main(["scenario", "run", str(shop), "--reps", "3", "--out", str(out)]) == 0
    capsys.readouterr()
    code, text, _ = _run(capsys, "states", str(out))
    assert code == 0
    moves = int(next(line for line in text.splitlines() if line.startswith("Moves:")).split()[1])
    assert moves >= 3 * 5
    assert "Lower experiment.state_capture.every_hours" not in text


def test_states_hints_when_a_run_has_very_few_moves(
    des_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "states", str(des_run))
    assert code == 0
    assert "Only 2 moves. Lower experiment.state_capture.every_hours to record more." in out
