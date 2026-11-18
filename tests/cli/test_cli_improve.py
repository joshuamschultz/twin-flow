"""T-253: `twinflow improve <project> --candidate FILE` (COMP-159, REQ-212, D-144, D-152)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from twinflow.cli import improve as improve_cli
from twinflow.cli.main import main
from twinflow.extensions.race import RaceResult

ROOT = Path(__file__).resolve().parents[2]
PRESS = ROOT / "examples" / "molding-press"

IDLE = "NAME = 'rest_always'\n\n\ndef decide(state):\n    return 'idle'\n"
BROKEN = "def decide(state)\n    return 'idle'\n"
BAD_ACTION = "NAME = 'wild'\n\n\ndef decide(state):\n    return 'no-such-product'\n"
DISPATCH = "NAME = 'edd_rule'\nDECISION = 'dispatch'\n\n\ndef decide(state):\n    return 'edd'\n"
EPISODES = ("--episodes", "20")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The press with one policy file: lowest_stock_first. family_campaign is the candidate."""
    folder = tmp_path / "project"
    shutil.copytree(PRESS, folder)
    (folder / "policies" / "family_campaign.py").rename(tmp_path / "family_campaign.py")
    return folder


@pytest.fixture
def better(tmp_path: Path) -> Path:
    return tmp_path / "family_campaign.py"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _improve(
    capsys: pytest.CaptureFixture[str], project: Path, candidate: Path, *more: str
) -> tuple[int, str, str]:
    code = main(["improve", str(project), "--candidate", str(candidate), *EPISODES, *more])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _proof(project: Path) -> dict[str, object]:
    (folder,) = sorted((project / "runs").glob("improve-*"))
    return json.loads((folder / "improve.json").read_text())  # type: ignore[no-any-return]


def test_a_better_candidate_is_copied_with_its_proof(
    project: Path, better: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _improve(capsys, project, better, "--seed", "4")
    assert code == 0
    assert (project / "policies" / "family_campaign.py").read_bytes() == better.read_bytes()
    assert "Copied" in out and "improve.json" in out and "dollars" in out.lower()
    proof = _proof(project)
    assert proof["promoted"] is True and proof["rule"] == "interval_above_zero"
    versus = proof["versus"]
    assert versus["score"]["low"] > 0 and versus["dollars"]["mean"] > 0  # type: ignore[index]
    assert proof["candidate"]["name"] == "family_campaign"  # type: ignore[index]
    assert proof["previous_best"]["name"] == "lowest_stock_first"  # type: ignore[index]
    assert proof["candidate"]["settings"] == {"target_days": 16}  # type: ignore[index]
    assert proof["previous_best"]["settings"] == {"stop_days": 10}  # type: ignore[index]
    assert proof["episodes"] == 20 and proof["seed"] == 4
    assert proof["tune_seed"] == 777 and proof["interval"] == 0.95
    assert proof["copied_to"] == str(project / "policies" / "family_campaign.py")


def test_a_worse_candidate_is_not_copied_and_says_why(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _improve(capsys, project, _write(tmp_path, "rest.py", IDLE))
    assert code == 0
    assert not (project / "policies" / "rest.py").exists()
    assert "Not copied" in out and "first" in out
    proof = _proof(project)
    assert proof["promoted"] is False and proof["copied_to"] is None
    assert proof["reason"] and proof["versus"]["score"]["mean"] < 0  # type: ignore[index]


def test_a_tie_is_not_copied(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    twin = (PRESS / "policies" / "lowest_stock_first.py").read_text()
    clone = _write(tmp_path, "twin.py", twin.replace("lowest_stock_first", "the_twin"))
    code, out, _ = _improve(capsys, project, clone)
    assert code == 0 and not (project / "policies" / "twin.py").exists()
    assert "Not copied" in out
    assert _proof(project)["promoted"] is False


def test_an_existing_file_of_the_same_name_is_refused_unless_replace(
    project: Path, better: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kept = project / "policies" / "family_campaign.py"
    kept.write_text("# old\n" + better.read_text())
    code, _, err = _improve(capsys, project, better)
    assert code == 1 and str(kept) in err and "--replace" in err
    assert kept.read_text().startswith("# old")
    assert not (project / "runs").exists()  # nothing was raced
    code, out, _ = _improve(capsys, project, better, "--replace")
    assert code == 0 and kept.read_bytes() == better.read_bytes() and "Copied" in out


def test_a_missing_candidate_names_the_path(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _improve(capsys, project, tmp_path / "ghost.py")
    assert code == 1 and str(tmp_path / "ghost.py") in err and "Fix:" in err


def test_a_missing_candidate_flag_is_exit_1(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["improve", str(project)])
    assert code == 1 and "--candidate" in capsys.readouterr().err


@pytest.mark.parametrize(
    "text,words",
    [
        (BROKEN, ("broken.py", "Fix:")),
        (BAD_ACTION, ("no-such-product", "wild")),
        (DISPATCH, ("dispatch", "make_next")),
    ],
)
def test_an_unusable_candidate_is_exit_1_and_not_copied(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, words: tuple[str]
) -> None:
    file = _write(tmp_path, "broken.py", text)
    code, out, err = _improve(capsys, project, file)
    assert code == 1 and all(w in out + err for w in words)
    assert not (project / "policies" / "broken.py").exists()


def test_a_project_with_no_policy_files_is_raced_against_the_built_ins(
    project: Path, better: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(project / "policies")
    code, out, _ = _improve(capsys, project, better, "--episodes", "10")
    assert code == 0 and "built-in" in out
    assert (project / "policies" / "family_campaign.py").is_file()


def test_a_missing_project_and_a_job_shop_are_exit_1(
    project: Path, tmp_path: Path, better: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _improve(capsys, tmp_path / "nope", better)
    assert code == 1 and str(tmp_path / "nope") in err
    code, _, err = _improve(capsys, ROOT / "examples" / "cnc-shop", better)
    assert code == 1 and "make_to_stock" in err


def test_an_internal_failure_is_exit_2(
    project: Path, better: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> RaceResult:
        raise RuntimeError("kaput")

    monkeypatch.setattr(improve_cli, "race", boom)
    code, _, err = _improve(capsys, project, better)
    assert code == 2 and "internal error" in err


# --- the rule, without a race -----------------------------------------------------------


def test_interval_rule_needs_the_whole_range_above_zero() -> None:
    wobbly = [5.0, -3.0, 9.0, -1.0, 7.0, 2.0]  # mean above zero, range crosses zero
    assert improve_cli.passes(improve_cli.paired(wobbly, [0.0] * 6, 0.95), "mean_above_zero")
    assert not improve_cli.passes(
        improve_cli.paired(wobbly, [0.0] * 6, 0.95), "interval_above_zero"
    )
    steady = [5.0, 6.0, 5.5, 6.5, 5.0, 6.0]
    assert improve_cli.passes(improve_cli.paired(steady, [0.0] * 6, 0.95), "interval_above_zero")


def test_one_run_cannot_prove_a_win() -> None:
    one = improve_cli.paired([5.0], [0.0], 0.95)
    assert improve_cli.passes(one, "mean_above_zero")
    assert not improve_cli.passes(one, "interval_above_zero")
