"""TF-004 T-170: simulation policy and distribution types (COMP-131, Data Model, D-092, D-109).

API fixed by these tests (implementer conforms):

    from twinflow.execution.policy import (
        Deterministic, LogNormal, Triangular, Uniform, ShiftedGamma, Empirical,
        Distribution, FactorRule, BreakdownRule, ScriptedOutage, SimulationPolicy,
        MAX_RULES,             # == 1_000 (each rule / outage list)
        MAX_EMPIRICAL_SAMPLES  # == 10_000
    )

    Every model is a frozen Pydantic v2 model with extra="forbid" and allow_inf_nan=False
    (the TF-002 `_Frozen` contract). Construction errors are `pydantic.ValidationError`.

    Distribution = Annotated[Deterministic | LogNormal | Triangular | Uniform | ShiftedGamma
                             | Empirical, Field(discriminator="kind")]
        Deterministic{kind="deterministic", value: float > 0 = 1.0}
        LogNormal{kind="lognormal", mean: float > 0 = 1.0, cv: 0 <= float <= 3}
        Triangular{kind="triangular", low >= 0, mode, high; low <= mode <= high, low < high}
        Uniform{kind="uniform", low >= 0, high > low}
        ShiftedGamma{kind="shifted_gamma", minimum >= 0, mean > minimum, 0.5 <= shape <= 20}
        Empirical{kind="empirical", samples: tuple[float > 0, ...] (1..10_000)}

    FactorRule{work_center_id: Id | None = None, machine_id: Id | None = None,
               family: Id | None = None, scope: "lot" | "unit" = "lot",
               distribution: Distribution}
    BreakdownRule{work_center_id: Id | None = None, machine_id: Id | None = None,
                  mtbf_seconds: float >= 60, repair: Distribution}
    ScriptedOutage{machine_id: Id, start: float, duration: float > 0,
                   probability: 0 < float <= 1 = 1.0}

    SimulationPolicy: every field and bound in SDD Data Model. Cross-field rules:
        replications <= max_replications;
        robustness_delta_probabilities (when given) has the same length as
        robustness_deltas_seconds, each in [0, 1], sum <= 1;
        any non-empty run_factors / setup_factors / breakdowns / outages under
        distribution_source="deterministic" is rejected with a message containing
        "simulation_rules_unused" (outages count: the deterministic source has none, so
        they would be silently ignored too).
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from twinflow.execution.policy import (
    MAX_EMPIRICAL_SAMPLES,
    MAX_RULES,
    BreakdownRule,
    Deterministic,
    Distribution,
    Empirical,
    FactorRule,
    LogNormal,
    ScriptedOutage,
    ShiftedGamma,
    SimulationPolicy,
    Triangular,
    Uniform,
)
from twinflow.policy import DISPATCH_NAMES

DIST = TypeAdapter(Distribution)
NON_FINITE = (math.nan, math.inf, -math.inf)


def _rule(**overrides: Any) -> FactorRule:
    return FactorRule(distribution=LogNormal(mean=1.0, cv=0.15), **overrides)


# --- Distribution families ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"kind": "deterministic"}, Deterministic),
        ({"kind": "lognormal", "cv": 0.2}, LogNormal),
        ({"kind": "triangular", "low": 0.9, "mode": 1.0, "high": 1.3}, Triangular),
        ({"kind": "uniform", "low": 0.9, "high": 1.1}, Uniform),
        ({"kind": "shifted_gamma", "minimum": 0.9, "mean": 1.05, "shape": 2}, ShiftedGamma),
        ({"kind": "empirical", "samples": [1.0, 1.2]}, Empirical),
    ],
)
def test_distribution_union_dispatches_on_kind(
    payload: dict[str, Any], expected_type: type
) -> None:
    assert isinstance(DIST.validate_python(payload), expected_type)


def test_distribution_defaults() -> None:
    assert Deterministic().value == 1.0
    assert LogNormal(cv=0.2).mean == 1.0


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "normal", "mean": 1.0},
        {"mean": 1.0, "cv": 0.1},
        {"kind": "lognormal", "mean": 1.0, "cv": 0.1, "sigma": 0.1},
    ],
)
def test_distribution_rejects_unknown_kind_missing_kind_and_extra_fields(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        DIST.validate_python(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "deterministic", "value": 0.0},
        {"kind": "deterministic", "value": -1.0},
        {"kind": "lognormal", "mean": 1.0, "cv": 3.01},
        {"kind": "lognormal", "mean": 1.0, "cv": -0.01},
        {"kind": "lognormal", "mean": 0.0, "cv": 0.1},
        {"kind": "triangular", "low": 1.1, "mode": 1.0, "high": 1.3},
        {"kind": "triangular", "low": 0.9, "mode": 1.4, "high": 1.3},
        {"kind": "triangular", "low": 1.0, "mode": 1.0, "high": 1.0},
        {"kind": "triangular", "low": -0.1, "mode": 1.0, "high": 1.3},
        {"kind": "uniform", "low": -0.1, "high": 1.1},
        {"kind": "uniform", "low": 1.0, "high": 1.0},
        {"kind": "uniform", "low": 1.2, "high": 1.1},
        {"kind": "shifted_gamma", "minimum": 0.9, "mean": 1.05, "shape": 0.4},
        {"kind": "shifted_gamma", "minimum": 0.9, "mean": 1.05, "shape": 20.01},
        {"kind": "shifted_gamma", "minimum": 0.9, "mean": 0.9, "shape": 2},
        {"kind": "shifted_gamma", "minimum": -0.1, "mean": 1.05, "shape": 2},
        {"kind": "empirical", "samples": []},
        {"kind": "empirical", "samples": [1.0] * (10_001)},
        {"kind": "empirical", "samples": [1.0, 0.0]},
    ],
)
def test_distribution_bounds_are_rejected(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DIST.validate_python(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "lognormal", "mean": 1.0, "cv": 3.0},
        {"kind": "lognormal", "mean": 1.0, "cv": 0.0},
        {"kind": "triangular", "low": 1.0, "mode": 1.0, "high": 1.3},
        {"kind": "triangular", "low": 0.0, "mode": 1.3, "high": 1.3},
        {"kind": "shifted_gamma", "minimum": 0.0, "mean": 1.05, "shape": 0.5},
        {"kind": "shifted_gamma", "minimum": 0.9, "mean": 1.05, "shape": 20},
        {"kind": "empirical", "samples": [1.0]},
        {"kind": "empirical", "samples": [1.0] * 10_000},
    ],
)
def test_distribution_bounds_are_inclusive_where_the_sdd_says(payload: dict[str, Any]) -> None:
    DIST.validate_python(payload)


@pytest.mark.parametrize("bad", NON_FINITE)
@pytest.mark.parametrize(
    ("kind", "field", "base"),
    [
        ("deterministic", "value", {}),
        ("lognormal", "mean", {"cv": 0.1}),
        ("lognormal", "cv", {"mean": 1.0}),
        ("triangular", "high", {"low": 0.9, "mode": 1.0}),
        ("uniform", "high", {"low": 0.9}),
        ("shifted_gamma", "mean", {"minimum": 0.9, "shape": 2}),
    ],
)
def test_distribution_rejects_non_finite_parameters(
    kind: str, field: str, base: dict[str, Any], bad: float
) -> None:
    with pytest.raises(ValidationError):
        DIST.validate_python({"kind": kind, field: bad, **base})


@pytest.mark.parametrize("bad", NON_FINITE)
def test_empirical_rejects_a_non_finite_sample(bad: float) -> None:
    with pytest.raises(ValidationError):
        Empirical(samples=(1.0, bad))


def test_empirical_cap_constant() -> None:
    assert MAX_EMPIRICAL_SAMPLES == 10_000
    assert MAX_RULES == 1_000


def test_distributions_are_frozen() -> None:
    dist = LogNormal(mean=1.0, cv=0.2)
    with pytest.raises(ValidationError):
        dist.cv = 0.5  # frozen model: assignment must raise


# --- Rule models -------------------------------------------------------------------------


def test_factor_rule_defaults_and_matching_fields() -> None:
    rule = _rule()
    assert (rule.work_center_id, rule.machine_id, rule.family, rule.scope) == (
        None,
        None,
        None,
        "lot",
    )
    assert _rule(scope="unit").scope == "unit"


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope": "batch"},
        {"work_center_id": ""},
        {"machine_id": "M" * 201},
        {"family": ""},
        {"colour": "red"},
    ],
)
def test_factor_rule_rejects_bad_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _rule(**overrides)


def test_factor_rule_requires_a_distribution() -> None:
    with pytest.raises(ValidationError):
        FactorRule.model_validate({"work_center_id": "WC1"})


def test_factor_rule_parses_a_toml_style_distribution_dict() -> None:
    rule = FactorRule.model_validate(
        {
            "work_center_id": "GRIND",
            "scope": "unit",
            "distribution": {"kind": "shifted_gamma", "minimum": 0.9, "mean": 1.05, "shape": 2},
        }
    )
    assert rule.distribution == ShiftedGamma(minimum=0.9, mean=1.05, shape=2)


def test_breakdown_rule_mtbf_floor_is_60_seconds() -> None:
    repair = LogNormal(mean=3600, cv=0.5)
    assert BreakdownRule(work_center_id="COIL", mtbf_seconds=60, repair=repair).mtbf_seconds == 60
    with pytest.raises(ValidationError):
        BreakdownRule(work_center_id="COIL", mtbf_seconds=59.9, repair=repair)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_breakdown_rule_rejects_non_finite_mtbf(bad: float) -> None:
    with pytest.raises(ValidationError):
        BreakdownRule(machine_id="M1", mtbf_seconds=bad, repair=Deterministic(value=600))


def test_scripted_outage_defaults_to_certain() -> None:
    assert ScriptedOutage(machine_id="M1", start=0.0, duration=600).probability == 1.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"probability": 0.0},
        {"probability": 1.01},
        {"duration": 0.0},
        {"machine_id": ""},
        {"start": math.inf},
        {"start": math.nan},
        {"duration": math.inf},
    ],
)
def test_scripted_outage_rejects_bad_fields(overrides: dict[str, Any]) -> None:
    fields: dict[str, Any] = {"machine_id": "M1", "start": 0.0, "duration": 600.0}
    with pytest.raises(ValidationError):
        ScriptedOutage(**{**fields, **overrides})


# --- SimulationPolicy --------------------------------------------------------------------


def test_simulation_policy_defaults_match_the_sdd() -> None:
    policy = SimulationPolicy()
    assert policy.replications == 20
    assert policy.max_replications == 100
    assert policy.time_budget_seconds == 60
    assert policy.confidence_level == 0.90
    assert policy.workers == 1
    assert policy.baseline_rule == "edd"
    assert policy.distribution_source == "deterministic"
    assert policy.run_factors == ()
    assert policy.setup_factors == ()
    assert policy.breakdowns == ()
    assert policy.outages == ()
    assert policy.robustness_deltas_seconds == (1800, 3600, 7200, 14400, 28800)
    assert policy.robustness_delta_probabilities is None
    assert policy.robustness_probe_machines == 1
    assert policy.robustness_objective == "weighted_tardiness"
    assert policy.max_candidates == 12


def test_simulation_policy_is_frozen_and_closed() -> None:
    policy = SimulationPolicy()
    with pytest.raises(ValidationError):
        policy.replications = 5  # frozen model: assignment must raise
    with pytest.raises(ValidationError):
        SimulationPolicy.model_validate({"seed": 1})


@pytest.mark.parametrize(
    "overrides",
    [
        {"replications": 0},
        {"replications": 101},
        {"max_replications": 0},
        {"max_replications": 1001},
        {"time_budget_seconds": 0},
        {"time_budget_seconds": 300.01},
        {"time_budget_seconds": -1},
        {"confidence_level": 0.49},
        {"confidence_level": 0.9991},
        {"workers": 0},
        {"workers": 17},
        {"baseline_rule": "lifo"},
        {"distribution_source": "capsule"},
        {"distribution_source": "history"},
        {"robustness_deltas_seconds": ()},
        {"robustness_deltas_seconds": tuple(float(i + 1) for i in range(11))},
        {"robustness_deltas_seconds": (1800, 0)},
        {"robustness_deltas_seconds": (1800, -60)},
        {"robustness_probe_machines": 0},
        {"robustness_probe_machines": 6},
        {"robustness_objective": "profit"},
        {"max_candidates": 0},
        {"max_candidates": 51},
    ],
)
def test_simulation_policy_rejects_out_of_bound_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SimulationPolicy(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"replications": 1},
        {"replications": 100},
        {"replications": 150, "max_replications": 200},
        {"replications": 1000, "max_replications": 1000},
        {"time_budget_seconds": 300},
        {"time_budget_seconds": 0.001},
        {"confidence_level": 0.5},
        {"confidence_level": 0.999},
        {"workers": 16},
        {"robustness_deltas_seconds": tuple(float(i + 1) for i in range(10))},
        {"robustness_deltas_seconds": (60.0,)},
        {"robustness_probe_machines": 5},
        {"robustness_objective": "makespan"},
        {"max_candidates": 50},
    ],
)
def test_simulation_policy_accepts_bound_values(overrides: dict[str, Any]) -> None:
    SimulationPolicy(**overrides)


@pytest.mark.parametrize("rule", DISPATCH_NAMES)
def test_simulation_policy_accepts_every_dispatch_rule_as_baseline(rule: str) -> None:
    assert SimulationPolicy(baseline_rule=rule).baseline_rule == rule


def test_replications_above_max_replications_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SimulationPolicy(replications=51, max_replications=50)


@pytest.mark.parametrize("bad", NON_FINITE)
@pytest.mark.parametrize("field", ["time_budget_seconds", "confidence_level"])
def test_simulation_policy_rejects_non_finite_scalars(field: str, bad: float) -> None:
    with pytest.raises(ValidationError):
        SimulationPolicy(**{field: bad})


@pytest.mark.parametrize("bad", NON_FINITE)
def test_simulation_policy_rejects_non_finite_delta(bad: float) -> None:
    with pytest.raises(ValidationError):
        SimulationPolicy(robustness_deltas_seconds=(1800, bad))


def test_delta_probabilities_same_length_and_sum_at_most_one() -> None:
    accepted = SimulationPolicy(robustness_delta_probabilities=(0.5, 0.25, 0.125, 0.125, 0.0))
    assert accepted.robustness_delta_probabilities == (0.5, 0.25, 0.125, 0.125, 0.0)
    SimulationPolicy(robustness_delta_probabilities=(0.1, 0.1, 0.1, 0.1, 0.1))


@pytest.mark.parametrize(
    "probabilities",
    [
        (0.2, 0.2, 0.2, 0.2),
        (0.1, 0.1, 0.1, 0.1, 0.1, 0.1),
        (1.1, 0.0, 0.0, 0.0, 0.0),
        (-0.1, 0.1, 0.1, 0.1, 0.1),
        (0.5, 0.5, 0.2, 0.0, 0.0),
        (0.1, 0.1, math.nan, 0.1, 0.1),
    ],
)
def test_delta_probabilities_rejected(probabilities: tuple[float, ...]) -> None:
    with pytest.raises(ValidationError):
        SimulationPolicy(robustness_delta_probabilities=probabilities)


def _outage() -> ScriptedOutage:
    return ScriptedOutage(machine_id="M1", start=3600.0, duration=1800.0)


def _breakdown() -> BreakdownRule:
    return BreakdownRule(
        work_center_id="COIL", mtbf_seconds=144000, repair=LogNormal(mean=3600, cv=0.5)
    )


@pytest.mark.parametrize(
    "field",
    ["run_factors", "setup_factors", "breakdowns", "outages"],
)
def test_rules_under_deterministic_source_are_rejected_as_unused(field: str) -> None:
    value = {
        "run_factors": (_rule(),),
        "setup_factors": (_rule(),),
        "breakdowns": (_breakdown(),),
        "outages": (_outage(),),
    }[field]
    with pytest.raises(ValidationError, match="simulation_rules_unused"):
        SimulationPolicy(**{field: value})
    with pytest.raises(ValidationError, match="simulation_rules_unused"):
        SimulationPolicy(distribution_source="deterministic", **{field: value})
    assert getattr(SimulationPolicy(distribution_source="profile", **{field: value}), field)


@pytest.mark.parametrize("field", ["run_factors", "setup_factors"])
def test_factor_rule_lists_are_capped_at_1000(field: str) -> None:
    SimulationPolicy(distribution_source="profile", **{field: (_rule(),) * MAX_RULES})
    with pytest.raises(ValidationError):
        SimulationPolicy(distribution_source="profile", **{field: (_rule(),) * (MAX_RULES + 1)})


def test_breakdown_and_outage_lists_are_capped_at_1000() -> None:
    SimulationPolicy(distribution_source="profile", breakdowns=(_breakdown(),) * MAX_RULES)
    SimulationPolicy(distribution_source="profile", outages=(_outage(),) * MAX_RULES)
    with pytest.raises(ValidationError):
        SimulationPolicy(
            distribution_source="profile", breakdowns=(_breakdown(),) * (MAX_RULES + 1)
        )
    with pytest.raises(ValidationError):
        SimulationPolicy(distribution_source="profile", outages=(_outage(),) * (MAX_RULES + 1))


def test_simulation_policy_parses_the_sdd_fuller_example() -> None:
    policy = SimulationPolicy.model_validate(
        {
            "replications": 20,
            "distribution_source": "profile",
            "baseline_rule": "edd",
            "run_factors": [
                {"distribution": {"kind": "lognormal", "mean": 1.0, "cv": 0.15}},
                {
                    "work_center_id": "GRIND",
                    "scope": "unit",
                    "distribution": {
                        "kind": "shifted_gamma",
                        "minimum": 0.9,
                        "mean": 1.05,
                        "shape": 2,
                    },
                },
            ],
            "setup_factors": [{"distribution": {"kind": "lognormal", "mean": 1.1, "cv": 0.3}}],
            "breakdowns": [
                {
                    "work_center_id": "COIL",
                    "mtbf_seconds": 144000,
                    "repair": {"kind": "lognormal", "mean": 3600, "cv": 0.5},
                }
            ],
        }
    )
    assert isinstance(policy.run_factors, tuple)
    assert policy.run_factors[1].distribution == ShiftedGamma(minimum=0.9, mean=1.05, shape=2)
    assert policy.setup_factors[0].distribution == LogNormal(mean=1.1, cv=0.3)
    assert policy.breakdowns[0].repair == LogNormal(mean=3600, cv=0.5)
