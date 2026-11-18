"""T-241: money leaks per policy and a Markov map for each top policy (COMP-148, COMP-157,
REQ-199, REQ-208).

Money leaks are the grouped states (on `experiment.markov.view`) where a policy loses the
most dollars per run to changeovers, holding and lost sales, with the cause split. The top
`experiment.race.top_policies` policies get a Markov chain on the same view.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import pytest

from twinflow.extensions.project import Project
from twinflow.extensions.race import LEAK_PARTS, RaceResult, race

MakeProject = Callable[..., Path]

EXPERIMENT = {
    "race": {"episodes": 5, "seed": 2, "tune_episodes": 2, "top_policies": 2},
    "markov": {"view": "simple", "top_k": 8},
}
POLICIES = ["lowest_cover", "family_campaign", "idle", "fixed_wheel"]


@pytest.fixture(scope="module")
def raced(shared_project: MakeProject) -> tuple[Project, RaceResult]:
    project = Project.open(shared_project(EXPERIMENT))
    return project, race(project, POLICIES)


def test_leaks_are_the_costliest_states_per_run_with_their_cause(
    raced: tuple[Project, RaceResult],
) -> None:
    project, result = raced
    top = result.ranking()[0]
    moves = result.moves(top.policy)
    view = project.view("simple")
    costs: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(LEAK_PARTS, 0.0))
    visits: dict[str, int] = defaultdict(int)
    for move in moves:
        label = view.label(move.before, project.facts.demand_rate, project.facts.family)
        visits[label] += 1
        for part in LEAK_PARTS:
            costs[label][part] += getattr(move.ledger, part)
    episodes = result.settings.episodes
    per_run = {s: sum(c.values()) / episodes for s, c in costs.items()}
    expected = sorted(per_run, key=lambda s: -per_run[s])[:5]
    leaks = top.leaks
    assert [leak.state for leak in leaks] == expected
    for leak in leaks:
        assert leak.per_run == pytest.approx(per_run[leak.state])
        assert leak.visits == visits[leak.state]
        assert leak.per_visit == pytest.approx(per_run[leak.state] * episodes / leak.visits)
        assert sum(leak.parts.values()) == pytest.approx(leak.per_run)
        assert leak.parts[leak.cause] == max(leak.parts.values())
        assert leak.share == pytest.approx(leak.visits / len(moves))
    assert [leak.per_run for leak in leaks] == sorted((x.per_run for x in leaks), reverse=True)


def test_every_raced_policy_has_its_leaks(raced: tuple[Project, RaceResult]) -> None:
    _, result = raced
    assert all(0 < len(r.leaks) <= 5 for r in result.ranking())


def test_markov_maps_for_the_top_policies_on_the_chosen_view(
    raced: tuple[Project, RaceResult],
) -> None:
    project, result = raced
    top = [r.policy for r in result.ranking()[:2]]
    assert list(result.chains) == top
    for name in top:
        chain = result.chains[name]
        again = project.markov(result.moves(name))
        assert chain.view == "simple" and len(chain.states) <= 8
        assert chain.states == again.states
        assert (chain.matrix == again.matrix).all()


def test_race_json_holds_leaks_and_maps(raced: tuple[Project, RaceResult], tmp_path: Path) -> None:
    _, result = raced
    result.write(tmp_path)
    saved = json.loads((tmp_path / "race.json").read_text())
    first = saved["results"][0]
    assert first["money_leaks"][0]["cause"] in LEAK_PARTS
    maps = saved["markov"]
    assert list(maps) == [r.policy for r in result.ranking()[:2]]
    one = next(iter(maps.values()))
    assert one["mermaid"].startswith("flowchart LR")
    assert one["view"] == "simple" and len(one["states"]) <= 8
