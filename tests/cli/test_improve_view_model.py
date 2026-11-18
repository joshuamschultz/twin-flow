"""TF-006 B-29: the improve view model (DATA-CONTRACT 4) and `improve.html`."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from twinflow.cli.improve import paired, passes
from twinflow.cli.main import main
from twinflow.extensions.improve_model import improve_view_model
from twinflow.extensions.project import Project, RaceSettings
from twinflow.extensions.race import PolicyResult, RaceResult, race

ROOT = Path(__file__).resolve().parents[2]
PRESS = ROOT / "examples" / "molding-press"
SCHEMA = ROOT / "src" / "twinflow" / "report" / "schemas" / "improve.schema.json"
EM_DASH = "—"


@pytest.fixture(scope="module")
def raced() -> RaceResult:
    project = Project.open(PRESS)
    names = ["family_campaign", "lowest_stock_first"]
    return race(project, names, settings=RaceSettings(episodes=12, seed=4, tune_episodes=4))


def _proof(result: RaceResult, name: str, versus_score: Any, promoted: bool) -> dict[str, Any]:
    ranking = result.ranking()
    other = next(r for r in ranking if r.policy != name)
    candidate = result.result(name)
    dollars = paired(candidate.episode_dollars, other.episode_dollars, 0.95)

    def row(item: PolicyResult) -> dict[str, Any]:
        return {
            "name": item.policy,
            "file": f"policies/{item.policy}.py",
            "sha256": hashlib.sha256(item.policy.encode()).hexdigest(),
            "settings": dict(item.settings),
        }

    return {
        "project": "/work/molding-press",
        "promoted": promoted,
        "reason": None,
        "rule": "interval_above_zero",
        "interval": 0.95,
        "episodes": result.settings.episodes,
        "seed": result.settings.seed,
        "score_name": "dollars",
        "candidate": row(candidate),
        "previous_best": {k: v for k, v in row(other).items() if k != "sha256"},
        "versus": {"score": versus_score.to_json(), "dollars": dollars.to_json()},
        "ranking": [r.policy for r in ranking],
        "copied_to": f"policies/{name}.py" if promoted else None,
        "written_at": "2026-11-18T09:00:00+00:00",
    }


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


def _won(raced: RaceResult) -> dict[str, Any]:
    first = raced.ranking()[0].policy
    other = raced.ranking()[1]
    score = paired(raced.result(first).episode_score, other.episode_score, 0.95)
    return improve_view_model(
        _proof(raced, first, score, passes(score, "interval_above_zero")), raced
    )


def test_a_win_says_yes_and_every_check_passes(raced: RaceResult) -> None:
    model = _won(raced)
    assert model["outcome"]["level"] == "won"
    assert re.fullmatch(r"Yes\. .+ is now your best rule\.", model["outcome"]["headline"])
    assert [c["key"] for c in model["checks"]] == [
        "ranked_first", "proven", "same_orders", "no_problems"
    ]  # fmt: skip
    assert all(c["pass"] for c in model["checks"])
    assert model["versus"]["wins"] <= model["versus"]["runs"] == 12
    assert len(model["versus"]["gaps"]) == 12
    assert model["floor"] and model["race_ref"] == "race.html"
    assert _valid(model) == []


def test_ahead_but_not_proven_names_the_range(raced: RaceResult) -> None:
    first = raced.ranking()[0].policy
    other = raced.ranking()[1]
    score = paired(raced.result(first).episode_score, other.episode_score, 0.95)
    wide = type(score)(mean=1200.0, std=900.0, low=-900.0, high=4100.0, n=12)
    model = improve_view_model(_proof(raced, first, wide, False), raced)
    assert model["outcome"]["level"] == "ahead_not_proven"
    assert model["outcome"]["headline"].startswith("Not yet.")
    assert "-$900" in model["outcome"]["reason"] and "4,100" in model["outcome"]["reason"]
    proven = next(c for c in model["checks"] if c["key"] == "proven")
    ranked = next(c for c in model["checks"] if c["key"] == "ranked_first")
    assert proven["pass"] is False and ranked["pass"] is True
    assert _valid(model) == []


def test_a_loss_says_no_and_names_the_old_best(raced: RaceResult) -> None:
    last = raced.ranking()[-1].policy
    first = raced.ranking()[0]
    score = paired(raced.result(last).episode_score, first.episode_score, 0.95)
    model = improve_view_model(_proof(raced, last, score, False), raced)
    assert model["outcome"]["level"] == "lost"
    assert model["outcome"]["headline"].startswith("No.")
    assert model["outcome"]["headline"].endswith("is still your best rule.")
    ranked = next(c for c in model["checks"] if c["key"] == "ranked_first")
    assert ranked["pass"] is False
    assert _valid(model) == []


def test_no_em_dash_in_any_sentence(raced: RaceResult) -> None:
    assert not [s for s in _strings(_won(raced)) if EM_DASH in s]


def test_improve_writes_improve_html_next_to_improve_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    shutil.copytree(PRESS, folder)
    (folder / "policies" / "family_campaign.py").rename(tmp_path / "family_campaign.py")
    code = main(["improve", str(folder), "--candidate", str(tmp_path / "family_campaign.py"),
                 "--episodes", "10", "--seed", "4"])  # fmt: skip
    capsys.readouterr()
    assert code == 0
    (out,) = sorted((folder / "runs").glob("improve-*"))
    page = (out / "improve.html").read_text()
    assert '"view":"improve"' in page and (out / "improve.json").is_file()
