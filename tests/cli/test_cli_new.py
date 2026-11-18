"""T-252 / T-265: `twinflow new <folder> --template NAME` and `twinflow new --list`
(COMP-159, COMP-165, REQ-211, REQ-225, D-149, D-155)."""

from __future__ import annotations

import fnmatch
import tomllib
from importlib import resources
from pathlib import Path

import polars as pl
import pytest

from twinflow.cli import new as new_cli
from twinflow.cli.main import main
from twinflow.scenario import ScenarioCapsule

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
TEMPLATES = ROOT / "src" / "twinflow" / "templates"
NAMES = ("make-to-stock", "job-shop", "custom-engine")


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_list_shows_every_template_with_a_plain_description(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _run(capsys, "new", "--list")
    assert code == 0
    for name in NAMES:
        assert any(line.startswith(name) and len(line) > len(name) + 5 for line in out.splitlines())


def test_the_template_folders_match_the_registry() -> None:
    assert {p.name for p in TEMPLATES.iterdir() if p.is_dir()} == set(new_cli.TEMPLATES)
    assert set(new_cli.TEMPLATES) == set(NAMES)


def test_new_creates_the_folder_and_names_the_capsule_after_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "my-plant"
    code, out, _ = _run(capsys, "new", str(target), "--template", "make-to-stock")
    assert code == 0
    assert (target / "my-plant.twin.yaml").is_file() and (target / "README.md").is_file()
    assert sorted(p.name for p in (target / "policies").iterdir()) == [
        "family_campaign.py",
        "lowest_stock_first.py",
    ]
    assert str(target) in out and "extensions check" in out


def test_new_accepts_an_empty_existing_folder(tmp_path: Path) -> None:
    target = tmp_path / "empty"
    target.mkdir()
    assert main(["new", str(target), "--template", "custom-engine"]) == 0
    assert (target / "engines" / "bakery.py").is_file()


def test_new_refuses_a_non_empty_folder_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    (target / "mine.txt").write_text("keep me")
    code, _, err = _run(capsys, "new", str(target), "--template", "make-to-stock")
    assert code == 1 and str(target) in err and "Fix:" in err
    assert [p.name for p in target.iterdir()] == ["mine.txt"]


def test_new_refuses_a_file_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x")
    code, _, err = _run(capsys, "new", str(target), "--template", "job-shop")
    assert code == 1 and str(target) in err


def test_unknown_template_gets_a_hint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _run(capsys, "new", str(tmp_path / "x"), "--template", "make-to-stok")
    assert code == 1 and "make-to-stok" in err and "make-to-stock" in err
    assert not (tmp_path / "x").exists()


def test_new_needs_a_template_and_a_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, "new", str(tmp_path / "x"))
    assert code == 1 and "--template" in err and "--list" in err
    code, _, err = _run(capsys, "new", "--template", "job-shop")
    assert code == 1 and "folder" in err


# --- the templates themselves ------------------------------------------------------------


def _same_files(left: Path, right: Path, names: list[str]) -> None:
    for name in names:
        assert (left / name).read_bytes() == (right / name).read_bytes(), name


def test_make_to_stock_is_the_molding_press_example() -> None:
    template, example = TEMPLATES / "make-to-stock", EXAMPLES / "molding-press"
    _same_files(
        template,
        example,
        [
            "molding-press.twin.yaml",
            "policies/family_campaign.py",
            "policies/lowest_stock_first.py",
        ],
    )
    assert {p.name for p in (template / "policies").iterdir()} == {
        p.name for p in (example / "policies").iterdir()
    }


def test_job_shop_is_the_cnc_shop_example_with_capture_and_money() -> None:
    template = ScenarioCapsule.load(TEMPLATES / "job-shop" / "job-shop.twin.yaml").to_dict()
    example = ScenarioCapsule.load(EXAMPLES / "cnc-shop" / "cnc-shop.twin.yaml").to_dict()
    assert template["experiment"].pop("state_capture")["every_hours"] > 0
    assert template["model"].pop("economics")
    assert template == example


def test_templates_ship_as_package_data() -> None:
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"][
        "package-data"
    ]["twinflow"]
    package = Path(str(resources.files("twinflow")))
    for file in (package / "templates").rglob("*"):
        if file.is_file() and "__pycache__" not in file.parts:
            relative = file.relative_to(package).as_posix()
            assert any(fnmatch.fnmatch(relative, pattern) for pattern in patterns), relative


@pytest.mark.parametrize("name", ["make-to-stock", "custom-engine"])
def test_a_new_project_checks_and_races_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    target = tmp_path / name
    assert main(["new", str(target), "--template", name]) == 0
    capsys.readouterr()
    code, out, err = _run(capsys, "extensions", "check", str(target))
    assert code == 0, out + err
    code, out, err = _run(capsys, "policies", "race", str(target), "--episodes", "5")
    assert code == 0, out + err
    assert "Rank" in out and (next((target / "runs").glob("race-*")) / "race.html").is_file()


def test_a_new_job_shop_runs_with_state_capture_and_reads_with_states(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "shop"
    assert main(["new", str(target), "--template", "job-shop"]) == 0
    capsys.readouterr()
    capsule = target / "shop.twin.yaml"
    code, out, err = _run(capsys, "scenario", "validate", str(capsule))
    assert code == 0, out + err
    run = tmp_path / "run"
    code, out, err = _run(capsys, "scenario", "run", str(capsule), "--reps", "1", "--out", str(run))
    assert code == 0, err
    moves = pl.read_parquet(next(run.rglob("moves.parquet")))
    assert moves.height > 0
    code, out, err = _run(capsys, "states", str(run))
    assert code == 0, out + err
    assert "total" in out
