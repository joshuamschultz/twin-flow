"""Tests for the scheduling profile and objective-spec helpers (T-089, COMP-004, COMP-005).

Requirements: REQ-067, REQ-068, REQ-069. Decision: D-068.
"""

from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from twinflow.scheduling.model import (
    EpsilonBound,
    EpsilonConstraint,
    Lexicographic,
    LexicographicLevel,
    ObjectiveKind,
    SchedulingProfile,
    WeightedSum,
    WeightedTerm,
)
from twinflow.scheduling.objectives import SENSE, primary_kind
from twinflow.scheduling.profile import default_profile, load_profile

SAMPLE_TOML = """
id = "plant-a"
time_budget_seconds = 30.0
robustness_load_targets = [0.95, 0.85]
default_portfolio = ["baseline", "cpsat"]

[class_weights]
1 = 1.0
2 = 3.0
4 = 9.0

[objective]
mode = "lexicographic"

[[objective.levels]]
kind = "weighted_tardiness"
tolerance = 0.05
tolerance_kind = "relative"

[[objective.levels]]
kind = "makespan"

[frozen_zone]
until = 3600.0
tolerance = 60.0
"""


# --- objective kinds and senses -----------------------------------------------------------


def test_objective_kind_has_exactly_thirteen_values() -> None:
    assert {kind.value for kind in ObjectiveKind} == {
        "makespan",
        "weighted_completion",
        "max_lateness",
        "tardy_count",
        "weighted_tardy_count",
        "total_tardiness",
        "weighted_tardiness",
        "earliness_tardiness",
        "total_setup",
        "cost",
        "churn",
        "robustness",
        "soft_penalty",
    }


def test_sense_covers_every_kind_robustness_max_rest_min() -> None:
    assert set(SENSE) == set(ObjectiveKind)
    assert SENSE[ObjectiveKind.ROBUSTNESS] == "max"
    assert all(
        SENSE[kind] == "min" for kind in ObjectiveKind if kind is not ObjectiveKind.ROBUSTNESS
    )


# --- objective specs validate -------------------------------------------------------------


def test_weighted_sum_rejects_negative_weight_and_non_positive_scale() -> None:
    with pytest.raises(ValidationError):
        WeightedTerm(kind=ObjectiveKind.MAKESPAN, weight=-1.0)
    with pytest.raises(ValidationError):
        WeightedTerm(kind=ObjectiveKind.MAKESPAN, weight=1.0, scale=0.0)


def test_lexicographic_rejects_negative_tolerance() -> None:
    with pytest.raises(ValidationError):
        LexicographicLevel(kind=ObjectiveKind.MAKESPAN, tolerance=-0.1)


def test_epsilon_bound_rejects_non_finite_upper() -> None:
    with pytest.raises(ValidationError):
        EpsilonBound(kind=ObjectiveKind.COST, upper=float("inf"))


# --- primary_kind ---------------------------------------------------------------------------


def test_primary_kind_is_first_lexicographic_level() -> None:
    spec = Lexicographic(
        levels=(
            LexicographicLevel(kind=ObjectiveKind.MAX_LATENESS),
            LexicographicLevel(kind=ObjectiveKind.MAKESPAN),
        )
    )
    assert primary_kind(spec) is ObjectiveKind.MAX_LATENESS


def test_primary_kind_is_single_or_first_weighted_term() -> None:
    single = WeightedSum(terms=(WeightedTerm(kind=ObjectiveKind.COST, weight=1.0),))
    several = WeightedSum(
        terms=(
            WeightedTerm(kind=ObjectiveKind.TOTAL_SETUP, weight=2.0),
            WeightedTerm(kind=ObjectiveKind.COST, weight=1.0),
        )
    )
    assert primary_kind(single) is ObjectiveKind.COST
    assert primary_kind(several) is ObjectiveKind.TOTAL_SETUP


def test_primary_kind_is_epsilon_primary() -> None:
    spec = EpsilonConstraint(
        primary=ObjectiveKind.WEIGHTED_TARDINESS,
        bounds=(EpsilonBound(kind=ObjectiveKind.MAKESPAN, upper=100.0),),
    )
    assert primary_kind(spec) is ObjectiveKind.WEIGHTED_TARDINESS


# --- default profile ------------------------------------------------------------------------


def test_default_profile_keeps_todays_behavior() -> None:
    profile = default_profile()
    assert profile.class_weights == {1: 1.0, 2: 2.0, 4: 4.0}
    assert profile.objective == Lexicographic(
        levels=(LexicographicLevel(kind=ObjectiveKind.MAKESPAN),)
    )
    assert profile.robustness_load_targets == (0.9, 0.8, 0.7)
    assert profile.default_portfolio == ("baseline",)
    assert profile.frozen_zone is None
    assert profile == SchedulingProfile()


@pytest.mark.parametrize("targets", [(1.1,), (0.0,), (0.7, 0.8)])
def test_profile_rejects_targets_outside_unit_interval_or_increasing(
    targets: tuple[float, ...],
) -> None:
    with pytest.raises(ValidationError):
        SchedulingProfile(robustness_load_targets=targets)


# --- load_profile ---------------------------------------------------------------------------


def test_load_profile_reads_toml_sample() -> None:
    profile = load_profile(SAMPLE_TOML)
    assert profile.id == "plant-a"
    assert profile.class_weights == {1: 1.0, 2: 3.0, 4: 9.0}
    assert profile.time_budget_seconds == 30.0
    assert profile.robustness_load_targets == (0.95, 0.85)
    assert profile.default_portfolio == ("baseline", "cpsat")
    assert profile.objective == Lexicographic(
        levels=(
            LexicographicLevel(
                kind=ObjectiveKind.WEIGHTED_TARDINESS, tolerance=0.05, tolerance_kind="relative"
            ),
            LexicographicLevel(kind=ObjectiveKind.MAKESPAN),
        )
    )
    assert profile.frozen_zone is not None
    assert profile.frozen_zone.until == 3600.0
    assert profile.frozen_zone.tolerance == 60.0


def test_load_profile_round_trips_through_its_own_dump() -> None:
    profile = load_profile(SAMPLE_TOML)
    assert SchedulingProfile.model_validate(profile.model_dump()) == profile


def test_load_profile_of_empty_text_is_default() -> None:
    assert load_profile("") == default_profile()


def test_load_profile_rejects_unknown_top_level_key() -> None:
    with pytest.raises(ValidationError):
        load_profile('colour = "blue"\n')


def test_load_profile_rejects_unknown_nested_key() -> None:
    with pytest.raises(ValidationError):
        load_profile('[objective]\nmode = "lexicographic"\nlevels = [{kind = "makespan", x = 1}]\n')


def test_load_profile_rejects_missing_weight_class() -> None:
    with pytest.raises(ValidationError):
        load_profile("[class_weights]\n1 = 1.0\n2 = 2.0\n")


def test_load_profile_rejects_malformed_toml() -> None:
    with pytest.raises(tomllib.TOMLDecodeError):
        load_profile("id = \n")
