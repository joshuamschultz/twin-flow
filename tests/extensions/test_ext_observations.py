"""T-266 (extension half): observation masks, `observe(state) -> state` (REQ-226).

The built-ins copy the RL repo's `factory_mdp.observations`: a hidden setup looks like the
first product on the press, bucketed stock shows a typical stock for its low/medium/high
level (1.5, 6 or 15 days, capped), a hidden day is day -1, and "own product" shows 8 days of
stock for every product but the one set up.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import pytest

from twinflow.engines.contract import run_episode
from twinflow.engines.step import rules
from twinflow.extensions.checker import ExtensionFailed
from twinflow.extensions.loader import load_project
from twinflow.extensions.project import Project
from twinflow.state.records import Snapshot

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "molding-press"
MakeProject = Callable[..., Path]
MASKS = ("full", "no_setup", "bucketed", "no_days_left", "own_product")


@pytest.fixture(scope="module")
def project() -> Project:
    return Project.open(EXAMPLE)


def _state(project: Project, stock: dict[str, float] | None = None, day: int = 10):  # type: ignore[no-untyped-def]
    start = project.engine().start(0)
    snapshot = Snapshot(
        t=float(day), period=day, stock=stock or dict(start.stock), setup={"press": "BLK-M"}
    )
    return project.policy_state(snapshot)


def test_builtin_observations_exist() -> None:
    assert set(load_project(None).names("observation")) >= set(MASKS)


def test_full_changes_nothing(project: Project) -> None:
    state = _state(project)
    assert project.observe(state, "full") == state
    assert project.config.observations == ("full",)


def test_no_setup_shows_the_first_product_on_the_press(project: Project) -> None:
    seen = project.observe(_state(project), "no_setup")
    assert seen.setup == "GRY-S"


def test_bucketed_shows_a_typical_stock_per_level(project: Project) -> None:
    stock = {p: 0.0 for p in project.model.product_ids}
    stock.update({"GRY-S": 100.0, "GRY-M": 200.0, "GRY-L": 400.0, "NAT-XL": 159.0})
    seen = project.observe(_state(project, stock), "bucketed")
    # GRY-S 100/60 = 1.7 days (low), GRY-M 5 days (medium), GRY-L 22 days (high)
    assert seen.stock["GRY-S"] == round(1.5 * 60)
    assert seen.stock["GRY-M"] == round(6.0 * 40)
    assert seen.stock["GRY-L"] == min(540, round(15.0 * 18))
    assert seen.stock["NAT-XL"] == min(160, round(15.0 * 4))
    assert seen.stock["BLK-S"] == round(1.5 * 45)  # empty is low
    assert seen.days_of_cover["GRY-M"] == 6.0  # cover follows the shown stock


def test_no_days_left_hides_the_day(project: Project) -> None:
    seen = project.observe(_state(project, day=55), "no_days_left")
    assert seen.period == -1 and seen.days_left == project.model.horizon + 1


def test_own_product_hides_other_stock(project: Project) -> None:
    stock = {p: 1.0 for p in project.model.product_ids}
    seen = project.observe(_state(project, stock), "own_product")
    assert seen.stock["BLK-M"] == 1.0  # the product on the press
    for product in project.model.products:
        if product.id != "BLK-M":
            assert seen.stock[product.id] == min(
                product.max_storage, round(8 * product.demand_rate)
            )


def test_masks_join_with_a_plus_in_order(project: Project) -> None:
    state = _state(project, day=55)
    both = project.observe(state, "bucketed+no_days_left")
    assert both == project.observe(project.observe(state, "bucketed"), "no_days_left")


@pytest.mark.parametrize("mask", MASKS[1:])
def test_a_masked_policy_equals_its_rule_on_the_masked_state(
    project: Project, sample_states: list[Snapshot], mask: str
) -> None:
    """A built-in policy under a mask decides as its RL rule does on the masked snapshot."""
    ext = project.extensions.get("policy", "family_campaign")
    for snapshot in sample_states[:100]:
        seen = project.observe(project.policy_state(snapshot), mask)
        masked = Snapshot(
            t=snapshot.t, period=seen.period, stock=dict(seen.stock), setup={"press": seen.setup}
        )
        assert ext(seen) == rules.choose("family_campaign", masked, project.model)


def test_experiment_observation_picks_masks(make_project: MakeProject) -> None:
    project = Project.open(
        make_project({"observation": ["full", "no_setup", "bucketed+no_days_left"]})
    )
    assert project.ok, project.problems
    assert project.config.observations == ("full", "no_setup", "bucketed+no_days_left")
    one = Project.open(make_project({"observation": "no_setup"}))
    assert one.config.observations == ("no_setup",)
    assert one.observe(_state(one)).setup == "GRY-S"  # the first chosen mask by default


def test_an_unknown_mask_is_a_problem(make_project: MakeProject) -> None:
    project = Project.open(make_project({"observation": ["full", "bucketed+no_day"]}))
    (problem,) = project.problems
    assert problem.field == "experiment.observation" and "no_days_left" in problem.fix


def test_the_decider_applies_the_mask(project: Project) -> None:
    engine = project.engine()
    policy = project.extensions.get("policy", "family_campaign")
    full = run_episode(engine, project.decider(policy, engine), 0)
    hidden = run_episode(engine, project.decider(policy, engine, observation="own_product"), 0)
    assert [m.action for m in full] != [m.action for m in hidden]
    tuned = project.decider(policy, engine, settings={"target_days": 20.0})
    assert [m.action for m in run_episode(engine, tuned, 0)] != [m.action for m in full]
    # the ledger is the real one: a mask changes decisions, never the dollars' meaning
    assert all(math.isfinite(m.ledger.total) for m in hidden)


def test_a_project_observation_file(make_project: MakeProject) -> None:
    text = "def observe(state):\n    return state.replace(stock={p: 0 for p in state.products})\n"
    folder = make_project({"observation": "blind"}, {"observations/blind.py": text})
    project = Project.open(folder)
    seen = project.observe(_state(project))
    assert set(seen.stock.values()) == {0} and seen.days_of_cover["GRY-S"] == 0.0


def test_an_observation_that_breaks_names_its_file(make_project: MakeProject) -> None:
    folder = make_project(
        {"observation": "broken"}, {"observations/broken.py": "def observe(state):\n    return 3\n"}
    )
    project = Project.open(folder)
    with pytest.raises(ExtensionFailed) as caught:
        project.observe(_state(project))
    assert caught.value.problem.code == "bad_value"
    assert caught.value.problem.file is not None and caught.value.problem.file.name == "broken.py"
