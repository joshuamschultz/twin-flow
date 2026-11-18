"""TF-006 B-05: the race view-model gaps R1 to R24 (DATA-CONTRACT 1.3).

The golden race is the one the design was drawn from:
`twinflow policies race examples/molding-press --episodes 40 --seed 0` (every policy, the
capsule's own race settings). It takes about 10 s, so it runs once per module.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from twinflow.extensions.project import Project
from twinflow.extensions.race import RaceResult, race
from twinflow.extensions.report_model import (
    KEYS,
    SCHEMA_VERSION,
    horizon_check,
    label,
    race_view_model,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "molding-press"
MakeProject = Callable[..., Path]
QUICK = {"episodes": 6, "seed": 3, "tune_episodes": 2, "top_policies": 2}


@pytest.fixture(scope="module")
def cli_race() -> tuple[Project, RaceResult]:
    """`twinflow policies race examples/molding-press --episodes 40 --seed 0`."""
    project = Project.open(EXAMPLE)
    settings = dataclasses.replace(project.config.race, episodes=40, seed=0)
    return project, race(project, settings=settings)


@pytest.fixture(scope="module")
def long_check(cli_race: tuple[Project, RaceResult]) -> dict[str, Any] | None:
    project, result = cli_race
    return horizon_check(project, result, days=180)


@pytest.fixture(scope="module")
def model(
    cli_race: tuple[Project, RaceResult], long_check: dict[str, Any] | None
) -> dict[str, Any]:
    project, result = cli_race
    return race_view_model(result, project, long_check=long_check)


@pytest.fixture(scope="module")
def plain_model(cli_race: tuple[Project, RaceResult]) -> dict[str, Any]:
    project, result = cli_race
    return race_view_model(result, project)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


# --- R1, R15, R16: top level ------------------------------------------------------------------


def test_top_level_names_the_view_and_its_version(model: dict[str, Any]) -> None:
    assert model["view"] == "race"
    assert model["schema_version"] == SCHEMA_VERSION == 1
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", model["generated_at"])
    assert model["twinflow_version"]
    assert set(KEYS) <= set(model)
    assert json.loads(json.dumps(model, allow_nan=False)) == model
    assert not any("\u2014" in s for s in _strings(model))


def test_problems_and_agent_summary(model: dict[str, Any]) -> None:
    assert model["problems"] == []  # every policy ran
    summary = model["agent_summary"]
    assert 5 <= len(summary) <= 10
    assert summary[0] == model["recommendation"]["sentence"]
    assert all(s.endswith(".") and "\u2014" not in s for s in summary)
    assert all("family_campaign" not in s for s in summary)  # labels, not ids


# --- R2, R3, R4: run by run --------------------------------------------------------------------


def test_gaps_run_by_run(cli_race: tuple[Project, RaceResult], model: dict[str, Any]) -> None:
    _, result = cli_race
    gaps = model["confidence"]["gaps"]
    assert len(gaps) == 40 and all(g > 0 for g in gaps)
    pick, base = result.ranking()[0], result.result("lowest_cover")
    expected = np.subtract(pick.episode_dollars, base.episode_dollars)
    assert gaps == [round(x) for x in expected]
    rows = {p["policy"]: p for p in model["policies"]}
    assert rows["family_campaign"]["dollars_by_run"] == [round(x) for x in pick.episode_dollars]
    assert rows["family_campaign"]["wins_vs_baseline"] == 40
    assert rows["lowest_cover"]["wins_vs_baseline"] == 0
    assert rows["idle"]["wins_vs_baseline"] == 0


# --- R5: when it pulls ahead ------------------------------------------------------------------


def test_timing_is_the_paired_gap_by_day(model: dict[str, Any]) -> None:
    timing = model["timing"]
    gap = timing["gap_by_day"]
    assert len(gap["mean"]) == len(gap["low"]) == len(gap["high"]) == 60
    assert all(
        lo <= m <= hi for lo, m, hi in zip(gap["low"], gap["mean"], gap["high"], strict=True)
    )
    assert timing["pass_day"] == 54
    assert timing["lowest"]["day"] == 42
    assert timing["lowest"]["dollars"] == pytest.approx(-23545, abs=1)
    assert gap["mean"][-1] == model["delta"]["per_run"]
    assert "54" in timing["words"] and "$23,545" in timing["words"]


# --- R6: cautions ------------------------------------------------------------------------------


def test_cautions_in_rule_order(plain_model: dict[str, Any]) -> None:
    cautions = plain_model["cautions"]
    assert [c["key"] for c in cautions] == ["late_gain", "more_stock", "runs_short"]
    for caution in cautions:
        assert caution["severity"] in {"note", "warn"}
        assert caution["title"] and not caution["title"].endswith(".")
        assert len(re.findall(r"[.!?](\s|$)", caution["body"])) == 2  # two short sentences
    by_key = {c["key"]: c for c in cautions}
    assert by_key["more_stock"]["title"] == "You will hold 2.6 times more stock"
    assert by_key["more_stock"]["body"].startswith("About 3,400 units on the shelf, up from 1,300.")
    assert by_key["runs_short"]["title"] == "Natural products still run short"
    assert "Each natural product is out about 1 day in 6." in by_key["runs_short"]["body"]
    assert "day 54 of 60" in by_key["late_gain"]["body"]


def test_late_gain_adds_the_long_check(
    model: dict[str, Any], plain_model: dict[str, Any], long_check: dict[str, Any] | None
) -> None:
    assert long_check is not None
    late = {c["key"]: c for c in model["cautions"]}["late_gain"]
    plain = {c["key"]: c for c in plain_model["cautions"]}["late_gain"]
    assert late["body"] == f"{plain['body']} {long_check['words']}"
    assert model["delta"]["long_check"] == long_check
    assert plain_model["delta"]["long_check"] is None


# --- R7, R8: stock for every policy ------------------------------------------------------------


def test_stock_by_day_for_every_policy(
    cli_race: tuple[Project, RaceResult], model: dict[str, Any]
) -> None:
    _, result = cli_race
    rows = {p["policy"]: p for p in model["policies"]}
    assert len(rows) == 12
    for row in rows.values():
        days = row["stock_by_day"]["mean"]
        assert len(days) == 60
        assert row["stock_end"] == pytest.approx(days[-1], abs=0.1)
        assert row["stock_peak"] == pytest.approx(max(days), abs=0.1)
        assert row["stock_start"] > 0
        assert set(row["out_share_by_product"]) == set(result.project.facts.products)
        assert all(0 <= v <= 1 for v in row["out_share_by_product"].values())
    assert round(rows["family_campaign"]["stock_end"]) == 1411
    assert round(rows["lowest_cover"]["stock_end"]) == 1033
    # the stock tally agrees with the average already reported
    pick = rows["family_campaign"]
    assert np.mean(pick["stock_by_day"]["mean"]) == pytest.approx(pick["avg_stock"], abs=0.2)


def test_out_share_matches_the_kept_moves(cli_race: tuple[Project, RaceResult]) -> None:
    _, result = cli_race
    rate = result.project.facts.demand_rate
    moves = result.moves("family_campaign")
    expected = {p: float(np.mean([m.after.stock[p] < rate[p] for m in moves])) for p in rate}
    got = result.result("family_campaign").out_share
    assert got == pytest.approx(expected)


# --- R9, R10, R11, R12: the replay -------------------------------------------------------------


def test_example_runs_use_one_typical_run(model: dict[str, Any]) -> None:
    runs = model["example_runs"]
    assert {"family_campaign", "lowest_cover"} <= set(runs)
    gaps = np.asarray(model["confidence"]["gaps"], dtype=float)
    typical = int(np.argmin(np.abs(gaps - gaps.mean())))
    assert {r["episode"] for r in runs.values()} == {typical}
    assert all(r["why"] == "typical" for r in runs.values())
    pick = runs["family_campaign"]
    assert pick["days"][-1]["cumulative"] == model["policies"][0]["dollars_by_run"][typical]


def test_example_days_carry_parts_and_lost_by_product(model: dict[str, Any]) -> None:
    for run in model["example_runs"].values():
        for day in run["days"]:
            assert set(day["parts"]) == set(model["parts"])
            assert all(v > 0 for v in day["lost_by_product"].values())
            assert sum(day["lost_by_product"].values()) == pytest.approx(day["lost"], abs=0.2)
    days = model["example_runs"]["lowest_cover"]["days"]
    assert any(d["lost_by_product"] for d in days)


def test_the_baseline_is_always_kept(
    cli_race: tuple[Project, RaceResult], model: dict[str, Any]
) -> None:
    _, result = cli_race
    assert result.result("lowest_cover").rank == 5  # outside the top 3
    assert "lowest_cover" in result.kept and "lowest_cover" in model["markov"]
    assert model["leaks"]["lowest_cover"]


# --- R13, R14, R17, R18, R19: map, money and tuning words --------------------------------------


def test_view_levels_and_economics(model: dict[str, Any]) -> None:
    assert model["views"]["by"] == "days_of_cover"
    assert model["views"]["levels"] == [
        {"name": "low", "below": 3.0},
        {"name": "ok", "below": 10.0},
        {"name": "high", "below": None},
    ]
    economics = model["economics"]
    assert economics["changeover"]["across_family"] == {"cost": 1200, "output_lost": 0.5}
    assert economics["changeover"]["within_family"] == {"cost": 200, "output_lost": 0.1}
    assert economics["holding_rate"] == pytest.approx(0.01)
    assert economics["lost_sale_rate"] == pytest.approx(0.3)


def test_tuning_spread_and_touchy(
    cli_race: tuple[Project, RaceResult], model: dict[str, Any]
) -> None:
    _, result = cli_race
    rows = {t["policy"]: t for t in model["tuning"]}
    dollars = [t.dollars for t in result.trials if t.policy == "abc_wheel" and t.dollars]
    assert rows["abc_wheel"]["spread"] == round(max(dollars) - min(dollars))
    assert rows["abc_wheel"]["touchy"] is (max(dollars) - min(dollars) > 0.25 * abs(max(dollars)))
    assert rows["family_campaign"]["touchy"] is False


def test_map_nodes_and_leaks_have_words(model: dict[str, Any]) -> None:
    for name, graph in model["markov"].items():
        ids = {n["id"] for n in graph["nodes"]}
        for node in graph["nodes"]:
            assert node["words"]["title"]
            assert node["words"]["setup"].startswith("Press ")
        for leak in model["leaks"][name]:
            assert leak["state_id"] is None or leak["state_id"] in ids
            assert leak["try"].endswith(".")
    node = next(
        n for n in model["markov"]["family_campaign"]["nodes"]
        if n["label"] == "grey:high black:low natural:low | black"
    )  # fmt: skip
    assert node["words"] == {
        "title": "Grey high, black and natural low",
        "setup": "Press on black",
    }


# --- R20 to R24: labels, floor, long check ------------------------------------------------------


def test_labels_people_see() -> None:
    assert label("s_big_s") == "Reorder point"
    assert label("setup_aware_runout") == "Changeover-aware run-out"
    assert label("myopic") == "Best for today only"
    assert label("idle") == "Do nothing"
    assert label("family_campaign") == "Family campaign"
    assert label("my_new_rule") == "My new rule"


def test_sentence_uses_labels(model: dict[str, Any]) -> None:
    sentence = model["recommendation"]["sentence"]
    assert sentence.startswith("Run Family campaign instead of Lowest cover.")
    assert "$22,088" in sentence
    assert model["delta"]["per_run"] == 22088
    assert model["confidence"]["words"] == "Very likely better"
    rows = {p["policy"]: p for p in model["policies"]}
    assert rows["s_big_s"]["label"] == "Reorder point"


def test_floor_rows_for_the_hero(model: dict[str, Any]) -> None:
    floor = model["floor"]
    keys = [r["key"] for r in floor]
    assert keys == ["changeovers", "fill_rate", "avg_stock", "rest_days", "lost_units"]
    rows = {r["key"]: r for r in floor}
    assert [rows[k]["label"] for k in keys[:4]] == [
        "Changeovers per run",
        "Orders filled from stock",
        "Stock on hand",
        "Days the press rests",
    ]
    assert rows["changeovers"]["short"] == "14 fewer"
    assert rows["changeovers"]["note"].startswith("Colour changes drop from ")
    assert rows["changeovers"]["note"].endswith("Those cost $1,200 each.")
    assert rows["fill_rate"]["short"] == "+2.7 points"
    assert rows["fill_rate"]["note"] == "Fewer shoppers find empty shelves."
    assert rows["avg_stock"]["short"] == "2.6 times more"
    assert rows["avg_stock"]["unit"] == "units"
    assert rows["rest_days"]["unit"] == "days"
    assert rows["rest_days"]["short"].endswith(" more of 60")
    assert {k for k, r in rows.items() if r["attention"]} == {"avg_stock", "rest_days"}
    assert all(r["note"].endswith(".") for r in floor)
    assert all("better" in r and "words" in r for r in floor)


def test_long_check_shape(
    cli_race: tuple[Project, RaceResult], long_check: dict[str, Any] | None
) -> None:
    assert long_check is not None
    assert set(long_check) == {
        "days", "runs", "per_run", "per_year", "low_per_year", "high_per_year", "pass_day", "words",
    }  # fmt: skip
    assert long_check["days"] == 180 and long_check["runs"] == 40
    assert long_check["per_year"] == pytest.approx(long_check["per_run"] * 365 / 180, abs=2)
    assert long_check["low_per_year"] <= long_check["per_year"] <= long_check["high_per_year"]
    assert long_check["words"].startswith("A 180-day check gives about $")
    # Yearly figures are rounded to $100 everywhere, like the hero (DESIGN.md 3.1).
    rounded = round(long_check["per_year"] / 100) * 100
    assert f"${abs(rounded):,}" in long_check["words"]
    project, result = cli_race
    assert horizon_check(project, result, days=60) is None  # the race is already that long


def test_no_long_check_when_the_baseline_wins(shared_project: MakeProject) -> None:
    project = Project.open(shared_project({"race": {**QUICK, "baseline": "lowest_cover"}}))
    result = race(project, ["lowest_cover", "idle"])
    assert horizon_check(project, result) is None
    model = race_view_model(result, project)
    assert model["recommendation"]["sentence"].startswith("Keep running Lowest cover.")
    assert model["timing"] is None
    assert model["cautions"] == []
    assert model["confidence"]["gaps"] == [0] * 6
