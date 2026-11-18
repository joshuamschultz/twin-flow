"""TF-006 B-23 and B-24: the words the tuning and details sections need.

Tuning gets one plain sentence per rule. Details gets the early-leader sentence that the old
race report carried under its rank chart.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from twinflow.extensions.project import Project
from twinflow.extensions.race import RaceResult, race
from twinflow.extensions.report_model import label, race_view_model

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "molding-press"


@pytest.fixture(scope="module")
def raced() -> tuple[Project, RaceResult]:
    project = Project.open(EXAMPLE)
    settings = dataclasses.replace(project.config.race, episodes=12, seed=0)
    return project, race(project, settings=settings)


@pytest.fixture(scope="module")
def model(raced: tuple[Project, RaceResult]) -> dict[str, Any]:
    project, result = raced
    return race_view_model(result, project)


def test_every_tuned_rule_has_a_plain_sentence(model: dict[str, Any]) -> None:
    assert model["tuning"]
    for row in model["tuning"]:
        words = row["words"]
        assert isinstance(words, str) and words
        assert "—" not in words
        if row["tried"] > 1:
            assert "gave the most profit" in words


def test_tuning_sentence_names_the_best_and_worst_setting(model: dict[str, Any]) -> None:
    row = next(t for t in model["tuning"] if t["policy"] == "family_campaign")
    scored = [t for t in row["trials"] if t["dollars"] is not None]
    best = max(scored, key=lambda t: t["dollars"])
    worst = min(scored, key=lambda t: t["dollars"])
    gap = best["dollars"] - worst["dollars"]
    best_text = " ".join(str(v).removesuffix(".0") for v in best["settings"].values())
    assert best_text in row["words"]
    assert f"${gap:,}" in row["words"]


def test_tuning_sentences_start_with_a_capital(model: dict[str, Any]) -> None:
    for row in model["tuning"]:
        assert row["words"][0].isupper()


def test_touchy_rule_gets_a_caution_in_its_sentence(model: dict[str, Any]) -> None:
    touchy = [t for t in model["tuning"] if t["touchy"]]
    steady = [t for t in model["tuning"] if not t["touchy"] and t["tried"] > 1]
    for row in touchy:
        assert "touchy" in row["words"]
    for row in steady:
        assert "touchy" not in row["words"]


def test_early_leader_sentence(raced: tuple[Project, RaceResult], model: dict[str, Any]) -> None:
    _, result = raced
    leader = result.early_leader
    block = model["early_leader"]
    assert leader is not None
    assert block["policy"] == leader
    assert block["label"] == label(leader)
    assert f"{label(leader)} led after day 1." in block["words"]
    assert "—" not in block["words"]


def test_early_leader_names_the_day_the_pick_passed_it(
    raced: tuple[Project, RaceResult], model: dict[str, Any]
) -> None:
    _, result = raced
    leader = result.early_leader
    pick = result.ranking()[0].policy
    assert leader is not None
    block = model["early_leader"]
    if leader == pick:
        assert "kept the lead" in block["words"]
    else:
        day = result.first_day_passed(leader, pick)
        if day is None:
            assert "passed" not in block["words"]
        else:
            assert f"passed it on day {day}" in block["words"]


def test_early_leader_is_null_when_nothing_finished() -> None:
    from twinflow.extensions.report_model import early_leader_block

    class Empty:
        early_leader = None

    assert early_leader_block(Empty(), None) is None  # type: ignore[arg-type]


def test_method_note_uses_the_word_rule(model: dict[str, Any]) -> None:
    method = model["about"]["method"]
    assert "policy" not in method.lower() and "policies" not in method.lower()
    assert method.startswith(f"Every rule ran {model['about']['episodes']} times")
