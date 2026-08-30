"""Integration tests for the real ScoringSurface seam (not the FakeSurface stub).

Runs the actual simulator through the module surface at tiny `reps`/`budget`, proving
that a scenario really evaluates to a KPI band and that an optimizer really searches
the lever space end to end. Kept small so it stays fast in CI.
"""

from __future__ import annotations

from twinflow.model import load_model
from twinflow.modules import (
    OBJECTIVES,
    OPTIMIZERS,
    IntRange,
    LeverSpace,
    Scenario,
    ScoringSurface,
    optimize,
)
from twinflow.plan.loader import load_plan

_MODEL = "examples/cnc-shop/model.yaml"
_PLAN = "examples/cnc-shop/plan.csv"
_HEADCOUNT = "labor.pools[0].headcount"


def _plan() -> list[object]:
    compiled = load_model(_MODEL)
    return load_plan(_PLAN, compiled.registry)


def test_surface_evaluates_to_a_band() -> None:
    surface = ScoringSurface(_MODEL, _plan(), reps=3, base_seed=0)
    evaluation = surface.evaluate(Scenario(levers={_HEADCOUNT: 3}))
    band = evaluation.intervals.on_time_pct
    assert band.lo <= band.mean <= band.hi
    assert 0.0 <= band.mean <= 100.0
    assert evaluation.kpis.run_hours > 0.0


def test_surface_evaluation_is_reproducible() -> None:
    surface = ScoringSurface(_MODEL, _plan(), reps=3, base_seed=0)
    first = surface.evaluate(Scenario(levers={_HEADCOUNT: 3}))
    second = surface.evaluate(Scenario(levers={_HEADCOUNT: 3}))
    assert first.intervals.on_time_pct.mean == second.intervals.on_time_pct.mean


def test_optimize_searches_the_lever_space() -> None:
    space = LeverSpace({_HEADCOUNT: IntRange(2, 4)})
    result = optimize(
        _MODEL,
        _plan(),
        space,
        OBJECTIVES.create("on_time_pct"),
        OPTIMIZERS.create("grid"),
        budget=3,
        reps=3,
    )
    assert result.best.scenario.levers[_HEADCOUNT] in (2, 3, 4)
    assert result.evaluations_used == 3
    assert len(result.history) == 3
