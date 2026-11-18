"""B-14: `twinflow policies race --out` writes the new decision view as race.html.

`write_race_report` builds the race view model (with the 180-day check) and renders it
through `twinflow.report.decision`. The old Plotly page stays reachable as
`write_legacy_race_report` until B-33 retires it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from twinflow.extensions.project import Project, RaceSettings
from twinflow.extensions.race import RaceResult, race
from twinflow.extensions.report import race_html, write_legacy_race_report, write_race_report

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def raced() -> RaceResult:
    project = Project.open(ROOT / "examples" / "molding-press")
    return race(project, settings=RaceSettings(episodes=6, seed=0, tune_episodes=2))


def _model(text: str) -> dict:
    tag = r'<script type="application/json" id="twinflow-data">(.*?)</script>'
    found = re.search(tag, text, re.S)
    assert found is not None
    return json.loads(found.group(1))


def test_race_html_is_the_decision_view(raced: RaceResult, tmp_path: Path) -> None:
    out = write_race_report(raced, tmp_path / "race.html")
    text = out.read_text(encoding="utf-8")
    model = _model(text)
    assert model["view"] == "race" and model["schema_version"] == 1
    assert model["recommendation"]["policy"] == raced.ranking()[0].policy
    assert model["delta"]["long_check"]["days"] == 180  # owner decision 1
    assert "plotly" not in text.lower() and "mermaid" not in text.lower()
    assert len(text.encode()) < 1_200_000


def test_the_long_check_can_be_skipped(raced: RaceResult, tmp_path: Path) -> None:
    out = write_race_report(raced, tmp_path / "race.html", long_check=False)
    assert _model(out.read_text(encoding="utf-8"))["delta"]["long_check"] is None


def test_the_old_page_is_still_reachable(raced: RaceResult, tmp_path: Path) -> None:
    out = write_legacy_race_report(raced, tmp_path / "old.html")
    assert out.read_text(encoding="utf-8") == race_html(raced)
