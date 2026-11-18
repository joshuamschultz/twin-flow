"""T-264: any engine as a reinforcement-learning environment (COMP-164, REQ-224, D-154).

`Environment(engine, observe)` follows Gymnasium's shape (`reset(seed) -> (obs, info)`,
`step(action) -> (obs, reward, terminated, truncated, info)`) without importing it. The
observation is a one-hot of a view's label or a vector of feature values. A tabular
Q-learner on the molding press over the `family` view, choosing which family to run,
beats the random rule on seeds it never trained on.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from twinflow.engines.environment import (
    Environment,
    FeatureObservation,
    ViewObservation,
    family_actions,
    grouped_labels,
)
from twinflow.engines.step.engine import StepEngine
from twinflow.engines.step.model import StepModel, parse_step_model
from twinflow.scenario import ScenarioCapsule
from twinflow.state.groups import GroupedView
from twinflow.state.records import Ledger, Move, Snapshot

ROOT = Path(__file__).resolve().parents[2]
CAPSULE = ROOT / "examples" / "molding-press" / "molding-press.twin.yaml"


@pytest.fixture(scope="module")
def press() -> StepModel:
    model, issues = parse_step_model(ScenarioCapsule.load(CAPSULE).model)
    assert model is not None, issues
    return model


def _view_observation(model: StepModel, name: str = "simple") -> ViewObservation:
    view = model.views[name]
    families = {p.id: p.family for p in model.products}
    rates = {p.id: p.demand_rate for p in model.products}

    def label(snapshot: Snapshot) -> str:
        return view.label(snapshot, rates, families)

    return ViewObservation(label, grouped_labels(view, model.product_ids, families))


class Counter:
    """A tiny custom engine: count up by the action, stop at 5 steps."""

    name = "counter"

    def start(self, seed: int, from_state: Snapshot | None = None) -> Snapshot:
        self.state = Snapshot(t=0.0, period=0, stock={"n": float(seed % 3)})
        return self.state

    def actions(self, state: Snapshot) -> tuple[str, ...]:
        return ("up", "stay")

    def step(self, action: str) -> Move:
        before = self.state
        n = before.stock["n"] + (1.0 if action == "up" else 0.0)
        after = Snapshot(t=before.t + 1, period=before.period + 1, stock={"n": n})
        self.state = after
        ledger = Ledger(revenue=n)
        return Move(before.period, before, action, None, "period", {}, after, ledger)

    def done(self) -> bool:
        return self.state.period >= 5


def test_reset_and_step_follow_the_gymnasium_shape(press: StepModel) -> None:
    env = Environment(StepEngine(press), _view_observation(press))
    obs, info = env.reset(seed=4)
    assert isinstance(obs, np.ndarray) and obs.shape == (len(env.observation_names),)
    assert obs.sum() == 1.0 and env.observation_names[int(obs.argmax())] == info["label"]
    assert env.action_names == (*press.product_ids, "idle")
    obs2, reward, terminated, truncated, info2 = env.step("idle")
    assert isinstance(reward, float) and reward == info2["move"].ledger.total
    assert terminated is False and truncated is False
    assert info2["move"].action == "idle"
    _, _, _, _, info3 = env.step(0)  # an action number picks from action_names
    assert info3["move"].action == press.product_ids[0]


def test_the_same_seed_gives_the_same_episode(press: StepModel) -> None:
    def rollout(seed: int) -> list[float]:
        env = Environment(StepEngine(press), _view_observation(press))
        env.reset(seed=seed)
        rewards, done = [], False
        while not done:
            _, reward, done, _, _ = env.step("idle")
            rewards.append(reward)
        return rewards

    assert rollout(7) == rollout(7) and rollout(7) != rollout(8)
    assert len(rollout(7)) == press.horizon


def test_a_thousand_random_steps_never_raise(press: StepModel) -> None:
    env = Environment(StepEngine(press), _view_observation(press))
    rng = np.random.default_rng(0)
    env.reset(seed=0)
    labels = set(env.observation_names)
    for step in range(1000):
        obs, _, terminated, truncated, info = env.step(int(rng.integers(env.action_count)))
        assert obs.sum() == 1.0
        assert info["label"] in labels  # every label the view makes is in the list
        if terminated or truncated:
            env.reset(seed=step)


def test_a_custom_engine_and_a_feature_vector() -> None:
    def features(snapshot: Snapshot) -> dict[str, float]:
        return {"n": snapshot.stock["n"], "day": float(snapshot.period)}

    env = Environment(Counter(), FeatureObservation(features), reward=lambda m: -m.ledger.total)
    obs, info = env.reset(seed=2)
    assert env.observation_names == ("day", "n")  # sorted names
    assert obs.tolist() == [0.0, 2.0]
    assert env.action_names == ("up", "stay")
    total, done = 0.0, False
    while not done:
        obs, reward, done, truncated, info = env.step("up")
        total += reward
    assert obs.tolist() == [5.0, 7.0] and total == -(3 + 4 + 5 + 6 + 7)


def test_bad_actions_and_steps_after_the_end_are_plain_errors() -> None:
    env = Environment(Counter(), FeatureObservation(lambda s: {"n": s.stock["n"]}))
    with pytest.raises(RuntimeError, match="reset"):
        env.step("up")
    env.reset(seed=0)
    with pytest.raises(ValueError, match="up, stay"):
        env.step("down")
    with pytest.raises(ValueError, match="0 to 1"):
        env.step(5)
    for _ in range(5):
        env.step("stay")
    with pytest.raises(RuntimeError, match="reset"):
        env.step("stay")


def test_max_steps_truncates() -> None:
    env = Environment(Counter(), FeatureObservation(lambda s: {"n": 0.0}), max_steps=2)
    env.reset(seed=0)
    assert env.step("up")[3] is False
    assert env.step("up")[3] is True


def test_grouped_labels_list_every_label_of_a_view(press: StepModel) -> None:
    view: GroupedView = press.views["family"]
    families = {p.id: p.family for p in press.products}
    labels = grouped_labels(view, press.product_ids, families)
    assert len(labels) == 3**3 * 3 and len(set(labels)) == len(labels)
    assert "grey:low black:ok natural:high | grey" in labels


def test_no_gymnasium_import_and_no_removed_module_names() -> None:
    source = (ROOT / "src" / "twinflow" / "engines" / "environment.py").read_text()
    assert "gymnasium" not in source.replace("Gymnasium", "")
    assert "adapters.rl" not in source and "_gym" not in source
    assert importlib.util.find_spec("twinflow.adapters.rl") is None


def _press_actions(press: StepModel) -> dict[str, object]:
    families = {p.id: p.family for p in press.products}
    rates = {p.id: p.demand_rate for p in press.products}
    return family_actions(press.product_ids, families, rates)  # type: ignore[return-value]


def _q_learning(press: StepModel, episodes: int, seed: int) -> tuple[Environment, np.ndarray]:
    """Tabular Q-learning: one row per family-view label, one column per family to run.
    The step size is 1 / visits (at least 0.02) and exploration falls from 100% to 5%."""
    env = Environment(
        StepEngine(press),
        _view_observation(press, "family"),
        reward=_scaled,
        actions=_press_actions(press),  # type: ignore[arg-type]
    )
    rng = np.random.default_rng(seed)
    table = np.zeros((len(env.observation_names), env.action_count))
    visits = np.zeros_like(table)
    gamma = 0.95
    for episode in range(episodes):
        epsilon = max(0.05, 1.0 - episode / (0.7 * episodes))
        obs, _ = env.reset(seed=seed + episode)
        state, done = int(obs.argmax()), False
        while not done:
            if rng.random() < epsilon:
                action = int(rng.integers(env.action_count))
            else:
                action = int(table[state].argmax())
            obs, reward, done, _, _ = env.step(action)
            nxt = int(obs.argmax())
            visits[state, action] += 1
            alpha = max(0.02, 1.0 / visits[state, action])
            target = reward + (0.0 if done else gamma * table[nxt].max())
            table[state, action] += alpha * (target - table[state, action])
            state = nxt
    return env, table


def _scaled(move: Move) -> float:
    return move.ledger.total / 1000.0


def _mean_profit(env: Environment, choose: object, seeds: range) -> float:
    totals = []
    for seed in seeds:
        obs, _ = env.reset(seed=seed)
        total, done = 0.0, False
        while not done:
            action = choose(obs)  # type: ignore[operator]
            obs, _, done, _, info = env.step(action)
            total += info["move"].ledger.total
        totals.append(total)
    return float(np.mean(totals))


def test_q_learning_on_the_press_beats_the_random_rule(press: StepModel) -> None:
    env, table = _q_learning(press, episodes=400, seed=0)
    held_out = range(50_000, 50_030)
    learned = _mean_profit(env, lambda obs: int(table[int(obs.argmax())].argmax()), held_out)
    raw = Environment(StepEngine(press), _view_observation(press, "family"))
    rng = np.random.default_rng(1)
    random_rule = _mean_profit(raw, lambda obs: int(rng.integers(raw.action_count)), held_out)
    untrained = _mean_profit(env, lambda obs: 0, held_out)  # always the first family
    assert learned > random_rule + 10_000
    assert learned > untrained + 10_000  # it learned something, not just the family actions


def test_family_actions_run_the_shortest_product_of_the_family(press: StepModel) -> None:
    env = Environment(
        StepEngine(press),
        _view_observation(press, "family"),
        actions=_press_actions(press),  # type: ignore[arg-type]
    )
    assert env.action_names == ("grey", "black", "natural")
    _, info = env.reset(seed=0)
    stock, rates = info["snapshot"].stock, {p.id: p.demand_rate for p in press.products}
    blacks = [p.id for p in press.products if p.family == "black"]
    shortest = min(blacks, key=lambda p: stock[p] / rates[p])
    _, _, _, _, step = env.step("black")
    assert step["move"].action == shortest and step["action"] == "black"
