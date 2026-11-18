"""T-266 (race half): every policy is raced under every observation mask (REQ-226).

On the RL repo's notebook 2 seed (150 episodes of seed 11, settings tuned on 30 episodes of
seed 777) the profit-by-view table equals the notebook's numbers (`race_golden.json`, made
by `make_race_golden.py` in the RL repo).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from twinflow.extensions.project import Project
from twinflow.extensions.race import RaceResult

MASKS = ["full", "no_setup", "bucketed", "own_product", "no_days_left", "bucketed+no_days_left"]


def test_every_policy_runs_under_every_mask(golden_race: tuple[Project, RaceResult]) -> None:
    _, result = golden_race
    assert result.observations == tuple(MASKS)
    for mask in MASKS:
        assert {r.policy for r in result.ranking(mask)} == {
            "s_big_s",
            "family_campaign",
            "lowest_cover",
        }


def test_profit_by_view_equals_the_rl_notebook(
    golden_race: tuple[Project, RaceResult], race_golden: dict[str, Any]
) -> None:
    _, result = golden_race
    table = result.profit_by_view()
    expected = race_golden["observation_table"]["profit"]
    assert set(table) == set(expected)
    for policy, row in expected.items():
        for mask, profit in row.items():
            assert table[policy][mask] == pytest.approx(profit, abs=1e-6), (policy, mask)


def test_tuning_equals_the_rl_repo(
    golden_race: tuple[Project, RaceResult], race_golden: dict[str, Any]
) -> None:
    _, result = golden_race
    tuning = race_golden["tuning"]
    ours = {(t.policy, tuple(sorted(t.settings.items()))): t.score for t in result.trials}
    theirs = {
        (t["policy"], tuple(sorted(t["settings"].items()))): t["profit"] for t in tuning["trials"]
    }
    assert ours.keys() == theirs.keys()
    for key, profit in theirs.items():
        assert ours[key] == pytest.approx(profit, abs=1e-6), key
    for policy, best in tuning["best"].items():
        assert dict(result.result(policy).settings) == pytest.approx(best)


def test_the_table_is_in_race_json(golden_race: tuple[Project, RaceResult], tmp_path: Path) -> None:
    _, result = golden_race
    result.write(tmp_path)
    saved = json.loads((tmp_path / "race.json").read_text())
    assert saved["profit_by_view"] == {
        p: {m: v for m, v in row.items()} for p, row in result.profit_by_view().items()
    }
    assert saved["observations"] == MASKS


BLIND = '''
"""Breaks on purpose."""
NAME = "blind"


def observe(state):
    raise RuntimeError("cannot see")
'''


def test_a_broken_mask_skips_the_policy_and_says_why(make_project: Any) -> None:
    from twinflow.extensions.race import race

    folder = make_project(
        {"race": {"episodes": 2, "tune_episodes": 1}}, files={"observations/blind.py": BLIND}
    )
    result = race(Project.open(folder), ["idle"], observations=["full", "blind"])
    assert result.ranking() == []
    assert [(p.kind, p.code) for p in result.skipped] == [("observation", "raised")]
