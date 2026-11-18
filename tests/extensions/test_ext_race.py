"""T-239: the race core (COMP-157, REQ-206, D-148, D-152).

Every policy runs on the same demand draws (common random numbers); the stats equal a hand
computation; a broken or slow policy is listed and skipped, never crashes the race; the
result is JSON (race.json) and keeps the moves of the top policies.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
import scipy.stats  # type: ignore[import-untyped]

from twinflow.engines.contract import run_episode
from twinflow.extensions.project import Project, RaceSettings
from twinflow.extensions.race import (
    RaceResult,
    first_day_passed,
    race,
    rank_by_day,
)
from twinflow.state.recorder import read_moves

MakeProject = Callable[..., Path]

QUICK = {"episodes": 6, "seed": 3, "tune_episodes": 2, "top_policies": 3}
TRIO = ["lowest_cover", "idle", "myopic"]

BROKEN_ON_DAY_3 = '''
"""Breaks on day three."""
NAME = "breaks"


def decide(state):
    if state.period == 3:
        return {}["boom"]
    return "idle"
'''

SLOW = '''
"""Sleeps on day two."""
import time

NAME = "sleepy"


def decide(state):
    if state.period == 2:
        time.sleep(3)
    return "idle"
'''

BAD_IMPORT = "import no_such_module_anywhere\n"

# A score that likes losing money (on purpose): the ranking by score is the ranking by
# dollars upside down, so the score winner loses dollars to every other policy.
UPSIDE_DOWN = '''
"""Minus the dollars (a bad score, on purpose)."""
NAME = "upside_down"


def reward(move):
    return -move.ledger.total
'''


@pytest.fixture(scope="module")
def trio(shared_project: MakeProject) -> tuple[Project, RaceResult]:
    project = Project.open(shared_project({"race": QUICK}))
    return project, race(project, TRIO)


def test_every_policy_sees_the_same_demand(trio: tuple[Project, RaceResult]) -> None:
    _, result = trio
    demand = {}
    for name in TRIO:
        moves = result.moves(name)
        assert len(moves) == QUICK["episodes"] * 60
        demand[name] = [
            tuple(v for k, v in sorted(m.events.items()) if k.startswith("demand.")) for m in moves
        ]
    assert demand["lowest_cover"] == demand["idle"] == demand["myopic"]


def test_stats_equal_a_hand_computation(trio: tuple[Project, RaceResult]) -> None:
    project, result = trio
    plan = project.episodes(QUICK["episodes"], QUICK["seed"])
    policy = project.extensions.get("policy", "lowest_cover")
    row = result.result("lowest_cover")  # raced at its tuned settings (T-240)
    decide = project.decider(policy, plan.engine, settings=row.settings)
    totals, sold, asked, switches = [], 0.0, 0.0, 0
    for seed in plan.seeds:
        moves = run_episode(plan.engine, decide, seed)
        totals.append(sum(m.ledger.total for m in moves))
        for m in moves:
            sold += sum(v for k, v in m.events.items() if k.startswith("sold."))
            asked += sum(v for k, v in m.events.items() if k.startswith("demand."))
            switches += m.before.setup != m.after.setup
    n = len(totals)
    mean, std = float(np.mean(totals)), float(np.std(totals, ddof=1))
    half = scipy.stats.t.ppf(0.975, n - 1) * std / math.sqrt(n)
    assert row.dollars.mean == pytest.approx(mean, abs=1e-6)
    assert row.dollars.std == pytest.approx(std, abs=1e-6)
    assert (row.dollars.low, row.dollars.high) == pytest.approx((mean - half, mean + half))
    assert row.score.mean == pytest.approx(mean)  # the default score is dollars
    assert row.fill_rate == pytest.approx(sold / asked)
    assert row.changeovers == pytest.approx(switches / n)
    assert sum(row.parts.values()) != 0
    parts = row.parts
    net = parts["revenue"] - sum(v for k, v in parts.items() if k != "revenue")
    assert net == pytest.approx(mean)


def test_ranking_is_best_first_with_paired_intervals(trio: tuple[Project, RaceResult]) -> None:
    _, result = trio
    ranked = result.ranking()
    assert [r.rank for r in ranked] == [1, 2, 3]
    assert ranked[0].score.mean >= ranked[1].score.mean >= ranked[2].score.mean
    best = ranked[0]
    assert best.vs_best is None
    other = ranked[1]
    diffs = np.array(other.episode_score) - np.array(best.episode_score)
    assert other.vs_best is not None
    assert other.vs_best.mean == pytest.approx(float(diffs.mean()))
    half = scipy.stats.t.ppf(0.975, len(diffs) - 1) * diffs.std(ddof=1) / math.sqrt(len(diffs))
    assert (other.vs_best.low, other.vs_best.high) == pytest.approx(
        (diffs.mean() - half, diffs.mean() + half)
    )


def test_cumulative_profit_and_rank_by_day(trio: tuple[Project, RaceResult]) -> None:
    _, result = trio
    row = result.result("lowest_cover")
    cum = np.cumsum(np.array(row.daily_dollars), axis=1)
    assert np.allclose(row.cumulative_dollars.mean, cum.mean(axis=0))
    assert np.allclose(row.cumulative_dollars.std, cum.std(axis=0, ddof=1))
    assert row.cumulative_dollars.mean[-1] == pytest.approx(row.dollars.mean)
    ranks = result.rank_by_day()
    assert set(ranks) == set(TRIO)
    assert all(len(r) == 60 for r in ranks.values())
    final = {r.policy: r.rank for r in result.ranking()}
    assert {name: r[-1] for name, r in ranks.items()} == final


def test_rank_by_day_and_first_day_passed_on_a_constructed_case() -> None:
    cumulative = {
        "fast": [10.0, 20.0, 25.0, 26.0, 27.0],
        "slow": [-5.0, 5.0, 25.0, 40.0, 60.0],
        "flat": [0.0, 0.0, 0.0, 0.0, 0.0],
    }
    assert rank_by_day(cumulative) == {
        "fast": [1, 1, 1, 2, 2],
        "slow": [3, 2, 1, 1, 1],  # a tie shares the best rank
        "flat": [2, 3, 3, 3, 3],
    }
    assert first_day_passed(cumulative, "fast", "slow") == 4  # days count from 1
    assert first_day_passed(cumulative, "slow", "fast") == 1
    assert first_day_passed(cumulative, "fast", "flat") is None


def test_broken_and_slow_policies_are_listed_and_skipped(make_project: MakeProject) -> None:
    folder = make_project(
        {"race": {**QUICK, "decision_timeout_s": 0.2}},
        files={
            "policies/breaks.py": BROKEN_ON_DAY_3,
            "policies/sleepy.py": SLOW,
            "policies/no_import.py": BAD_IMPORT,
        },
    )
    project = Project.open(folder)
    result = race(project, ["idle", "breaks", "sleepy", "no_import", "no_such_policy"])
    assert [r.policy for r in result.ranking()] == ["idle"]
    skipped = {s.name: s for s in result.skipped}
    assert set(skipped) == {"breaks", "sleepy", "no_import", "no_such_policy"}
    assert skipped["breaks"].code == "raised" and skipped["breaks"].line == 8
    assert "day 3" in skipped["breaks"].message
    assert skipped["sleepy"].code == "slow" and "decision_timeout_s" in skipped["sleepy"].fix
    assert skipped["no_import"].code == "import_error"
    assert skipped["no_such_policy"].code == "unknown_name"
    assert all(p in result.problems for p in result.skipped)


def test_a_policy_that_wins_on_score_but_loses_dollars_is_flagged(
    make_project: MakeProject,
) -> None:
    folder = make_project(
        {"score": "upside_down", "race": QUICK},
        files={"rewards/upside_down.py": UPSIDE_DOWN},
    )
    result = race(Project.open(folder), ["idle", "lowest_cover", "family_campaign"])
    ranked = result.ranking()
    assert [r.policy for r in ranked][0] == "idle"
    winner, last = ranked[0], ranked[-1]
    assert winner.dollars.mean < last.dollars.mean
    assert winner.dollar_warning is not None and last.policy in winner.dollar_warning
    assert last.dollar_warning is None  # last on score: nothing below it
    assert all(r.score.mean == pytest.approx(-r.dollars.mean) for r in ranked)


def test_the_result_is_json_and_writes_race_json(
    trio: tuple[Project, RaceResult], tmp_path: Path
) -> None:
    _, result = trio
    data = result.to_json()
    text = json.dumps(data, allow_nan=False)
    assert json.loads(text)["ranking"][0] == result.ranking()[0].policy
    written = result.write(tmp_path / "race")
    assert (tmp_path / "race" / "race.json") in written
    saved = json.loads((tmp_path / "race" / "race.json").read_text())
    assert saved == json.loads(text)
    assert saved["score"] == "dollars" and saved["settings"]["episodes"] == QUICK["episodes"]
    top = result.ranking()[0].policy
    moves = read_moves(tmp_path / "race" / "policies" / top)
    assert len(moves) == QUICK["episodes"] * 60


def test_race_settings_come_from_the_capsule_unless_given(make_project: MakeProject) -> None:
    project = Project.open(make_project({"race": QUICK}))
    result = race(project, ["idle"], settings=RaceSettings(episodes=2, seed=1, tune_episodes=1))
    assert result.settings.episodes == 2
    assert len(result.result("idle").episode_score) == 2


# --- floor facts and the baseline (T-269, REQ-228, REQ-229) ------------------------------------


def test_stock_and_lost_units_equal_a_hand_count(trio: tuple[Project, RaceResult]) -> None:
    _, result = trio
    for name in TRIO:
        moves = result.moves(name)
        row = result.result(name)
        stock = [sum(m.after.stock.values()) for m in moves]
        lost = sum(v for m in moves for k, v in m.events.items() if k.startswith("lost."))
        assert row.avg_stock == pytest.approx(float(np.mean(stock)))
        assert row.lost_units == pytest.approx(lost / QUICK["episodes"])
        saved = row.to_json()
        assert saved["avg_stock"] == row.avg_stock and saved["lost_units"] == row.lost_units


def test_the_baseline_keeps_its_moves_and_map_outside_the_top(
    shared_project: MakeProject,
) -> None:
    quick = {**QUICK, "top_policies": 1, "baseline": "idle"}
    project = Project.open(shared_project({"race": quick}))
    result = race(project, TRIO)
    top = result.ranking()[0].policy
    assert top != "idle"
    assert list(result.kept) == [top, "idle"]
    assert list(result.chains) == [top, "idle"]
    assert len(result.moves("idle")) == QUICK["episodes"] * 60


def test_a_baseline_in_the_top_adds_no_extra_policy(shared_project: MakeProject) -> None:
    quick = {**QUICK, "top_policies": 2}
    project = Project.open(shared_project({"race": quick}))
    result = race(project, TRIO)
    top = [r.policy for r in result.ranking()[:2]]
    assert project.baseline == "lowest_cover" and "lowest_cover" in top
    assert list(result.kept) == top and list(result.chains) == top
