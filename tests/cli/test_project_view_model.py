"""TF-006 B-30: the project view model (DATA-CONTRACT 5) and `extensions check --html`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from twinflow.cli.main import main
from twinflow.extensions.project import Project, ProjectError
from twinflow.extensions.project_model import project_error_model, project_view_model

ROOT = Path(__file__).resolve().parents[2]
PRESS = ROOT / "examples" / "molding-press"
SCHEMA = ROOT / "src" / "twinflow" / "report" / "schemas" / "project.schema.json"
EM_DASH = "—"
BAD_ACTION = "NAME = 'my_rule'\n\n\ndef decide(state):\n    return 'GRY-X'\n"


def _valid(model: dict[str, Any]) -> list[str]:
    schema = json.loads(SCHEMA.read_text())
    return [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(model)]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


@pytest.fixture(scope="module")
def press_model() -> dict[str, Any]:
    return project_view_model(Project.open(PRESS))


@pytest.fixture
def broken(tmp_path: Path) -> Path:
    folder = tmp_path / "press"
    shutil.copytree(PRESS, folder)
    (folder / "policies" / "my_rule.py").write_text(BAD_ACTION)
    return folder


def test_the_molding_press_is_ready(press_model: dict[str, Any]) -> None:
    assert press_model["status"]["level"] == "ready"
    assert press_model["status"]["headline"] == "Ready. 0 problems to fix."
    assert press_model["twin"]["products"] == 12
    assert [f["name"] for f in press_model["twin"]["families"]] == ["grey", "black", "natural"]
    assert press_model["about"]["summary"] == (
        "One press, 12 products in 3 families. Each run is 60 days."
    )
    own = [r for r in press_model["rules"] if r["source"] == "project"]
    built = [r for r in press_model["rules"] if r["source"] == "builtin"]
    assert (len(own), len(built)) == (2, 10)
    assert [r["policy"] for r in press_model["rules"] if r["is_baseline"]] == ["lowest_cover"]
    assert press_model["score"] == {"name": "dollars", "is_dollars": True, "file": None}
    assert press_model["problems"] == []
    assert _valid(press_model) == []


def test_costs_are_in_plain_words(press_model: dict[str, Any]) -> None:
    costs = {c["label"]: c["value"] for c in press_model["twin"]["costs"]}
    assert costs["Colour change"] == "$1,200 and half a day"
    assert costs["Same colour change"] == "$200 and 10% of a day"
    assert press_model["twin"]["assumptions"][0].startswith("demand is Poisson")
    assert press_model["twin"]["horizon"] == {"value": 60, "unit": "days"}


def test_a_bad_policy_file_comes_first_with_file_line_and_fix(broken: Path) -> None:
    model = project_view_model(Project.open(broken))
    assert model["status"]["level"] == "needs_fixes"
    assert model["status"]["errors"] >= 1
    assert model["status"]["headline"].endswith("to fix before you can race.")
    first = model["problems"][0]
    assert first["severity"] == "error"
    assert first["where"].endswith("my_rule.py") and first["line"]
    assert "GRY-X" in first["message"] and first["fix"]
    assert _valid(model) == []


def test_a_project_that_cannot_open_gives_a_broken_page(tmp_path: Path) -> None:
    folder = tmp_path / "press"
    shutil.copytree(PRESS, folder)
    capsule = next(folder.glob("*.twin.yaml"))
    capsule.write_text(capsule.read_text().replace("horizon: 60", "horizon: sixty"))
    with pytest.raises(ProjectError) as caught:
        Project.open(folder)
    model = project_error_model(folder, caught.value)
    assert model["status"]["level"] == "broken" and model["problems"]
    assert _valid(model) == []


def test_recent_results_list_the_latest_improve_run(tmp_path: Path) -> None:
    folder = tmp_path / "press"
    shutil.copytree(PRESS, folder)
    run = folder / "runs" / "improve-20261117T080000Z"
    run.mkdir(parents=True)
    (run / "improve.json").write_text(
        json.dumps(
            {
                "promoted": True,
                "candidate": {"name": "family_campaign"},
                "previous_best": {"name": "lowest_stock_first"},
                "written_at": "2026-11-17T08:00:00+00:00",
            }
        )
    )
    (run / "improve.html").write_text("<html></html>")
    recent = project_view_model(Project.open(folder))["recent"]
    assert recent[0]["view"] == "improve" and recent[0]["id"] == run.name
    assert recent[0]["href"] == "runs/improve-20261117T080000Z/improve.html"
    assert "Family campaign" in recent[0]["sentence"]


def test_a_project_with_no_policies_says_the_built_ins_will_race(tmp_path: Path) -> None:
    folder = tmp_path / "press"
    shutil.copytree(PRESS, folder)
    shutil.rmtree(folder / "policies")
    model = project_view_model(Project.open(folder))
    assert [r for r in model["rules"] if r["source"] == "project"] == []
    assert "No rules of your own yet" in model["rules_note"]


def test_no_em_dash_in_any_sentence(press_model: dict[str, Any], broken: Path) -> None:
    for model in (press_model, project_view_model(Project.open(broken))):
        assert not [s for s in _strings(model) if EM_DASH in s]


def test_check_html_writes_project_html(broken: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extensions", "check", str(broken), "--html"]) == 1
    capsys.readouterr()
    page = (broken / "project.html").read_text()
    assert '"view":"project"' in page and "my_rule.py" in page


def test_check_html_on_a_ready_project_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "press"
    shutil.copytree(PRESS, folder)
    assert main(["extensions", "check", str(folder), "--html"]) == 0
    capsys.readouterr()
    assert "Ready. 0 problems to fix." in (folder / "project.html").read_text()
