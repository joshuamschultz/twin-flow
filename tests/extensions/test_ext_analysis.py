"""T-267: the analysis API behind the two sample notebooks (REQ-227).

Every function is checked on the golden seeds against the RL repo's `analysis.py`
(`race_golden.json`, written by `make_race_golden.py` in the RL repo): the tuning trials,
cumulative profit with bands, rank by day, first day passed, the reward parts, the horizon
table, the aliasing pair finder, next-state entropy, the Markov chains behind the heatmap
and network figures, and the inventory grid.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl
import pytest

from twinflow.extensions import analysis
from twinflow.extensions.project import Project, RaceSettings
from twinflow.extensions.race import RaceResult, race

Golden = dict[str, Any]


def _typed(project: Project, policy: str, settings: dict[str, float]) -> dict[str, Any]:
    """Golden settings with the grid's own value types (a whole number stays an int)."""
    grid = project.extensions.get("policy", policy).grid
    out: dict[str, Any] = {}
    for key, value in settings.items():
        same = [v for v in grid.get(key, ()) if v == value]
        out[key] = same[0] if same else value
    return out


@pytest.fixture(scope="module")
def project(shared_project: Any) -> Project:
    return Project.open(shared_project())


@pytest.fixture(scope="module")
def tuned(project: Project, race_golden: Golden) -> dict[str, dict[str, Any]]:
    best = race_golden["tuning"]["all_best"]
    return {name: _typed(project, name, s) for name, s in best.items()}


@pytest.fixture(scope="module")
def notebook1(project: Project, race_golden: Golden, tuned: dict[str, Any]) -> RaceResult:
    gold = race_golden["notebook1"]
    settings = RaceSettings(episodes=gold["episodes"], seed=gold["seed"])
    fixed = {name: tuned.get(name, {}) for name in gold["policies"]}
    return race(project, gold["policies"], settings=settings, fixed=fixed)


@pytest.fixture(scope="module")
def ss_episodes(project: Project, race_golden: Golden, tuned: dict[str, Any]) -> list[Any]:
    gold = race_golden["aliasing"]
    return analysis.run_episodes(
        project, "s_big_s", gold["episodes"], gold["seed"], settings=tuned["s_big_s"]
    )


@pytest.fixture(scope="module")
def campaign_episodes(project: Project, race_golden: Golden, tuned: dict[str, Any]) -> list[Any]:
    gold = race_golden["markov"]
    return analysis.run_episodes(
        project,
        "family_campaign",
        gold["episodes"],
        gold["seed"],
        settings=tuned["family_campaign"],
    )


# --- tuning ------------------------------------------------------------------------------------


def test_tuning_trials_table(golden_race: tuple[Project, RaceResult], race_golden: Golden) -> None:
    _, result = golden_race
    table = analysis.tuning_trials(result)
    assert table.columns == ["policy", "settings", "score", "dollars", "best", "broke"]
    gold = race_golden["tuning"]["trials"]
    assert table.height == len(gold)
    ours = [r for r in table.iter_rows(named=True)]
    for policy in ("s_big_s", "family_campaign", "lowest_cover"):  # each in grid order
        mine = [r["dollars"] for r in ours if r["policy"] == policy]
        theirs = [t["profit"] for t in gold if t["policy"] == policy]
        assert mine == pytest.approx(theirs, abs=1e-6)
        assert sum(r["best"] for r in ours if r["policy"] == policy) == 1
    summary = analysis.tuning_summary(result)
    assert summary.columns == ["policy", "settings tried", "best setting", "best", "worst"]
    by_policy = {r["policy"]: r for r in summary.iter_rows(named=True)}
    for policy in ("s_big_s", "family_campaign", "lowest_cover"):
        profits = [t["profit"] for t in gold if t["policy"] == policy]
        assert by_policy[policy]["settings tried"] == len(profits)
        assert by_policy[policy]["best"] == pytest.approx(max(profits))
        assert by_policy[policy]["worst"] == pytest.approx(min(profits))


# --- notebook 1: long-term thinking ------------------------------------------------------------


def test_cumulative_profit_mean_and_bands(notebook1: RaceResult, race_golden: Golden) -> None:
    gold = race_golden["notebook1"]
    table = analysis.cumulative_profit(notebook1)
    assert table.columns == ["day", "policy", "mean", "std", "low", "high"]
    for policy in gold["policies"]:
        rows = table.filter(pl.col("policy") == policy).sort("day")
        assert rows["day"].to_list() == list(range(1, 61))
        assert np.allclose(rows["mean"].to_numpy(), gold["mean_cumulative"][policy], atol=1e-6)
        assert np.allclose(rows["std"].to_numpy(), gold["std_cumulative"][policy], atol=1e-6)
        assert np.allclose(rows["low"].to_numpy(), rows["mean"].to_numpy() - rows["std"].to_numpy())


def test_rank_by_day_and_first_day_passed(notebook1: RaceResult, race_golden: Golden) -> None:
    gold = race_golden["notebook1"]
    ranks = analysis.rank_by_day(notebook1)
    assert ranks.columns[0] == "day" and set(ranks.columns[1:]) == set(gold["policies"])
    for policy in gold["policies"]:
        assert ranks[policy].to_list() == gold["rank_by_day"][policy]
    for case in gold["first_day_passed"]:
        day = analysis.first_day_passed(notebook1, case["leader"], case["challenger"])
        assert day == case["day"], case


def test_reward_parts_per_run(notebook1: RaceResult, race_golden: Golden) -> None:
    gold = race_golden["notebook1"]["parts_per_episode"]
    parts = analysis.parts_per_run(notebook1)
    renamed = {"setup": "changeover"}
    for row in parts.iter_rows(named=True):
        want = gold[row["policy"]]
        for part in ("revenue", "production", "setup", "holding", "lost_sales", "total"):
            assert row[part] == pytest.approx(want[renamed.get(part, part)], abs=1e-6), part


def test_horizon_table(project: Project, race_golden: Golden, tuned: dict[str, Any]) -> None:
    gold = race_golden["horizon_table"]
    policies = list(gold["profit"])
    table = analysis.horizon_table(
        project,
        policies,
        gold["horizons"],
        episodes=gold["episodes"],
        seed=gold["seed"],
        settings=tuned,
    )
    assert table.columns == ["policy", *(f"{h} days" for h in gold["horizons"])]
    for row in table.iter_rows(named=True):
        for horizon in gold["horizons"]:
            want = gold["profit"][row["policy"]][str(horizon)]
            assert row[f"{horizon} days"] == pytest.approx(want, abs=1e-6), (row["policy"], horizon)


# --- notebook 2: what the state must hold ------------------------------------------------------


def test_aliasing_pair_finder(
    project: Project, ss_episodes: list[Any], race_golden: Golden, tuned: dict[str, Any]
) -> None:
    gold = race_golden["aliasing"]
    budget = gold["budget"]
    pair = analysis.find_aliased_pair(
        project,
        ss_episodes,
        "s_big_s",
        settings=tuned["s_big_s"],
        view="bucketed",
        buckets=budget["n_buckets"],
        per_bucket=budget["per_bucket"],
        lookahead=budget["lookahead"],
        draws=budget["n_draws"],
        seed=budget["seed"],
    )
    assert pair is not None
    assert pair.bucket == gold["bucket"]
    for state, want in zip(pair.states, gold["states"], strict=True):
        assert state.period == want["day"]
        assert dict(state.stock) == {k: float(v) for k, v in want["stock"].items()}
        assert next(iter(state.setup.values())) == want["setup"]
    assert list(pair.best) == gold["best"]
    for values, want in zip(pair.values, gold["values"], strict=True):
        assert values == pytest.approx(want, abs=1e-6)
    assert pair.regret == pytest.approx(gold["regret"], abs=1e-6)
    table = analysis.aliasing_table(project, pair)
    assert table.columns == ["", "A", "B"]
    assert "best action" in table[""].to_list()


def test_next_state_entropy(project: Project, ss_episodes: list[Any], race_golden: Golden) -> None:
    gold = race_golden["entropy"]
    budget = gold["budget"]
    table = analysis.next_state_entropy(
        project,
        ss_episodes,
        view="family",
        min_rows=budget["min_rows"],
        max_groups=budget["max_groups"],
        states_per_group=budget["states_per_group"],
        draws=budget["n_draws"],
        seed=budget["seed"],
    )
    assert table.columns == ["label", "action", "days", "grouped view", "full view"]
    ours = {(r["label"], r["action"]): r for r in table.iter_rows(named=True)}
    assert set(ours) == {(r["label"], r["action"]) for r in gold["rows"]}
    for want in gold["rows"]:
        row = ours[(want["label"], want["action"])]
        assert row["days"] == want["days"]
        assert row["grouped view"] == pytest.approx(want["bucketed"], abs=1e-9)
        assert row["full view"] == pytest.approx(want["full"], abs=1e-9)
    assert table["days"].to_list() == sorted(table["days"].to_list(), reverse=True)


def test_markov_chains_and_figures(
    project: Project, campaign_episodes: list[Any], race_golden: Golden
) -> None:
    gold = race_golden["markov"]
    moves = [m for episode in campaign_episodes for m in episode]
    family = project.markov(moves, "family", top_k=15)
    assert list(family.states) == gold["family"]["labels"]
    assert np.allclose(family.matrix, gold["family"]["matrix"], atol=1e-12)
    assert family.coverage == pytest.approx(gold["family"]["coverage"])
    full = project.markov(moves, "full", top_k=15)
    assert len(full.share) == gold["full"]["distinct"]
    assert full.coverage == pytest.approx(gold["full"]["coverage"])
    chains = {"family": family, "full": full}
    heat = analysis.markov_heatmap(chains)
    assert len(heat.data) == 2 and heat.data[0].type == "heatmap"
    assert np.allclose(np.array(heat.data[0].z), family.matrix)
    network = analysis.markov_network({"family": family})
    nodes = [t for t in network.data if t.mode and "markers" in t.mode]
    assert len(nodes) == 1 and len(nodes[0].x) == len(family.states)
    assert len(network.layout.annotations) == len(family.edges)


def test_inventory_grid(
    project: Project, campaign_episodes: list[Any], race_golden: Golden
) -> None:
    gold = race_golden["inventory"]
    figure = analysis.inventory_grid(project, campaign_episodes[0])
    cover, levels = figure.data[0], figure.data[1]
    assert np.allclose(np.array(cover.z), gold["cover"], atol=1e-12)
    assert np.array(levels.z).tolist() == gold["levels"]
    assert list(cover.y) == list(project.facts.products)
    changes = [i for i in range(1, 60) if gold["setups"][i] != gold["setups"][i - 1]]
    assert len(figure.layout.shapes) == 2 * len(changes)


def test_profit_by_view_table(golden_race: tuple[Project, RaceResult], race_golden: Golden) -> None:
    _, result = golden_race
    table = analysis.profit_by_view(result)
    gold = race_golden["observation_table"]["profit"]
    assert table.columns[0] == "observation"
    for row in table.iter_rows(named=True):
        for policy, by_mask in gold.items():
            assert row[policy] == pytest.approx(by_mask[row["observation"]], abs=1e-6)
    loss = analysis.profit_by_view(result, loss=True)
    assert loss.filter(pl.col("observation") == "full")["s_big_s"].item() == 0.0


def _colours(value: Any) -> set[str]:
    """Every `#rrggbb` colour written anywhere in a figure."""
    import json
    import re

    return {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", json.dumps(value, default=str))}


def test_figures_draw_with_token_colours_only(
    project: Project, campaign_episodes: list[Any]
) -> None:
    """B-38: the analysis figures use the design tokens, not Viridis, teal or plotly_white."""
    from twinflow.report import theme

    allowed = {
        v.lower()
        for mode in theme.MODES
        for v in theme.tokens()[mode].values()
        if isinstance(v, str) and v.startswith("#") and len(v) == 7
    }
    moves = [m for episode in campaign_episodes for m in episode]
    family = project.markov(moves, "family", top_k=15)
    figures = [
        analysis.markov_heatmap({"family": family}),
        analysis.markov_network({"family": family}),
        analysis.inventory_grid(project, campaign_episodes[0]),
    ]
    for figure in figures:
        assert _colours(figure.to_plotly_json()) <= allowed
        assert tuple(figure.layout.template.layout.colorway) == theme.palette("light").series
