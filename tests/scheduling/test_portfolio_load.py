"""Load ratio and load-dependent weights (T-157; COMP-023, REQ-119, D-080, Pinedo p.430).

API these tests pin (`twinflow.scheduling.portfolio.load`):

- `load_ratio(compiled: CompiledProblem) -> float | None`: the bottleneck load ratio, the max
  over work centers of (sum over the center's pending ops of the shortest run on the center's
  machines + n * sbar) / (sum over the center's machines of working seconds in [now, H)).
  n and sbar are the center's `InstanceStats` values (DEV-9). H = profile `horizon_end`, else
  problem `horizon_end`, else the max job due, else the ratio is None.
- `effective_objective(profile: SchedulingProfile, ratio: float | None) -> ObjectiveSpec`:
  with `profile.load_weights` set, a `WeightedSum` whose terms keep the light terms' kinds,
  order and scale and whose weights are (1 - theta) * light + theta * heavy with
  theta = clamp((ratio - light_load) / (heavy_load - light_load), 0, 1); ratio None means the
  light weights. Without `load_weights`, the profile objective unchanged.

The hand values are computed in each test's comments, not by calling project code.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

import pytest
from kernel_cases import compile_request, load_request, machine, op, request_from

from twinflow.scheduling.model import (
    CompiledProblem,
    ObjectiveKind,
    SchedulingProfile,
    WeightedSum,
)

WT = ObjectiveKind.WEIGHTED_TARDINESS
SETUP = ObjectiveKind.TOTAL_SETUP

LIGHT = {WT: 0.8, SETUP: 0.2}
HEAVY = {WT: 0.3, SETUP: 0.7}


def _load() -> ModuleType:
    return importlib.import_module("twinflow.scheduling.portfolio.load")


def _profile(**fields: Any) -> SchedulingProfile:
    base: dict[str, Any] = {
        "objective": {
            "mode": "weighted_sum",
            "terms": [{"kind": "weighted_tardiness", "weight": 1.0}],
        },
        "load_weights": {
            "light_load": 0.6,
            "heavy_load": 0.95,
            "light": [
                {"kind": "weighted_tardiness", "weight": LIGHT[WT], "scale": 50.0},
                {"kind": "total_setup", "weight": LIGHT[SETUP], "scale": 10.0},
            ],
            "heavy": [
                {"kind": "weighted_tardiness", "weight": HEAVY[WT], "scale": 50.0},
                {"kind": "total_setup", "weight": HEAVY[SETUP], "scale": 10.0},
            ],
        },
    }
    return SchedulingProfile.model_validate(base | fields)


def _two_centers(
    *, profile_horizon: float | None, problem_horizon: float | None, due: float | None
) -> CompiledProblem:
    """Center A: A1 (calendar [10,30) + [40,100)) and A2 (always on); center B: B1.

    Pending ops: a1 runs A1 20 / A2 25 (shortest 20), a2 runs A1 30, b1 runs B1 10.
    No setup functions, so sbar is 0 on both centers. now = 20.
    """
    jobs: list[dict[str, Any]] = [
        {"id": "a1", "operations": [op("a1-1", {"A1": 20.0, "A2": 25.0}, center="A")]},
        {"id": "a2", "operations": [op("a2-1", {"A1": 30.0}, center="A")]},
        {"id": "b1", "operations": [op("b1-1", {"B1": 10.0}, center="B")]},
    ]
    if due is not None:
        jobs[0]["due"] = due
    problem: dict[str, Any] = {
        "id": "load-hand",
        "now": 20.0,
        "jobs": jobs,
        "machines": [
            machine("A1", center="A", calendar_id="calA"),
            machine("A2", center="A"),
            machine("B1", center="B"),
        ],
        "work_centers": [{"id": "A"}, {"id": "B"}],
        "calendars": [{"id": "calA", "intervals": [[10.0, 30.0], [40.0, 100.0]]}],
    }
    if problem_horizon is not None:
        problem["horizon_end"] = problem_horizon
    profile = {"horizon_end": profile_horizon} if profile_horizon is not None else None
    return compile_request(request_from(problem, profile=profile))


# --- load_ratio ------------------------------------------------------------------------------


def test_load_ratio_pinedo_14_2_1_counts_setups_and_runs_to_the_max_due() -> None:
    # Shortest runs 13 + 9 + 13 + 10 = 45; n = 4, sbar = 2.0 (DEV-9) -> 45 + 8 = 53.
    # No horizon: H = max due = 37, now = 0, one always-on machine -> 37 working seconds.
    compiled = compile_request(load_request("pinedo_14_2_1"))
    assert _load().load_ratio(compiled) == pytest.approx(53.0 / 37.0)


def test_load_ratio_uses_the_profile_horizon_first_and_calendar_working_time() -> None:
    # H = 60 (profile beats problem 1000). A: work 20 + 30 = 50; A1 works [20,30) + [40,60)
    # = 30, A2 works 40 -> 70; 50/70. B: 10 / 40 = 0.25. Max = 50/70.
    compiled = _two_centers(profile_horizon=60.0, problem_horizon=1000.0, due=80.0)
    assert _load().load_ratio(compiled) == pytest.approx(50.0 / 70.0)


def test_load_ratio_falls_back_to_the_problem_horizon() -> None:
    # H = 1000. A1 works [20,30) + [40,100) = 70, A2 works 980 -> 1050; A = 50/1050.
    # B = 10/980. Max = 50/1050.
    compiled = _two_centers(profile_horizon=None, problem_horizon=1000.0, due=80.0)
    assert _load().load_ratio(compiled) == pytest.approx(50.0 / 1050.0)


def test_load_ratio_falls_back_to_the_max_due() -> None:
    # H = 80. A1 works [20,30) + [40,80) = 50, A2 works 60 -> 110; A = 50/110.
    # B = 10/60. Max = 50/110.
    compiled = _two_centers(profile_horizon=None, problem_horizon=None, due=80.0)
    assert _load().load_ratio(compiled) == pytest.approx(50.0 / 110.0)


def test_load_ratio_without_horizon_or_dues_is_none() -> None:
    compiled = _two_centers(profile_horizon=None, problem_horizon=None, due=None)
    assert _load().load_ratio(compiled) is None


# --- effective_objective ------------------------------------------------------------------------


def _weights(spec: Any) -> dict[ObjectiveKind, float]:
    assert isinstance(spec, WeightedSum)
    assert [term.kind for term in spec.terms] == [WT, SETUP]
    assert [term.scale for term in spec.terms] == [50.0, 10.0]
    return {term.kind: term.weight for term in spec.terms}


@pytest.mark.parametrize("ratio", [0.0, 0.3, 0.59, 0.6])
def test_effective_objective_at_or_below_light_load_gives_the_light_weights(ratio: float) -> None:
    assert _weights(_load().effective_objective(_profile(), ratio)) == pytest.approx(LIGHT)


@pytest.mark.parametrize("ratio", [0.95, 0.96, 1.5, 40.0])
def test_effective_objective_at_or_above_heavy_load_gives_the_heavy_weights(ratio: float) -> None:
    assert _weights(_load().effective_objective(_profile(), ratio)) == pytest.approx(HEAVY)


def test_effective_objective_halfway_gives_the_mean_weights() -> None:
    halfway = (0.6 + 0.95) / 2
    expected = {WT: (0.8 + 0.3) / 2, SETUP: (0.2 + 0.7) / 2}
    assert _weights(_load().effective_objective(_profile(), halfway)) == pytest.approx(expected)


def test_effective_objective_quarter_way_interpolates_linearly() -> None:
    ratio = 0.6 + 0.25 * (0.95 - 0.6)
    expected = {WT: 0.75 * 0.8 + 0.25 * 0.3, SETUP: 0.75 * 0.2 + 0.25 * 0.7}
    assert _weights(_load().effective_objective(_profile(), ratio)) == pytest.approx(expected)


def test_effective_objective_with_no_ratio_gives_the_light_weights() -> None:
    assert _weights(_load().effective_objective(_profile(), None)) == pytest.approx(LIGHT)


def test_heavier_load_never_lowers_the_setup_weight_or_raises_the_tardiness_weight() -> None:
    profile = _profile()
    ratios = [0.0, 0.5, 0.6, 0.65, 0.7, 0.8, 0.9, 0.95, 1.2]
    weights = [_weights(_load().effective_objective(profile, r)) for r in ratios]
    for lighter, heavier in zip(weights, weights[1:], strict=False):
        assert heavier[SETUP] >= lighter[SETUP]
        assert heavier[WT] <= lighter[WT]


@pytest.mark.parametrize("ratio", [None, 0.1, 0.8, 3.0])
def test_effective_objective_without_load_weights_is_the_profile_objective(
    ratio: float | None,
) -> None:
    profile = _profile(load_weights=None)
    assert _load().effective_objective(profile, ratio) == profile.objective


def test_no_horizon_and_no_dues_end_to_end_gives_none_and_the_light_weights() -> None:
    compiled = _two_centers(profile_horizon=None, problem_horizon=None, due=None)
    ratio = _load().load_ratio(compiled)
    assert ratio is None
    assert _weights(_load().effective_objective(_profile(), ratio)) == pytest.approx(LIGHT)
