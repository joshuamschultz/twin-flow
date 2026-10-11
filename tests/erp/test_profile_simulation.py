"""The `[simulation]` table of the planning profile (T-185, COMP-133).

Requirements: REQ-139 (signed `[simulation]` table), REQ-143 (bounded, named refusals).
Decisions: D-104 (the simulation policy is the `[simulation]` table of the signed profile),
D-109 (bounded lists), TF-005 D-126 (signed, fail closed).

Contract under test:

* `PlanningProfile.simulation: SimulationPolicy = SimulationPolicy()`.
* `load_planning_profile` pops `[simulation]` beside `[erp]` and validates it into
  `SimulationPolicy`; every failure is one `ProfileError` whose message lists
  `<dotted path>: <pydantic error type>` entries. List indexes render as `[i]` and the
  discriminator tag of a distribution is not part of the path (SDD COMP-133:
  `simulation.run_factors[2].distribution.cv`). Values are never echoed.
* `profile_digest` covers the table.
* `twinflow.execution.policy.policy_digest(policy)` is the lowercase SHA-256 hex of the
  policy's canonical JSON (`model_dump(mode="json")`, sorted keys, compact separators, the
  same canonical form as `profile_digest`), recorded on every simulation.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pytest

import twinflow.execution.policy as policy_module
from twinflow.erp.profile import (
    PlanningProfile,
    ProfileError,
    load_planning_profile,
    profile_digest,
)
from twinflow.execution.policy import (
    BreakdownRule,
    FactorRule,
    LogNormal,
    ShiftedGamma,
    SimulationPolicy,
)
from twinflow.scheduling.model import ObjectiveKind
from twinflow.scheduling.profile import load_profile

SCHEDULING_TOML = """
id = "plant-a"
time_budget_seconds = 30.0
"""

ERP_TOML = """
[erp]
horizon_days = 28
"""

# SDD Data Model, "Seed calibration (closed 2026-10-10)", verbatim.
SEED_SIMULATION_TOML = """
[simulation]
distribution_source = "profile"   # assumption: seed calibration, replace with shop history
baseline_rule = "edd"

[[simulation.run_factors]]
distribution = { kind = "lognormal", mean = 1.0, cv = 0.15 }   # assumption

[[simulation.setup_factors]]
distribution = { kind = "lognormal", mean = 1.1, cv = 0.3 }    # assumption
# no [[simulation.breakdowns]] until MTBF and MTTR are measured
"""

# SDD Data Model, "Fuller example (every rule kind)", verbatim.
FULLER_SIMULATION_TOML = """
[simulation]
replications = 20
distribution_source = "profile"
baseline_rule = "edd"

[[simulation.run_factors]]
distribution = { kind = "lognormal", mean = 1.0, cv = 0.15 }

[[simulation.run_factors]]
work_center_id = "GRIND"
scope = "unit"
distribution = { kind = "shifted_gamma", minimum = 0.9, mean = 1.05, shape = 2 }

[[simulation.setup_factors]]
distribution = { kind = "lognormal", mean = 1.1, cv = 0.3 }

[[simulation.breakdowns]]
work_center_id = "COIL"
mtbf_seconds = 144000
repair = { kind = "lognormal", mean = 3600, cv = 0.5 }
"""

SEED_TOML = SCHEDULING_TOML + ERP_TOML + SEED_SIMULATION_TOML
FULLER_TOML = SCHEDULING_TOML + ERP_TOML + FULLER_SIMULATION_TOML

SEED_RUN = FactorRule(distribution=LogNormal(mean=1.0, cv=0.15))
SEED_SETUP = FactorRule(distribution=LogNormal(mean=1.1, cv=0.3))


def _with_simulation(*lines: str) -> str:
    """Scheduling and ERP keys plus a `[simulation]` table holding exactly `lines`."""
    return SCHEDULING_TOML + ERP_TOML + "\n[simulation]\n" + "\n".join(lines) + "\n"


def _profile_rules(*blocks: str) -> str:
    """A `profile`-source `[simulation]` table followed by array-of-table `blocks`."""
    return _with_simulation('distribution_source = "profile"') + "\n".join(blocks) + "\n"


def _breakdown(mtbf_seconds: float | int) -> str:
    return (
        "[[simulation.breakdowns]]\n"
        'machine_id = "M1"\n'
        f"mtbf_seconds = {mtbf_seconds}\n"
        'repair = { kind = "lognormal", mean = 600, cv = 0.5 }\n'
    )


def _run_factor(distribution: str = 'kind = "lognormal", mean = 1.0, cv = 0.15') -> str:
    return f"[[simulation.run_factors]]\ndistribution = {{ {distribution} }}\n"


def _load_error(text: str) -> str:
    """The `ProfileError` message `text` raises (fails the test if it loads)."""
    with pytest.raises(ProfileError) as caught:
        load_planning_profile(text)
    return str(caught.value)


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _policy_digest(policy: SimulationPolicy) -> str:
    """`twinflow.execution.policy.policy_digest`, looked up per call so a missing helper fails
    each test on its own instead of breaking collection."""
    digest: str = policy_module.policy_digest(policy)
    return digest


# --- the SDD examples load ------------------------------------------------------------------


def test_load_planning_profile_seed_calibration_loads_into_simulation_policy() -> None:
    simulation = load_planning_profile(SEED_TOML).simulation

    assert isinstance(simulation, SimulationPolicy)
    assert simulation.distribution_source == "profile"
    assert simulation.baseline_rule == "edd"
    assert simulation.run_factors == (SEED_RUN,)
    assert simulation.setup_factors == (SEED_SETUP,)
    assert simulation.breakdowns == ()
    assert simulation.outages == ()
    # Everything the seed does not name keeps its SDD default.
    assert simulation == SimulationPolicy(
        distribution_source="profile", run_factors=(SEED_RUN,), setup_factors=(SEED_SETUP,)
    )


def test_load_planning_profile_seed_calibration_factor_values_are_exact() -> None:
    simulation = load_planning_profile(SEED_TOML).simulation

    run = simulation.run_factors[0]
    setup = simulation.setup_factors[0]
    assert isinstance(run.distribution, LogNormal)
    assert (run.distribution.mean, run.distribution.cv) == (1.0, 0.15)
    assert isinstance(setup.distribution, LogNormal)
    assert (setup.distribution.mean, setup.distribution.cv) == (1.1, 0.3)
    assert (run.work_center_id, run.machine_id, run.family, run.scope) == (None, None, None, "lot")


def test_load_planning_profile_fuller_example_loads_every_rule_kind() -> None:
    simulation = load_planning_profile(FULLER_TOML).simulation

    assert simulation.replications == 20
    assert simulation.distribution_source == "profile"
    assert simulation.baseline_rule == "edd"
    assert simulation.run_factors == (
        SEED_RUN,
        FactorRule(
            work_center_id="GRIND",
            scope="unit",
            distribution=ShiftedGamma(minimum=0.9, mean=1.05, shape=2),
        ),
    )
    assert simulation.setup_factors == (SEED_SETUP,)
    assert simulation.breakdowns == (
        BreakdownRule(
            work_center_id="COIL", mtbf_seconds=144_000, repair=LogNormal(mean=3600, cv=0.5)
        ),
    )


def test_load_planning_profile_simulation_table_leaves_scheduling_and_erp_halves_alone() -> None:
    with_table = load_planning_profile(FULLER_TOML)
    without_table = load_planning_profile(SCHEDULING_TOML + ERP_TOML)

    assert with_table.scheduling == load_profile(SCHEDULING_TOML)
    assert with_table.scheduling == without_table.scheduling
    assert with_table.erp == without_table.erp


def test_load_planning_profile_every_scalar_simulation_key_is_read() -> None:
    simulation = load_planning_profile(
        _with_simulation(
            "replications = 7",
            "max_replications = 9",
            "time_budget_seconds = 45.5",
            "confidence_level = 0.95",
            "workers = 4",
            'baseline_rule = "spt"',
            "robustness_deltas_seconds = [600.0, 1200.0]",
            "robustness_delta_probabilities = [0.25, 0.5]",
            "robustness_probe_machines = 3",
            'robustness_objective = "makespan"',
            "max_candidates = 5",
        )
    ).simulation

    assert simulation.replications == 7
    assert simulation.max_replications == 9
    assert simulation.time_budget_seconds == 45.5
    assert simulation.confidence_level == 0.95
    assert simulation.workers == 4
    assert simulation.baseline_rule == "spt"
    assert simulation.robustness_deltas_seconds == (600.0, 1200.0)
    assert simulation.robustness_delta_probabilities == (0.25, 0.5)
    assert simulation.robustness_probe_machines == 3
    assert simulation.robustness_objective == ObjectiveKind.MAKESPAN
    assert simulation.max_candidates == 5


# --- defaults -------------------------------------------------------------------------------


def test_load_planning_profile_without_simulation_table_gives_simulation_policy_defaults() -> None:
    assert load_planning_profile(SCHEDULING_TOML + ERP_TOML).simulation == SimulationPolicy()


def test_load_planning_profile_empty_text_gives_simulation_defaults() -> None:
    assert load_planning_profile("").simulation == SimulationPolicy()


def test_load_planning_profile_empty_simulation_table_gives_defaults() -> None:
    assert load_planning_profile(_with_simulation()).simulation == SimulationPolicy()


def test_planning_profile_built_without_simulation_defaults_it() -> None:
    profile = load_planning_profile(SCHEDULING_TOML + ERP_TOML)

    rebuilt = PlanningProfile(scheduling=profile.scheduling, erp=profile.erp)

    assert rebuilt.simulation == SimulationPolicy()


# --- unknown keys fail with dotted paths ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "path"),
    [
        (_with_simulation("zz_unknown = 1"), "simulation.zz_unknown: extra_forbidden"),
        (
            _profile_rules(_breakdown(3600) + "zz_unknown = 1\n"),
            "simulation.breakdowns[0].zz_unknown: extra_forbidden",
        ),
        (
            _profile_rules(
                _run_factor(), _run_factor('kind = "lognormal", cv = 0.1, zz_unknown = 1')
            ),
            "simulation.run_factors[1].distribution.zz_unknown: extra_forbidden",
        ),
        (
            _profile_rules(_run_factor('kind = "weibull", shape = 2')),
            "simulation.run_factors[0].distribution: union_tag_invalid",
        ),
    ],
    ids=["table-key", "rule-key", "distribution-key", "distribution-kind"],
)
def test_load_planning_profile_unknown_simulation_key_names_dotted_path(
    text: str, path: str
) -> None:
    assert path in _load_error(text)


# --- bad values fail with dotted paths (REQ-143) -------------------------------------------


def test_load_planning_profile_mtbf_59_rejected_with_indexed_path_and_no_value() -> None:
    message = _load_error(_profile_rules(_breakdown(3600), _breakdown(59)))

    assert "simulation.breakdowns[1].mtbf_seconds: greater_than_equal" in message
    assert "59" not in message  # values are never echoed (TF-005 REQ-170)


def test_load_planning_profile_first_breakdown_mtbf_path_is_index_zero() -> None:
    message = _load_error(_profile_rules(_breakdown(59)))

    assert "simulation.breakdowns[0].mtbf_seconds: greater_than_equal" in message


def test_load_planning_profile_mtbf_60_accepted() -> None:
    simulation = load_planning_profile(_profile_rules(_breakdown(60))).simulation

    assert simulation.breakdowns[0].mtbf_seconds == 60


def test_load_planning_profile_bad_cv_in_third_run_factor_drops_the_kind_tag() -> None:
    text = FULLER_TOML + _run_factor('kind = "lognormal", mean = 1.0, cv = 4.0')

    message = _load_error(text)

    assert "simulation.run_factors[2].distribution.cv: less_than_equal" in message
    assert "lognormal" not in message


@pytest.mark.parametrize(
    ("lines", "path"),
    [
        (["time_budget_seconds = inf"], "simulation.time_budget_seconds: finite_number"),
        (["time_budget_seconds = 301"], "simulation.time_budget_seconds: less_than_equal"),
        (["time_budget_seconds = 0"], "simulation.time_budget_seconds: greater_than"),
        (["confidence_level = 0.4"], "simulation.confidence_level: greater_than_equal"),
        (["workers = 17"], "simulation.workers: less_than_equal"),
        (["max_replications = 1001"], "simulation.max_replications: less_than_equal"),
        (['baseline_rule = "random"'], "simulation.baseline_rule: literal_error"),
        (['distribution_source = "file"'], "simulation.distribution_source: literal_error"),
        (
            ["robustness_probe_machines = 6"],
            "simulation.robustness_probe_machines: less_than_equal",
        ),
        (["max_candidates = 51"], "simulation.max_candidates: less_than_equal"),
        (["robustness_deltas_seconds = []"], "simulation.robustness_deltas_seconds: too_short"),
        (
            ["robustness_deltas_seconds = [600.0, -1.0]"],
            "simulation.robustness_deltas_seconds[1]: greater_than",
        ),
        (
            [
                "robustness_deltas_seconds = [600.0, 1200.0]",
                "robustness_delta_probabilities = [1.5, 0.0]",
            ],
            "simulation.robustness_delta_probabilities[0]: less_than_equal",
        ),
    ],
)
def test_load_planning_profile_bad_scalar_value_names_dotted_path(
    lines: list[str], path: str
) -> None:
    assert path in _load_error(_with_simulation(*lines))


def test_load_planning_profile_eleven_deltas_rejected() -> None:
    deltas = ", ".join(str(600.0 * (i + 1)) for i in range(11))

    message = _load_error(_with_simulation(f"robustness_deltas_seconds = [{deltas}]"))

    assert "simulation.robustness_deltas_seconds: too_long" in message


def test_load_planning_profile_ten_deltas_accepted() -> None:
    deltas = ", ".join(str(600.0 * (i + 1)) for i in range(10))

    simulation = load_planning_profile(
        _with_simulation(f"robustness_deltas_seconds = [{deltas}]")
    ).simulation

    assert len(simulation.robustness_deltas_seconds) == 10


# Model-level checks (cross-field) have no single field; they report at the table itself.
_TABLE_VALUE_ERROR = re.compile(r"simulation(\.[a-z_]+)?: value_error")


@pytest.mark.parametrize(
    "lines",
    [
        ["replications = 101"],  # default max_replications is 100
        ["replications = 50", "max_replications = 40"],
        [  # probabilities of another length
            "robustness_deltas_seconds = [600.0, 1200.0, 1800.0]",
            "robustness_delta_probabilities = [0.5, 0.5]",
        ],
        [  # probabilities summing over 1
            "robustness_deltas_seconds = [600.0, 1200.0]",
            "robustness_delta_probabilities = [0.6, 0.5]",
        ],
        [  # rules under the deterministic source would be silently ignored
            'distribution_source = "deterministic"',
            "[[simulation.run_factors]]",
            'distribution = { kind = "lognormal", mean = 1.0, cv = 0.15 }',
        ],
    ],
    ids=[
        "reps-over-default-cap",
        "reps-over-own-cap",
        "probabilities-length",
        "probabilities-sum",
        "rules-unused",
    ],
)
def test_load_planning_profile_cross_field_violation_rejected_at_simulation_path(
    lines: list[str],
) -> None:
    assert _TABLE_VALUE_ERROR.search(_load_error(_with_simulation(*lines)))


@pytest.mark.parametrize(
    "lines",
    [
        ["replications = 100"],
        ["replications = 40", "max_replications = 40"],
        [  # decimal drift: 0.1 + 0.2 + 0.7 is a hair over 1.0 in floats
            "robustness_deltas_seconds = [600.0, 1200.0, 1800.0]",
            "robustness_delta_probabilities = [0.1, 0.2, 0.7]",
        ],
    ],
    ids=["reps-at-default-cap", "reps-at-own-cap", "probabilities-sum-one"],
)
def test_load_planning_profile_cross_field_boundaries_accepted(lines: list[str]) -> None:
    assert isinstance(load_planning_profile(_with_simulation(*lines)).simulation, SimulationPolicy)


@pytest.mark.parametrize("table", ["run_factors", "setup_factors"])
def test_load_planning_profile_one_thousand_and_one_factor_rules_rejected(table: str) -> None:
    block = f'[[simulation.{table}]]\ndistribution = {{ kind = "deterministic" }}\n'

    message = _load_error(_profile_rules(block * 1_001))

    assert f"simulation.{table}: too_long" in message


def test_load_planning_profile_one_thousand_and_one_breakdowns_rejected() -> None:
    message = _load_error(_profile_rules(_breakdown(3600) * 1_001))

    assert "simulation.breakdowns: too_long" in message


def test_load_planning_profile_one_thousand_and_one_outages_rejected() -> None:
    block = '[[simulation.outages]]\nmachine_id = "M1"\nstart = 0.0\nduration = 60.0\n'

    message = _load_error(_profile_rules(block * 1_001))

    assert "simulation.outages: too_long" in message


def test_load_planning_profile_one_thousand_factor_rules_accepted() -> None:
    block = '[[simulation.run_factors]]\ndistribution = { kind = "deterministic" }\n'

    simulation = load_planning_profile(_profile_rules(block * 1_000)).simulation

    assert len(simulation.run_factors) == 1_000


def test_load_planning_profile_reports_erp_and_simulation_problems_together() -> None:
    text = SCHEDULING_TOML + "\n[erp]\nhorizon_days = 0\n" + "\n[simulation]\nworkers = 0\n"

    message = _load_error(text)

    assert "erp.horizon_days" in message
    assert "simulation.workers: greater_than_equal" in message


# --- digests --------------------------------------------------------------------------------


def test_profile_digest_covers_the_simulation_table() -> None:
    profile = load_planning_profile(SEED_TOML)

    assert profile.model_dump(mode="json")["simulation"] == profile.simulation.model_dump(
        mode="json"
    )
    assert profile_digest(profile) == _canonical_sha256(profile.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("cv = 0.15", "cv = 0.16"),  # a factor value
        ("mtbf_seconds = 144000", "mtbf_seconds = 144001"),  # a breakdown value
        ("replications = 20", "replications = 21"),  # a scalar
        ('work_center_id = "GRIND"', 'work_center_id = "LATHE"'),  # a rule's match
    ],
)
def test_profile_digest_changes_when_the_simulation_table_changes(old: str, new: str) -> None:
    assert old in FULLER_TOML
    changed = FULLER_TOML.replace(old, new, 1)

    assert profile_digest(load_planning_profile(changed)) != profile_digest(
        load_planning_profile(FULLER_TOML)
    )


def test_profile_digest_differs_with_and_without_a_simulation_calibration() -> None:
    assert profile_digest(load_planning_profile(SEED_TOML)) != profile_digest(
        load_planning_profile(SCHEDULING_TOML + ERP_TOML)
    )


def test_profile_digest_of_explicit_simulation_defaults_equals_omitted_table() -> None:
    explicit = _with_simulation("replications = 20", 'baseline_rule = "edd"')

    assert profile_digest(load_planning_profile(explicit)) == profile_digest(
        load_planning_profile(SCHEDULING_TOML + ERP_TOML)
    )


def test_planning_profile_with_simulation_json_round_trips() -> None:
    profile = load_planning_profile(FULLER_TOML)

    restored = PlanningProfile.model_validate_json(profile.model_dump_json())

    assert restored == profile
    assert restored.simulation == profile.simulation
    assert profile_digest(restored) == profile_digest(profile)


def test_policy_digest_is_sha256_of_the_policy_canonical_json() -> None:
    policy = load_planning_profile(FULLER_TOML).simulation

    digest = _policy_digest(policy)

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == _canonical_sha256(policy.model_dump(mode="json"))


def test_policy_digest_is_stable_across_equal_policies() -> None:
    first = load_planning_profile(FULLER_TOML).simulation
    again = load_planning_profile(FULLER_TOML).simulation
    rebuilt = SimulationPolicy.model_validate_json(first.model_dump_json())

    assert _policy_digest(first) == _policy_digest(again) == _policy_digest(rebuilt)
    assert _policy_digest(SimulationPolicy()) == _policy_digest(SimulationPolicy())


@pytest.mark.parametrize(
    "change",
    [
        {"replications": 21},
        {"baseline_rule": "spt"},
        {"robustness_deltas_seconds": (1800.0, 3600.0)},
        {"robustness_objective": ObjectiveKind.MAKESPAN},
        {"distribution_source": "profile", "run_factors": (SEED_RUN,)},
    ],
    ids=["replications", "baseline", "deltas", "objective", "rules"],
)
def test_policy_digest_changes_when_any_field_changes(change: dict[str, Any]) -> None:
    base = SimulationPolicy()
    changed = SimulationPolicy.model_validate({**base.model_dump(), **change})

    assert changed != base
    assert _policy_digest(changed) != _policy_digest(base)


def test_policy_digest_ignores_the_rest_of_the_profile() -> None:
    """Only `[simulation]` is policy: an ERP or scheduling edit leaves the policy digest."""
    first = load_planning_profile(FULLER_TOML)
    other = load_planning_profile(FULLER_TOML.replace("horizon_days = 28", "horizon_days = 29"))

    assert profile_digest(first) != profile_digest(other)
    assert _policy_digest(first.simulation) == _policy_digest(other.simulation)


def test_profile_digest_matches_the_signed_store_digest_for_non_ascii_ids() -> None:
    """The store signs `json.dumps(..., ensure_ascii=True)`; a profile with a non-ASCII id
    must re-digest to the same value or every load of it fails closed."""
    profile = load_planning_profile('id = "Werk-Süd"\n')
    stored = json.dumps(
        profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    assert profile_digest(profile) == hashlib.sha256(stored.encode("utf-8")).hexdigest()
