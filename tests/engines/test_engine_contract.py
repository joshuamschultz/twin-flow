"""T-227: the engine contract and `run_episode` (COMP-149, REQ-194)."""

from __future__ import annotations

import pytest

from twinflow.engines import Engine, run_episode
from twinflow.state.records import Ledger, Move, Snapshot


class CounterEngine:
    """A toy engine: stock goes up by one on 'up', the episode lasts `horizon` periods."""

    name = "counter"

    def __init__(self, horizon: int = 3) -> None:
        self.horizon = horizon
        self.seeds: list[int] = []
        self._state = Snapshot(t=0.0, period=0)
        self._index = 0

    def start(self, seed: int, from_state: Snapshot | None = None) -> Snapshot:
        self.seeds.append(seed)
        self._state = from_state or Snapshot(t=0.0, period=0, stock={"A": 0.0})
        self._index = 0
        return self._state

    def actions(self, state: Snapshot) -> tuple[str, ...]:
        return ("up", "stay")

    def step(self, action: str) -> Move:
        before = self._state
        units = before.stock["A"] + (1.0 if action == "up" else 0.0)
        after = Snapshot(t=before.t + 1.0, period=before.period + 1, stock={"A": units})
        move = Move(
            self._index, before, action, self.name, "period", {}, after, Ledger(revenue=units)
        )
        self._state, self._index = after, self._index + 1
        return move

    def done(self) -> bool:
        return self._state.period >= self.horizon


def test_toy_engine_meets_the_protocol() -> None:
    assert isinstance(CounterEngine(), Engine)


def test_run_episode_steps_until_done_and_chains_states() -> None:
    engine = CounterEngine(horizon=3)
    seen: list[Snapshot] = []

    def decide(state: Snapshot) -> str:
        seen.append(state)
        return "up"

    moves = run_episode(engine, decide, seed=7)

    assert engine.seeds == [7]
    assert [m.index for m in moves] == [0, 1, 2]
    assert [m.after.stock["A"] for m in moves] == [1.0, 2.0, 3.0]
    assert seen == [m.before for m in moves]
    assert all(a.after == b.before for a, b in zip(moves, moves[1:], strict=False))


def test_run_episode_starts_from_a_given_state() -> None:
    start = Snapshot(t=10.0, period=2, stock={"A": 5.0})
    moves = run_episode(CounterEngine(horizon=3), lambda s: "stay", seed=0, from_state=start)
    assert len(moves) == 1
    assert moves[0].before == start


def test_run_episode_refuses_an_action_the_engine_does_not_offer() -> None:
    with pytest.raises(ValueError, match="'jump'.*up, stay"):
        run_episode(CounterEngine(), lambda s: "jump", seed=0)


def test_run_episode_on_an_engine_already_done_gives_no_moves() -> None:
    start = Snapshot(t=0.0, period=9, stock={"A": 0.0})
    assert run_episode(CounterEngine(horizon=3), lambda s: "up", seed=0, from_state=start) == []
