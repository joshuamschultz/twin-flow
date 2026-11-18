"""T-250: `twinflow policies list|check|race <project>` (COMP-159, REQ-209, REQ-213, D-145)."""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from twinflow.cli import policies as policies_cli
from twinflow.cli.main import main
from twinflow.extensions.race import RaceResult

ROOT = Path(__file__).resolve().parents[2]
PRESS = ROOT / "examples" / "molding-press"
SHOP = ROOT / "examples" / "cnc-shop"

BROKEN = "def decide(state)\n    return 'idle'\n"
BAD_ACTION = "NAME = 'wild'\n\n\ndef decide(state):\n    return 'no-such-product'\n"
KEEP = "NAME = 'keep'\n\n\ndef decide(state):\n    return 'idle'\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    shutil.copytree(PRESS, folder)
    return folder


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_policies_list_shows_name_source_and_settings(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "policies", "list", str(project))
    assert code == 0
    header = out.splitlines()[0]
    for column in ("name", "source", "decision", "settings", "problems"):
        assert column in header
    row = next(line for line in out.splitlines() if "lowest_stock_first" in line)
    assert "project" in row and "stop_days" in row
    assert any("kanban" in line and "builtin" in line for line in out.splitlines())


def test_policies_check_passes_on_the_press(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "policies", "check", str(project))
    assert code == 0 and "No problems found" in out


def test_policies_check_names_a_broken_file(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "policies" / "oops.py").write_text(BROKEN)
    code, out, _ = _run(capsys, "policies", "check", str(project))
    assert code == 1
    assert "policies/oops.py" in out and "Fix:" in out


def test_policies_check_leaves_out_other_kinds(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "views").mkdir()
    (project / "views" / "bad.py").write_text(BROKEN)
    code, out, _ = _run(capsys, "policies", "check", str(project))
    assert code == 0 and "bad.py" not in out


@pytest.mark.parametrize("sub", ["list", "check", "race"])
def test_a_missing_folder_is_exit_1_with_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], sub: str
) -> None:
    missing = tmp_path / "nope"
    code, _, err = _run(capsys, "policies", sub, str(missing))
    assert code == 1 and str(missing) in err and "Fix:" in err


@pytest.mark.parametrize("sub", ["list", "check", "race"])
def test_a_non_make_to_stock_capsule_is_exit_1_in_plain_words(
    capsys: pytest.CaptureFixture[str], sub: str
) -> None:
    code, _, err = _run(capsys, "policies", sub, str(SHOP))
    assert code == 1 and "make_to_stock" in err and "Fix:" in err


def test_race_prints_a_ranking_progress_and_the_report_path(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "policies", "race", str(project), "--episodes", "5")
    assert code == 0
    lines = out.splitlines()
    header = next(line for line in lines if line.startswith("Rank"))
    for column in ("Policy", "Score", "Dollars per run", "Fill rate", "Changeovers"):
        assert column in header
    assert any(line.strip().startswith("lowest_stock_first (full):") for line in lines)  # progress
    assert "95%" in out
    runs = sorted((project / "runs").glob("race-*"))
    assert len(runs) == 1
    assert (runs[0] / "race.json").is_file() and (runs[0] / "race.html").is_file()
    assert str(runs[0] / "race.html") in out
    ranks = [line.split()[0] for line in lines[lines.index(header) + 1 :] if line[:1].isdigit()]
    assert ranks[:3] == ["1", "2", "3"]


def test_race_honors_out_episodes_seed_and_policies_folder(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "keep.py").write_text(KEEP)
    out_dir = tmp_path / "out"
    code, out, _ = _run(
        capsys, "policies", "race", str(project), "--policies", str(folder),
        "--episodes", "4", "--seed", "9", "--out", str(out_dir),
    )  # fmt: skip
    assert code == 0
    data = json.loads((out_dir / "race.json").read_text())
    assert data["settings"]["episodes"] == 4 and data["settings"]["seed"] == 9
    names = {r["policy"] for r in data["results"]}
    assert "keep" in names and "lowest_stock_first" not in names  # the folder replaces policies/
    assert not (project / "runs").exists()


def test_race_with_one_observation(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _run(
        capsys, "policies", "race", str(project), "--episodes", "3", "--observation", "bucketed"
    )
    assert code == 0 and "(bucketed)" in out


def test_race_unknown_observation_is_exit_1_with_a_hint(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(
        capsys, "policies", "race", str(project), "--episodes", "3", "--observation", "bucketd"
    )
    assert code == 1 and "bucketd" in err and "bucketed" in err


@pytest.mark.parametrize("flag,value", [("--episodes", "0"), ("--episodes", "x"), ("--seed", "y")])
def test_race_bad_numbers_are_exit_1_naming_the_flag(
    project: Path, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    code, _, err = _run(capsys, "policies", "race", str(project), flag, value)
    assert code == 1 and flag in err


def test_race_lists_a_skipped_policy_with_its_reason(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "policies" / "wild.py").write_text(BAD_ACTION)
    code, out, _ = _run(capsys, "policies", "race", str(project), "--episodes", "3")
    assert code == 0
    assert "Skipped" in out and "wild" in out and "no-such-product" in out


def test_race_prints_the_dollar_warning(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = policies_cli.race

    def warned(*args: object, **kwargs: object) -> RaceResult:
        result = real(*args, **kwargs)  # type: ignore[arg-type]
        rows = list(result.results)
        rows[0] = dataclasses.replace(rows[0], dollar_warning="x wins on your score but loses")
        return dataclasses.replace(result, results=tuple(rows))

    monkeypatch.setattr(policies_cli, "race", warned)
    code, out, _ = _run(capsys, "policies", "race", str(project), "--episodes", "3")
    assert code == 0 and "Warning: x wins on your score but loses" in out


def test_an_internal_failure_is_exit_2(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> RaceResult:
        raise RuntimeError("kaput")

    monkeypatch.setattr(policies_cli, "race", boom)
    code, _, err = _run(capsys, "policies", "race", str(project), "--episodes", "3")
    assert code == 2 and "internal error" in err


def test_race_moves_can_be_read_with_the_plants_own_views(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "race"
    code, _, _ = _run(
        capsys, "policies", "race", str(project), "--episodes", "3", "--out", str(out)
    )
    assert code == 0
    assert json.loads((out / "scenario.json").read_text())["model"]["domain"] == "make_to_stock"

    kept = sorted(p for p in (out / "policies").iterdir() if p.is_dir())[0]
    code, printed, _ = _run(capsys, "states", str(kept), "--view", "simple", "--top", "3")

    assert code == 0
    assert "Top states (view simple)" in printed


# --- B-31/B-32: the race shows up in the workspace DECISIONS pages ------------------------------


def _stored_races(workspace: Path) -> list[Path]:
    return sorted((workspace / "decisions" / "race").glob("*.json"))


def test_race_publishes_its_view_into_the_workspace_store(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from twinflow.report.decision_store import DecisionStore

    workspace = tmp_path / "ws"
    code, out, _ = _run(
        capsys, "policies", "race", str(project), "--episodes", "3",
        "--out", str(tmp_path / "out"), "--workspace", str(workspace),
    )  # fmt: skip
    assert code == 0
    stored = _stored_races(workspace)
    assert len(stored) == 1
    race_id = stored[0].stem
    model = DecisionStore(workspace).load("race", race_id)
    assert model["view"] == "race"
    assert race_id.startswith(f"{model['about']['project']}-")  # molding-press-<stamp>
    assert f"Workspace: {race_id}" in out


def test_race_publishes_to_the_workspace_named_in_the_environment(
    project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "env-ws"
    monkeypatch.setenv("TWINFLOW_WORKSPACE_ROOT", str(workspace))
    code, _, _ = _run(
        capsys, "policies", "race", str(project), "--episodes", "3", "--out", str(tmp_path / "o")
    )
    assert code == 0 and len(_stored_races(workspace)) == 1


def test_race_without_a_workspace_writes_no_store(
    project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TWINFLOW_WORKSPACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    code, out, _ = _run(
        capsys, "policies", "race", str(project), "--episodes", "3", "--out", str(tmp_path / "o")
    )
    assert code == 0 and "Workspace:" not in out
    assert not list(tmp_path.rglob("decisions"))
