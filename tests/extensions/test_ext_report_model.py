"""T-269: the decision data behind the race report (REQ-228, REQ-229).

`race_view_model(result, project)` is the one JSON document the report page reads: the
recommendation, the baseline ("what we do today"), money per run and per year, how sure we
are in plain words, what changes on the floor, cost parts, daily profit bands, a day-by-day
example run, Markov maps with money, leaks, tuning and warnings.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from twinflow.extensions.project import Project, RaceSettings
from twinflow.extensions.race import RaceResult, race, stat
from twinflow.extensions.report_model import KEYS, label, race_view_model

MakeProject = Callable[..., Path]
QUICK = {"episodes": 6, "seed": 3, "tune_episodes": 2, "top_policies": 2}


@pytest.fixture(scope="module")
def golden(golden_race: tuple[Project, RaceResult]) -> tuple[RaceResult, dict[str, Any]]:
    project, result = golden_race
    return result, race_view_model(result, project)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def test_the_model_is_plain_json_with_every_key(golden: tuple[RaceResult, dict]) -> None:
    _, model = golden
    assert set(KEYS) <= set(model)
    text = json.dumps(model, allow_nan=False)
    assert json.loads(text) == model
    assert not any("—" in s for s in _strings(model))  # no em dashes in any words


def test_recommendation_and_baseline(golden: tuple[RaceResult, dict]) -> None:
    result, model = golden
    best = result.ranking()[0]
    assert model["recommendation"]["policy"] == best.policy
    assert model["baseline"]["policy"] == "lowest_cover"
    assert model["baseline"]["source"] == "setting"
    assert model["recommendation"]["same_as_baseline"] is False
    sentence = model["recommendation"]["sentence"]
    assert label(best.policy) in sentence and "Lowest cover" in sentence  # B-05 R20: labels


def test_money_per_run_and_per_year(golden: tuple[RaceResult, dict]) -> None:
    result, model = golden
    best, base = result.ranking()[0], result.result("lowest_cover")
    delta = model["delta"]
    per_run = best.dollars.mean - base.dollars.mean
    assert delta["per_run"] == round(per_run)
    assert delta["runs_per_year"] == pytest.approx(365 / 60, abs=0.01)
    assert delta["per_year"] == round(per_run * 365 / 60)
    assert "365" in delta["assumption"] and "60" in delta["assumption"]


def test_confidence_in_words_from_the_paired_interval(golden: tuple[RaceResult, dict]) -> None:
    result, model = golden
    best, base = result.ranking()[0], result.result("lowest_cover")
    diffs = np.subtract(best.episode_dollars, base.episode_dollars)
    paired = stat(list(diffs), 0.95)
    sure = model["confidence"]
    assert (sure["low"], sure["high"]) == (round(paired.low), round(paired.high))
    assert sure["wins"] == int((diffs > 0).sum()) and sure["runs"] == len(diffs)
    assert paired.low > 0 and sure["level"] == "very_likely_better"
    assert sure["words"].startswith("Very likely better")
    assert f"${round(paired.low):,}" in sure["detail"]


def test_floor_changes_against_the_baseline(golden: tuple[RaceResult, dict]) -> None:
    result, model = golden
    best, base = result.ranking()[0], result.result("lowest_cover")
    floor = {row["key"]: row for row in model["floor"]}
    # B-05 R21: cross_family is folded into the changeovers note, rest_days is new
    assert set(floor) == {"changeovers", "fill_rate", "avg_stock", "rest_days", "lost_units"}
    row = floor["changeovers"]
    assert row["change"] == pytest.approx(best.changeovers - base.changeovers, abs=0.05)
    assert row["better"] == (best.changeovers < base.changeovers)
    assert floor["fill_rate"]["recommended"] == pytest.approx(best.fill_rate, abs=1e-4)
    assert all(row["words"] for row in floor.values())
    assert model["recommendation"]["why"]


def test_every_policy_has_parts_and_daily_profit_bands(golden: tuple[RaceResult, dict]) -> None:
    result, model = golden
    rows = {p["policy"]: p for p in model["policies"]}
    assert list(rows) == [r.policy for r in result.ranking()]
    for raced in result.ranking():
        row = rows[raced.policy]
        assert row["parts"] == {k: round(v) for k, v in raced.parts.items()}
        days = row["profit_by_day"]
        assert len(days["mean"]) == len(days["low"]) == len(days["high"]) == 60
        assert days["mean"][-1] == round(raced.cumulative_dollars.mean[-1])
        assert days["low"][-1] <= days["mean"][-1] <= days["high"][-1]
        assert row["is_baseline"] == (raced.policy == "lowest_cover")


def test_an_example_run_for_the_top_policies_and_the_baseline(
    golden: tuple[RaceResult, dict],
) -> None:
    result, model = golden
    products = [p["id"] for p in model["products"]]
    assert len(products) == 12 and all(p["low_stock"] > 0 for p in model["products"])
    assert set(model["example_runs"]) == set(result.kept)
    run = model["example_runs"]["lowest_cover"]
    typical = model["example_runs"][result.ranking()[0].policy]["episode"]
    assert run["episode"] == typical and len(run["days"]) == 60  # B-05 R9: the typical run
    moves = list(result.moves("lowest_cover"))[60 * typical : 60 * (typical + 1)]
    for day, move in zip(run["days"], moves, strict=True):
        assert day["day"] == move.index + 1
        assert set(day["stock"]) == set(products)
        assert day["stock"]["GRY-S"] == round(move.after.stock["GRY-S"])
        assert day["setup"] == move.after.setup["press"]
    assert run["days"][-1]["cumulative"] == round(sum(m.ledger.total for m in moves))


def test_markov_maps_with_money(golden: tuple[RaceResult, dict]) -> None:
    result, model = golden
    assert set(model["markov"]) == set(result.chains)
    for name, chain in result.chains.items():
        graph = model["markov"][name]
        ids = {node["id"] for node in graph["nodes"]}
        assert [n["label"] for n in graph["nodes"]] == list(chain.states)
        assert all(e["from"] in ids and e["to"] in ids for e in graph["edges"])
        node = graph["nodes"][0]
        money = chain.money[node["label"]]
        assert node["per_day"] == round(money.total)
        assert node["sticky"] == (node["label"] in chain.sticky)
        assert node["share"] == pytest.approx(chain.share[node["label"]], abs=1e-4)


def test_leaks_tuning_and_warnings(golden: tuple[RaceResult, dict]) -> None:
    result, model = golden
    best = result.ranking()[0]
    assert [x["state"] for x in model["leaks"][best.policy]] == [x.state for x in best.leaks]
    tuned = {t["policy"]: t for t in model["tuning"]}
    assert set(tuned) == {t.policy for t in result.trials}
    for name, row in tuned.items():
        assert row["chosen"] == dict(result.result(name).settings)
        assert len(row["trials"]) == sum(t.policy == name for t in result.trials)
    assert isinstance(model["warnings"], list)
    assert model["what_if"]["note"].startswith("Rules act the same here.")


def test_keep_running_when_the_baseline_wins(shared_project: MakeProject) -> None:
    project = Project.open(shared_project({"race": {**QUICK, "baseline": "lowest_cover"}}))
    result = race(project, ["lowest_cover", "idle"])
    model = race_view_model(result, project)
    assert model["recommendation"]["same_as_baseline"] is True
    assert model["recommendation"]["sentence"].startswith("Keep running Lowest cover")
    assert model["delta"]["per_run"] == 0
    assert model["confidence"]["level"] == "same_policy"


def test_a_baseline_left_out_of_the_race_is_a_warning(shared_project: MakeProject) -> None:
    project = Project.open(shared_project({"race": {**QUICK, "baseline": "kanban"}}))
    result = race(project, ["lowest_cover", "idle"], settings=RaceSettings(**QUICK))
    model = race_view_model(result, project)
    assert model["baseline"]["policy"] == "lowest_cover"  # the built-in default stands in
    assert any("kanban" in w["text"] for w in model["warnings"])
