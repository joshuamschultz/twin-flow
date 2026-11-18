"""T-230: the make-to-stock step engine on a small hand-checked model (COMP-150, REQ-195)."""

from __future__ import annotations

import numpy as np
import pytest

from twinflow.engines.contract import Engine, run_episode
from twinflow.engines.step.engine import StepEngine, demand_draws
from twinflow.engines.step.model import StepModel, parse_step_model
from twinflow.state.records import Snapshot


def _model(horizon: int = 3) -> StepModel:
    model, issues = parse_step_model(
        {
            "domain": "make_to_stock",
            "period": "day",
            "horizon": horizon,
            "start_cover_days": 2,
            "line": {
                "id": "press",
                "changeover": {
                    "within_family": {"cost": 10, "output_lost": 0.1},
                    "across_family": {"cost": 100, "output_lost": 0.5},
                },
            },
            "products": [
                {
                    "id": "A",
                    "family": "f1",
                    "demand": 10,
                    "run_rate": 100,
                    "price": 5,
                    "unit_cost": 2,
                    "max_storage": 50,
                },
                {
                    "id": "B",
                    "family": "f2",
                    "demand": 5,
                    "run_rate": 40,
                    "price": 10,
                    "unit_cost": 4,
                    "max_storage": 30,
                },
                {
                    "id": "C",
                    "family": "f1",
                    "demand": 5,
                    "run_rate": 30,
                    "price": 6,
                    "unit_cost": 3,
                    "max_storage": 100,
                },
            ],
            "economics": {"defaults": {"holding_rate": 0.1, "lost_sale_rate": 0.5}},
        }
    )
    assert model is not None, issues
    return model


def _fixed(*days: tuple[int, int, int]) -> np.ndarray:
    return np.array([days])  # one episode


def test_engine_meets_the_contract() -> None:
    assert isinstance(StepEngine(_model()), Engine)


def test_start_cover_and_actions() -> None:
    engine = StepEngine(_model())
    start = engine.start(1)
    assert dict(start.stock) == {"A": 20.0, "B": 10.0, "C": 10.0}
    assert dict(start.setup) == {"press": "A"}
    assert (start.t, start.period) == (0.0, 0)
    assert engine.actions(start) == ("A", "B", "C", "idle")


def test_start_cover_is_capped_by_storage() -> None:
    model = _model()
    assert StepEngine(model).start(0).stock["A"] == min(50, round(model.start_cover_days * 10))


def test_cross_family_changeover_by_hand() -> None:
    engine = StepEngine(_model(), _fixed((12, 3, 0), (0, 0, 0), (0, 0, 0)))
    state = engine.start(0)
    move = engine.step("B")
    # B from A is across families: half of 40 = 20 made, room 20. sold 12 A and 3 B.
    assert dict(move.events) == {
        "demand.A": 12.0, "made.A": 0.0, "sold.A": 12.0, "lost.A": 0.0,
        "demand.B": 3.0, "made.B": 20.0, "sold.B": 3.0, "lost.B": 0.0,
        "demand.C": 0.0, "made.C": 0.0, "sold.C": 0.0, "lost.C": 0.0,
    }  # fmt: skip
    assert move.before == state
    assert dict(move.after.stock) == {"A": 8.0, "B": 27.0, "C": 10.0}
    assert dict(move.after.setup) == {"press": "B"}
    assert (move.after.period, move.after.t) == (1, 1.0)
    ledger = move.ledger
    assert ledger.revenue == pytest.approx(12 * 5 + 3 * 10)
    assert ledger.production == pytest.approx(20 * 4)
    assert ledger.setup == pytest.approx(100)
    assert ledger.holding == pytest.approx(0.1 * 2 * 8 + 0.1 * 4 * 27 + 0.1 * 3 * 10)
    assert ledger.lost_sales == 0.0 and ledger.late == 0.0
    assert (move.action, move.actor, move.kind, move.index) == ("B", "press", "period", 0)


def test_within_family_changeover_loses_ten_percent() -> None:
    engine = StepEngine(_model(), np.zeros((1, 3, 3), dtype=int))
    engine.start(0)
    move = engine.step("C")  # C is in A's family: 10% lost, round(30 x 0.9) = 27 made
    assert move.events["made.C"] == 27.0
    assert move.ledger.setup == 10.0
    assert move.ledger.production == pytest.approx(27 * 3)


def test_no_changeover_and_full_run_when_staying_on_a_product() -> None:
    engine = StepEngine(_model(), np.full((1, 3, 3), 20))
    engine.start(0)
    move = engine.step("A")  # room 50 - 20 = 30 caps the 100-unit run
    assert move.events["made.A"] == 30.0
    assert move.ledger.setup == 0.0


def test_unmet_demand_is_lost_and_costs_a_penalty() -> None:
    engine = StepEngine(_model(), _fixed((30, 0, 0), (0, 0, 0), (0, 0, 0)))
    engine.start(0)
    move = engine.step("idle")  # A has 20: 20 sold, 10 lost at 0.5 x $5
    assert move.events["sold.A"] == 20.0 and move.events["lost.A"] == 10.0
    assert move.ledger.lost_sales == pytest.approx(25.0)
    assert dict(move.after.setup) == {"press": "A"}  # idle keeps the setup
    assert move.after.stock["A"] == 0.0


def test_idle_keeps_the_setup_and_makes_nothing() -> None:
    engine = StepEngine(_model(), np.zeros((1, 3, 3), dtype=int))
    engine.start(0)
    engine.step("B")
    move = engine.step("idle")
    assert dict(move.after.setup) == {"press": "B"}
    assert all(move.events[f"made.{p}"] == 0.0 for p in "ABC")
    assert move.ledger.production == 0.0 and move.ledger.setup == 0.0


def test_done_after_horizon_and_step_then_raises() -> None:
    engine = StepEngine(_model(horizon=2), np.zeros((1, 2, 3), dtype=int))
    engine.start(0)
    assert not engine.done()
    engine.step("idle")
    engine.step("idle")
    assert engine.done()
    with pytest.raises(ValueError, match="finished"):
        engine.step("idle")


def test_step_before_start_and_unknown_action_are_plain_errors() -> None:
    engine = StepEngine(_model())
    with pytest.raises(ValueError, match="start"):
        engine.step("idle")
    engine.start(0)
    with pytest.raises(ValueError, match=r"'Z'.*A, B, C, idle"):
        engine.step("Z")


def test_demand_draws_copy_the_rl_numpy_call() -> None:
    model = _model()
    draws = demand_draws(model, 4, 2024)
    expected = np.random.default_rng(2024).poisson([10.0, 5.0, 5.0], size=(4, 3, 3))
    assert draws.shape == (4, 3, 3)
    assert (draws == expected).all()


def test_without_draws_the_seed_makes_one_episode_of_dice() -> None:
    model = _model()
    first = run_episode(StepEngine(model), lambda s: "idle", 7)
    again = run_episode(StepEngine(model), lambda s: "idle", 7)
    other = run_episode(StepEngine(model), lambda s: "idle", 8)
    assert first == again
    assert first != other
    one = demand_draws(model, 1, 7)[0]
    assert [first[d].events["demand.A"] for d in range(3)] == [float(x) for x in one[:, 0]]


def test_with_draws_the_seed_picks_the_episode_row() -> None:
    model = _model()
    draws = demand_draws(model, 5, 11)
    engine = StepEngine(model, draws)
    moves = run_episode(engine, lambda s: "idle", 3)
    assert [m.events["demand.B"] for m in moves] == [float(x) for x in draws[3, :, 1]]
    with pytest.raises(ValueError, match="episode"):
        engine.start(5)


def test_start_from_a_saved_snapshot_continues_the_day_count() -> None:
    model = _model(horizon=3)
    engine = StepEngine(model, np.zeros((1, 3, 3), dtype=int))
    saved = Snapshot(t=1.0, period=1, stock={"A": 7, "B": 8, "C": 9}, setup={"press": "C"})
    state = engine.start(0, saved)
    assert state == saved
    moves = []
    while not engine.done():
        moves.append(engine.step("idle"))
    assert len(moves) == 2  # days 1 and 2 are left
    assert moves[-1].after.period == 3
    assert moves[0].before == saved


def test_snapshot_from_the_engine_round_trips_as_a_start_state() -> None:
    model = _model()
    engine = StepEngine(model, np.zeros((1, 3, 3), dtype=int))
    engine.start(0)
    middle = engine.step("B").after
    resumed = StepEngine(model, np.zeros((1, 3, 3), dtype=int))
    assert resumed.start(0, middle) == middle
    assert resumed.step("idle").index == 1


@pytest.mark.parametrize(
    ("snapshot", "words"),
    [
        (Snapshot(t=0, period=0, stock={"A": 1}, setup={"press": "A"}), "B"),
        (
            Snapshot(t=0, period=0, stock={"A": 1, "B": 1, "C": 1, "Z": 1}, setup={"press": "A"}),
            "Z",
        ),
        (Snapshot(t=0, period=0, stock={"A": 1.5, "B": 1, "C": 1}, setup={"press": "A"}), "whole"),
        (Snapshot(t=0, period=0, stock={"A": 1, "B": 1, "C": 1}, setup={"press": "Q"}), "setup"),
        (Snapshot(t=0, period=0, stock={"A": 1, "B": 1, "C": 1}, setup={"oven": "A"}), "press"),
        (Snapshot(t=0, period=9, stock={"A": 1, "B": 1, "C": 1}, setup={"press": "A"}), "period"),
    ],
)
def test_a_bad_start_snapshot_is_refused_in_plain_words(snapshot: Snapshot, words: str) -> None:
    with pytest.raises(ValueError, match=words):
        StepEngine(_model()).start(0, snapshot)


def test_run_episode_refuses_an_action_the_engine_does_not_offer() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        run_episode(StepEngine(_model()), lambda s: "Z", 0)
