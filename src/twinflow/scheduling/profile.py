"""Scheduling profile: the default plant policy and TOML loading (COMP-004).

The `SchedulingProfile` type lives in `model.py`; this module only builds and reads it.
Signing of profile files is deferred to TF-003 (B-2).
"""

from __future__ import annotations

import tomllib

from twinflow.scheduling.model import SchedulingProfile


def default_profile() -> SchedulingProfile:
    """The profile that keeps today's behavior: lexicographic makespan, baseline solver.

    Class weights {1: 1, 2: 2, 4: 4}, robustness load targets (0.9, 0.8, 0.7) (Pinedo
    p.486), portfolio ("baseline",), no frozen zone.
    """
    return SchedulingProfile()


def load_profile(text: str) -> SchedulingProfile:
    """Parse a TOML profile and validate it; omitted fields take the default values.

    Top-level keys are `SchedulingProfile` fields. TOML table keys are always strings, so
    `[class_weights]` keys such as `1` arrive as `"1"` and are coerced to int by model
    validation. Unknown keys at any level are rejected.

    Raises:
        tomllib.TOMLDecodeError: the text is not valid TOML.
        pydantic.ValidationError: the data is not a valid profile.
    """
    return SchedulingProfile.model_validate(tomllib.loads(text))
