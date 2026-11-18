"""Tests for the ERP planning profile (T-196, COMP-113, COMP-126).

Requirement: REQ-160. Decision: D-115 (TF-002 `SchedulingProfile` stays unchanged).

Contract under test: `load_planning_profile(text)` parses TOML, pops `[erp]` into
`ErpPolicy`, hands the remaining top-level keys to TF-002 `SchedulingProfile`, and raises
one documented error type, `ProfileError` (a `ValueError`), whose message names the dotted
TOML path of the offending key.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from twinflow.erp.profile import (
    ErpPolicy,
    PlanningProfile,
    ProfileError,
    load_planning_profile,
    profile_digest,
)
from twinflow.scheduling.model import AbsDiffTerm, SchedulingProfile, SetupFunction
from twinflow.scheduling.profile import load_profile

SCHEDULING_TOML = """
id = "plant-a"
time_budget_seconds = 30.0

[class_weights]
1 = 1.0
2 = 3.0
4 = 9.0
"""

ERP_TOML = """
[erp]
shortage_policy = "flag"
horizon_days = 28

[erp.class_weights]
"A-KEY" = 4
"B-CORE" = 2
"C-SPOT" = 1

[erp.setup_functions.wc-coil]
id = "coil-diameter"
terms = [{ kind = "abs_diff", param = "wire_diameter_mm", seconds_per_unit = 120.0 }]
"""

SAMPLE_TOML = SCHEDULING_TOML + ERP_TOML

# Same content as SAMPLE_TOML; every key and table that TOML lets move is moved.
SAMPLE_TOML_REORDERED = """
time_budget_seconds = 30.0
id = "plant-a"

[erp.setup_functions.wc-coil]
terms = [{ seconds_per_unit = 120.0, param = "wire_diameter_mm", kind = "abs_diff" }]
id = "coil-diameter"

[erp.class_weights]
"C-SPOT" = 1
"A-KEY" = 4
"B-CORE" = 2

[class_weights]
4 = 9.0
1 = 1.0
2 = 3.0

[erp]
horizon_days = 28
shortage_policy = "flag"
"""


def _with_erp(*lines: str) -> str:
    """Scheduling keys plus an `[erp]` table holding exactly `lines`."""
    return SCHEDULING_TOML + "\n[erp]\n" + "\n".join(lines) + "\n"


# --- happy path -----------------------------------------------------------------------------


def test_load_planning_profile_sample_loads_scheduling_part_as_tf002_profile() -> None:
    profile = load_planning_profile(SAMPLE_TOML)

    assert isinstance(profile, PlanningProfile)
    assert isinstance(profile.scheduling, SchedulingProfile)
    # The scheduling half is exactly what TF-002 would load from the same keys.
    assert profile.scheduling == load_profile(SCHEDULING_TOML)
    assert profile.scheduling.id == "plant-a"
    assert profile.scheduling.time_budget_seconds == 30.0
    assert profile.scheduling.class_weights == {1: 1.0, 2: 3.0, 4: 9.0}


def test_load_planning_profile_sample_loads_erp_policy() -> None:
    erp = load_planning_profile(SAMPLE_TOML).erp

    assert isinstance(erp, ErpPolicy)
    assert erp.class_weights == {"A-KEY": 4, "B-CORE": 2, "C-SPOT": 1}
    assert erp.shortage_policy == "flag"
    assert erp.horizon_days == 28


def test_load_planning_profile_setup_function_is_tf002_setup_function_keyed_by_work_center() -> (
    None
):
    erp = load_planning_profile(SAMPLE_TOML).erp

    assert set(erp.setup_functions) == {"wc-coil"}
    function = erp.setup_functions["wc-coil"]
    assert isinstance(function, SetupFunction)
    assert function == SetupFunction(
        id="coil-diameter",
        terms=(AbsDiffTerm(param="wire_diameter_mm", seconds_per_unit=120.0),),
    )


# --- defaults -------------------------------------------------------------------------------


def test_load_planning_profile_without_erp_table_gives_sdd_defaults() -> None:
    erp = load_planning_profile(SCHEDULING_TOML).erp

    assert erp.class_weights == {}
    assert erp.default_class == 1
    assert erp.stock_class == 1
    assert erp.rush_class == 4
    assert erp.alternate_penalty_seconds == 600
    assert erp.shortage_policy == "flag"
    assert erp.due_at == "end_of_day"
    assert erp.horizon_days == 56
    assert erp.po_past_due_grace_seconds == 86_400
    assert erp.setup_family_param is None
    assert erp.setup_functions == {}
    assert erp.use_overtime is True  # COMP-126
    assert erp == ErpPolicy()


def test_load_planning_profile_empty_text_gives_both_defaults() -> None:
    profile = load_planning_profile("")

    assert profile.scheduling == SchedulingProfile()
    assert profile.erp == ErpPolicy()


def test_load_planning_profile_erp_keys_override_defaults() -> None:
    erp = load_planning_profile(
        _with_erp(
            "default_class = 2",
            "stock_class = 2",
            "rush_class = 2",
            "alternate_penalty_seconds = 900",
            'shortage_policy = "hold"',
            'due_at = "start_of_day"',
            "po_past_due_grace_seconds = 3600",
            'setup_family_param = "wire_family"',
            "use_overtime = false",
        )
    ).erp

    assert erp.default_class == 2
    assert erp.stock_class == 2
    assert erp.rush_class == 2
    assert erp.alternate_penalty_seconds == 900
    assert erp.shortage_policy == "hold"
    assert erp.due_at == "start_of_day"
    assert erp.po_past_due_grace_seconds == 3600
    assert erp.setup_family_param == "wire_family"
    assert erp.use_overtime is False


# --- unknown keys fail with their TOML path ---------------------------------------------------


def test_load_planning_profile_unknown_top_level_key_names_its_path() -> None:
    with pytest.raises(ProfileError, match=r"zz_unknown_top"):
        load_planning_profile("zz_unknown_top = 1\n" + ERP_TOML)


def test_load_planning_profile_unknown_key_in_erp_names_dotted_path() -> None:
    with pytest.raises(ProfileError, match=re.escape("erp.zz_unknown_erp")):
        load_planning_profile(_with_erp("zz_unknown_erp = 1"))


def test_load_planning_profile_unknown_key_in_setup_function_names_wc_and_key() -> None:
    text = SAMPLE_TOML.replace('id = "coil-diameter"', 'id = "coil-diameter"\nzz_unknown_setup = 1')

    with pytest.raises(
        ProfileError, match=re.escape("erp.setup_functions.wc-coil.zz_unknown_setup")
    ):
        load_planning_profile(text)


def test_load_planning_profile_unknown_key_in_nested_scheduling_table_names_path() -> None:
    text = SAMPLE_TOML + "\n[frozen_zone]\nuntil = 60.0\ntolerance = 0.0\nzz_unknown_zone = 1\n"

    with pytest.raises(ProfileError, match=re.escape("frozen_zone.zz_unknown_zone")):
        load_planning_profile(text)


def test_profile_error_is_a_value_error() -> None:
    assert issubclass(ProfileError, ValueError)


# --- value bounds -----------------------------------------------------------------------------


def test_load_planning_profile_class_weight_three_rejected_with_path() -> None:
    with pytest.raises(ProfileError, match=re.escape("erp.class_weights")):
        load_planning_profile(_with_erp("[erp.class_weights]", '"B-CORE" = 3'))


@pytest.mark.parametrize("field", ["default_class", "stock_class", "rush_class"])
def test_load_planning_profile_named_class_three_rejected_with_path(field: str) -> None:
    with pytest.raises(ProfileError, match=re.escape(f"erp.{field}")):
        load_planning_profile(_with_erp(f"{field} = 3"))


@pytest.mark.parametrize("days", [0, 367])
def test_load_planning_profile_horizon_days_out_of_range_rejected(days: int) -> None:
    with pytest.raises(ProfileError, match=re.escape("erp.horizon_days")):
        load_planning_profile(_with_erp(f"horizon_days = {days}"))


@pytest.mark.parametrize("days", [1, 366])
def test_load_planning_profile_horizon_days_boundaries_accepted(days: int) -> None:
    assert load_planning_profile(_with_erp(f"horizon_days = {days}")).erp.horizon_days == days


def test_load_planning_profile_unknown_shortage_policy_rejected() -> None:
    with pytest.raises(ProfileError, match=re.escape("erp.shortage_policy")):
        load_planning_profile(_with_erp('shortage_policy = "panic"'))


@pytest.mark.parametrize("due_at", ["end_of_day", "start_of_day"])
def test_load_planning_profile_due_at_accepts_documented_values(due_at: str) -> None:
    assert load_planning_profile(_with_erp(f'due_at = "{due_at}"')).erp.due_at == due_at


@pytest.mark.parametrize("due_at", ["noon", "END_OF_DAY", ""])
def test_load_planning_profile_due_at_rejects_other_values(due_at: str) -> None:
    with pytest.raises(ProfileError, match=re.escape("erp.due_at")):
        load_planning_profile(_with_erp(f'due_at = "{due_at}"'))


def test_load_planning_profile_scheduling_validation_error_is_profile_error() -> None:
    # TF-002 rejects class_weights without exactly the keys 1, 2, 4; still one error type.
    with pytest.raises(ProfileError):
        load_planning_profile("[class_weights]\n1 = 1.0\n")


def test_load_planning_profile_invalid_toml_is_profile_error() -> None:
    with pytest.raises(ProfileError):
        load_planning_profile("[erp\nhorizon_days = 5\n")


def test_erp_policy_rejects_class_three_when_built_directly() -> None:
    with pytest.raises(ValidationError):
        ErpPolicy.model_validate({"class_weights": {"B-CORE": 3}})


# --- TF-002 loader unchanged --------------------------------------------------------------------


def test_tf002_load_profile_still_rejects_erp_table() -> None:
    with pytest.raises(ValidationError):
        load_profile(SAMPLE_TOML)


# --- digest -------------------------------------------------------------------------------------


def test_profile_digest_is_64_lowercase_hex() -> None:
    digest = profile_digest(load_planning_profile(SAMPLE_TOML))

    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_profile_digest_is_stable_across_loads_and_key_order() -> None:
    first = profile_digest(load_planning_profile(SAMPLE_TOML))
    again = profile_digest(load_planning_profile(SAMPLE_TOML))
    reordered = profile_digest(load_planning_profile(SAMPLE_TOML_REORDERED))

    assert first == again == reordered


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"B-CORE" = 2', '"B-CORE" = 4'),  # erp class map
        ("horizon_days = 28", "horizon_days = 29"),  # erp scalar
        ("seconds_per_unit = 120.0", "seconds_per_unit = 121.0"),  # setup function term
        ("time_budget_seconds = 30.0", "time_budget_seconds = 31.0"),  # scheduling half
    ],
)
def test_profile_digest_changes_when_a_value_changes(old: str, new: str) -> None:
    assert old in SAMPLE_TOML
    changed = SAMPLE_TOML.replace(old, new)

    assert profile_digest(load_planning_profile(changed)) != profile_digest(
        load_planning_profile(SAMPLE_TOML)
    )


# --- JSON round trip ------------------------------------------------------------------------


def test_planning_profile_json_round_trips() -> None:
    profile = load_planning_profile(SAMPLE_TOML)

    restored = PlanningProfile.model_validate_json(profile.model_dump_json())

    assert restored == profile
    assert profile_digest(restored) == profile_digest(profile)
