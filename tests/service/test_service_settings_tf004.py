"""ServiceSettings for TF-004 simulation caps (T-188, COMP-140; REQ-139, D-105).

Pinned (SDD COMP-140): `max_simulate_seconds = 120.0`, `max_simulation_replications = 200`,
`max_simulation_candidates = 20`, `max_simulation_workers = 4`. Like every existing numeric
bound they must be positive (`ValueError`), and like the existing numeric bounds they are
constructor settings only: `from_env` reads no environment variable for them (the existing
pattern reads env only for files, hosts and origins). `max_simulate_seconds` must also be
finite: it becomes the guard timeout of the simulate tools (an infinite cap is no cap).
"""

from __future__ import annotations

import math

import pytest

from twinflow.service.settings import ServiceSettings

SIMULATION_FIELDS = (
    "max_simulate_seconds",
    "max_simulation_replications",
    "max_simulation_candidates",
    "max_simulation_workers",
)


def test_simulation_caps_have_pinned_defaults() -> None:
    settings = ServiceSettings()

    assert settings.max_simulate_seconds == 120.0
    assert settings.max_simulation_replications == 200
    assert settings.max_simulation_candidates == 20
    assert settings.max_simulation_workers == 4


def test_simulation_caps_are_configurable() -> None:
    settings = ServiceSettings(
        max_simulate_seconds=30.0,
        max_simulation_replications=50,
        max_simulation_candidates=6,
        max_simulation_workers=1,
    )

    assert (
        settings.max_simulate_seconds,
        settings.max_simulation_replications,
        settings.max_simulation_candidates,
        settings.max_simulation_workers,
    ) == (30.0, 50, 6, 1)


@pytest.mark.parametrize("name", SIMULATION_FIELDS)
@pytest.mark.parametrize("value", [0, -1])
def test_simulation_caps_must_be_positive(name: str, value: int) -> None:
    assert name in ServiceSettings.__dataclass_fields__, f"{name} is not a setting"

    with pytest.raises(ValueError, match=name):
        ServiceSettings(**{name: value})  # type: ignore[arg-type]  # dynamic field under test


@pytest.mark.parametrize("value", [math.inf, math.nan])
def test_max_simulate_seconds_must_be_finite(value: float) -> None:
    assert "max_simulate_seconds" in ServiceSettings.__dataclass_fields__

    with pytest.raises(ValueError, match="max_simulate_seconds"):
        ServiceSettings(max_simulate_seconds=value)
