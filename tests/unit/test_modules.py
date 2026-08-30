"""Unit tests for the twinflow.modules surface (registry, space, objectives, costs,
optimizers, surrogate models).

These never run the simulator: a `FakeSurface` returns a deterministic `Evaluation`
whose on-time % is a known function of one lever (a parabola peaking at headcount 4),
so search logic, objective ranking, cost math, and surrogate fitting are all tested
fast and deterministically. The real `ScoringSurface` seam is exercised separately in
tests/integration/test_modules_surface.py.
"""

from __future__ import annotations

import polars as pl
import pytest

from twinflow.instrumentation.aggregate import AggregatedKpis, Interval
from twinflow.instrumentation.inventory import InventoryKpis
from twinflow.instrumentation.kpis import KpiSet
from twinflow.model import load_model
from twinflow.modules import (
    CapacityCost,
    Choice,
    CostObjective,
    Evaluation,
    IntRange,
    LaborCost,
    LatenessPenalty,
    LeverSpace,
    LinearSurrogate,
    MetricObjective,
    NearestNeighborSurrogate,
    OptimizationResult,
    Registry,
    Scenario,
    TotalCost,
    WeightedObjective,
)
from twinflow.modules.models import MODELS, predicted_ranking
from twinflow.modules.objectives import OBJECTIVES, is_better, worst_score
from twinflow.modules.optimizers import (
    OPTIMIZERS,
    GeneticSearch,
    GridSearch,
    HillClimb,
    RandomSearch,
)

_MODEL_PATH = "examples/cnc-shop/model.yaml"
_HEADCOUNT = "labor.pools[0].headcount"


@pytest.fixture(scope="module")
def compiled() -> object:
    return load_model(_MODEL_PATH)


def _kpis(on_time: float, lateness: dict[str, float | None] | None = None) -> KpiSet:
    return KpiSet(
        completion_by_order={"wo-1": 1000.0},
        lateness_by_order=lateness if lateness is not None else {"wo-1": -100.0},
        on_time_pct=on_time,
        utilization_by_cell={"cut": 0.5},
        utilization_by_machine={"cut": 0.5},
        wait_seconds_by_location={},
        labor_pool_utilization={"default": 0.5},
        wip_by_location=pl.DataFrame(),
        machine_hours_by_machine={},
        labor_hours_by_pool_skill={},
        run_hours=10.0,
        setup_hours=0.0,
        event_counts_by_location={},
    )


def _evaluation(levers: dict[str, object], on_time: float, compiled: object) -> Evaluation:
    interval = Interval(mean=on_time, lo=on_time - 2, hi=on_time + 2, p50=on_time, n=3)
    aggregated = AggregatedKpis(
        on_time_pct=interval,
        lateness_by_order={},
        completion_by_order={},
        utilization_by_cell={},
        reps=3,
        level=0.9,
    )
    return Evaluation(Scenario(levers=levers), _kpis(on_time), aggregated, compiled)  # type: ignore[arg-type]


class FakeSurface:
    """A stand-in ScoringSurface: on-time % peaks at headcount 4 (a parabola)."""

    def __init__(self, compiled: object) -> None:
        self._compiled = compiled
        self.evaluated: list[int] = []

    def evaluate(self, scenario: Scenario) -> Evaluation:
        headcount = int(scenario.levers[_HEADCOUNT])
        self.evaluated.append(headcount)
        on_time = 100.0 - (headcount - 4) ** 2 * 8.0
        return _evaluation(dict(scenario.levers), on_time, self._compiled)


# --------------------------------------------------------------------------- registry
def test_registry_register_create_names() -> None:
    reg: Registry[str] = Registry("thing")
    reg.register("a", lambda: "alpha")
    assert reg.names() == ["a"]
    assert reg.create("a") == "alpha"
    assert "a" in reg


def test_registry_rejects_duplicate() -> None:
    reg: Registry[str] = Registry("thing")
    reg.register("a", lambda: "alpha")
    with pytest.raises(ValueError, match="already registered"):
        reg.register("a", lambda: "beta")


def test_registry_unknown_name_lists_available() -> None:
    reg: Registry[str] = Registry("thing")
    reg.register("a", lambda: "alpha")
    with pytest.raises(KeyError, match="available: a"):
        reg.create("missing")


# ------------------------------------------------------------------------------ space
def test_int_range_values_sample_clamp_neighbors() -> None:
    domain = IntRange(2, 8, step=2)
    assert domain.values() == [2, 4, 6, 8]
    assert domain.clamp(5) in (4, 6)
    assert domain.clamp(100) == 8
    assert domain.clamp(-1) == 2
    assert domain.neighbors(4) == [2, 6]
    assert domain.neighbors(8) == [6]


def test_int_range_rejects_bad_bounds() -> None:
    with pytest.raises(ValueError):
        IntRange(5, 1)
    with pytest.raises(ValueError):
        IntRange(1, 5, step=0)


def test_choice_domain() -> None:
    domain = Choice(("fifo", "edd", "spt"))
    assert domain.values() == ["fifo", "edd", "spt"]
    assert domain.clamp("nope") == "fifo"
    assert set(domain.neighbors("edd")) == {"fifo", "spt"}


def test_lever_space_grid_and_size() -> None:
    space = LeverSpace({"a": IntRange(1, 2), "b": Choice(("x", "y"))})
    assert space.grid_size() == 4
    grid = list(space.grid())
    assert len(grid) == 4
    assert {tuple(sorted(p.items())) for p in grid} == {
        (("a", 1), ("b", "x")),
        (("a", 1), ("b", "y")),
        (("a", 2), ("b", "x")),
        (("a", 2), ("b", "y")),
    }


def test_lever_space_neighbors_change_one_lever() -> None:
    space = LeverSpace({"a": IntRange(1, 5), "b": IntRange(1, 5)})
    point = {"a": 3, "b": 3}
    for neighbour in space.neighbors(point):
        differing = [k for k in point if neighbour[k] != point[k]]
        assert len(differing) == 1


def test_lever_space_rejects_empty() -> None:
    with pytest.raises(ValueError):
        LeverSpace({})


# ------------------------------------------------------------------- objectives/costs
def test_is_better_and_worst_score() -> None:
    assert is_better("max", 5, 3)
    assert not is_better("max", 3, 5)
    assert is_better("min", 3, 5)
    assert worst_score("max") == float("-inf")
    assert worst_score("min") == float("inf")


def test_metric_objective_reads_mean(compiled: object) -> None:
    obj = MetricObjective("on_time", "max", lambda e: e.intervals.on_time_pct.mean)
    assert obj.score(_evaluation({}, 80.0, compiled)) == 80.0


def test_builtin_objectives_registered() -> None:
    assert set(OBJECTIVES.names()) >= {
        "on_time_pct",
        "robust_on_time",
        "makespan",
        "mean_lateness",
        "utilization",
    }
    assert OBJECTIVES.create("makespan").direction == "min"
    assert OBJECTIVES.create("on_time_pct").direction == "max"


def test_weighted_objective_blends_with_orientation(compiled: object) -> None:
    on_time = OBJECTIVES.create("on_time_pct")  # max
    makespan = OBJECTIVES.create("makespan")  # min -> negated inside blend
    blend = WeightedObjective("blend", ((1.0, on_time), (0.01, makespan)))
    evaluation = _evaluation({}, 90.0, compiled)
    # score = 1*90 + 0.01*(-1000) = 90 - 10 = 80
    assert blend.score(evaluation) == pytest.approx(80.0)


def test_labor_cost_uses_headcount_and_makespan(compiled: object) -> None:
    evaluation = _evaluation({}, 90.0, compiled)  # completion 1000s -> 1000/3600 hr
    headcount = sum(p.headcount for p in evaluation.compiled.labor_pools)
    expected = headcount * 40.0 * (1000.0 / 3600.0)
    assert LaborCost(wage_per_hour=40.0).cost(evaluation) == pytest.approx(expected)


def test_capacity_cost_prices_installed_machines(compiled: object) -> None:
    evaluation = _evaluation({}, 90.0, compiled)
    installed = sum(spec.capacity for spec in evaluation.compiled.locations)
    assert CapacityCost(cost_per_machine=1000.0).cost(evaluation) == installed * 1000.0


def test_lateness_penalty_charges_only_tardy(compiled: object) -> None:
    kpis = _kpis(50.0, lateness={"a": 3600.0, "b": -100.0, "c": None})
    interval = Interval(mean=50.0, lo=48.0, hi=52.0, p50=50.0, n=3)
    aggregated = AggregatedKpis(interval, {}, {}, {}, reps=3, level=0.9)
    evaluation = Evaluation(Scenario(levers={}), kpis, aggregated, compiled)  # type: ignore[arg-type]
    # only +3600s counts = 1 hr * 10/hr = 10
    assert LatenessPenalty(penalty_per_hour=10.0).cost(evaluation) == pytest.approx(10.0)


def test_total_cost_sums_terms(compiled: object) -> None:
    evaluation = _evaluation({}, 90.0, compiled)
    total = TotalCost((LaborCost(40.0), CapacityCost(100.0)))
    expected = LaborCost(40.0).cost(evaluation) + CapacityCost(100.0).cost(evaluation)
    assert total.cost(evaluation) == pytest.approx(expected)


def test_cost_objective_minimises(compiled: object) -> None:
    obj = CostObjective("labour", LaborCost(40.0))
    assert obj.direction == "min"
    assert obj.score(_evaluation({}, 90.0, compiled)) > 0


def _inventory_evaluation(
    compiled: object,
    horizon: float,
    average_level: dict[str, float],
    stockout_seconds: dict[str, float],
) -> Evaluation:
    inventory = InventoryKpis(
        average_level=average_level,
        ending_level={},
        stockout_seconds=stockout_seconds,
        orders_placed={},
        total_ordered={},
    )
    interval = Interval(50.0, 48.0, 52.0, 50.0, 3)
    aggregated = AggregatedKpis(interval, {}, {}, {}, reps=3, level=0.9)
    return Evaluation(
        Scenario(levers={}), _kpis(50.0), aggregated, compiled,  # type: ignore[arg-type]
        horizon=horizon, inventory=inventory,
    )


def test_inventory_holding_cost(compiled: object) -> None:
    from twinflow.modules import InventoryHoldingCost

    # avg level 100 units, held over 3600 s = 1 hr, at $2/unit/hr = $200
    evaluation = _inventory_evaluation(compiled, 3600.0, {"resin": 100.0}, {})
    assert InventoryHoldingCost(rate_per_unit_hour=2.0).cost(evaluation) == pytest.approx(200.0)


def test_stockout_penalty(compiled: object) -> None:
    from twinflow.modules import StockoutPenalty

    # 7200 s = 2 hr empty at $50/hr = $100
    evaluation = _inventory_evaluation(compiled, 10000.0, {}, {"resin": 7200.0})
    assert StockoutPenalty(penalty_per_hour=50.0).cost(evaluation) == pytest.approx(100.0)


def test_service_level_objective(compiled: object) -> None:
    # one stock empty 25% of a 1000 s horizon -> 75% service level
    evaluation = _inventory_evaluation(compiled, 1000.0, {"resin": 40.0}, {"resin": 250.0})
    assert OBJECTIVES.create("service_level").score(evaluation) == pytest.approx(75.0)


def test_service_level_no_stocks_is_full(compiled: object) -> None:
    assert OBJECTIVES.create("service_level").score(_evaluation({}, 50.0, compiled)) == 100.0


# ------------------------------------------------------------------------- optimizers
def _space() -> LeverSpace:
    return LeverSpace({_HEADCOUNT: IntRange(1, 8)})


def test_grid_search_finds_parabola_peak(compiled: object) -> None:
    surface = FakeSurface(compiled)
    result = GridSearch().optimize(surface, _space(), OBJECTIVES.create("on_time_pct"), budget=20)
    assert result.best.scenario.levers[_HEADCOUNT] == 4
    assert result.best_score == pytest.approx(100.0)


def test_grid_search_respects_budget_and_caches(compiled: object) -> None:
    surface = FakeSurface(compiled)
    result = GridSearch().optimize(surface, _space(), OBJECTIVES.create("on_time_pct"), budget=3)
    assert result.evaluations_used == 3  # stopped at budget, not all 8 grid points
    assert len(surface.evaluated) == 3


def test_hill_climb_finds_peak(compiled: object) -> None:
    surface = FakeSurface(compiled)
    result = HillClimb().optimize(
        surface, _space(), OBJECTIVES.create("on_time_pct"), budget=40, seed=1
    )
    assert result.best.scenario.levers[_HEADCOUNT] == 4


def test_random_search_respects_budget(compiled: object) -> None:
    surface = FakeSurface(compiled)
    result = RandomSearch().optimize(
        surface, _space(), OBJECTIVES.create("on_time_pct"), budget=6, seed=2
    )
    assert result.evaluations_used <= 6
    assert isinstance(result, OptimizationResult)


def test_genetic_search_finds_peak(compiled: object) -> None:
    surface = FakeSurface(compiled)
    result = GeneticSearch(population=6).optimize(
        surface, _space(), OBJECTIVES.create("on_time_pct"), budget=40, seed=3
    )
    assert result.best.scenario.levers[_HEADCOUNT] == 4


def test_optimizer_rejects_zero_budget(compiled: object) -> None:
    with pytest.raises(ValueError, match="budget"):
        GridSearch().optimize(FakeSurface(compiled), _space(), OBJECTIVES.create("on_time_pct"), 0)


def test_all_optimizers_registered() -> None:
    assert set(OPTIMIZERS.names()) == {"grid", "random", "hill_climb", "genetic"}


# ------------------------------------------------------------------------------ models
def _linear_training(compiled: object) -> list[Evaluation]:
    # on_time = 10 * headcount, a clean line the linear surrogate must recover.
    return [_evaluation({_HEADCOUNT: h}, 10.0 * h, compiled) for h in range(1, 6)]


def test_linear_surrogate_recovers_line(compiled: object) -> None:
    model = LinearSurrogate()
    model.fit(_linear_training(compiled), OBJECTIVES.create("on_time_pct"))
    assert model.predict({_HEADCOUNT: 10}) == pytest.approx(100.0, abs=1e-6)


def test_nearest_neighbor_returns_closest(compiled: object) -> None:
    model = NearestNeighborSurrogate()
    model.fit(_linear_training(compiled), OBJECTIVES.create("on_time_pct"))
    # nearest to 3.4 is headcount 3 -> on_time 30
    assert model.predict({_HEADCOUNT: 3}) == pytest.approx(30.0)


def test_surrogate_predict_before_fit_raises() -> None:
    with pytest.raises(RuntimeError):
        LinearSurrogate().predict({_HEADCOUNT: 3})


def test_surrogate_fit_empty_raises(compiled: object) -> None:
    with pytest.raises(ValueError):
        LinearSurrogate().fit([], OBJECTIVES.create("on_time_pct"))


def test_predicted_ranking_covers_grid(compiled: object) -> None:
    model = LinearSurrogate()
    model.fit(_linear_training(compiled), OBJECTIVES.create("on_time_pct"))
    ranking = predicted_ranking(model, _space())
    assert len(ranking) == 8  # one per grid point


def test_models_registered() -> None:
    assert set(MODELS.names()) == {"linear", "nearest_neighbor"}
