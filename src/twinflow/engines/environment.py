"""COMP-164: any engine as a reinforcement-learning environment (REQ-224, D-154).

`Environment(engine, observe, reward=None)` wraps an engine that meets the contract
(REQ-194, built-in or a company's own engine file) in the shape Gymnasium uses, without
importing Gymnasium (D-154), so an RL library plugs in with a two-line adapter:

- `reset(seed=None) -> (observation, info)`
- `step(action) -> (observation, reward, terminated, truncated, info)`
- `action_names` (the engine's actions in the first state; `step` takes a name or its
  number), `action_count`, `observation_names`

The observation is a numpy vector built by one of two observers:

- `ViewObservation(label, labels)`: a one-hot of the state's label under a view (a grouped
  view or a view file). `labels` lists every label the view can make (`grouped_labels`
  lists them for a grouped view); a label outside the list lights an extra `other` slot.
- `FeatureObservation(features, names=None)`: feature values in `names` order (default:
  the names of the first state, sorted).

The reward is the move's dollar ledger total unless `reward(move)` is given (a reward
extension, for example). `info` holds the `move`, the state's `label` (view observers),
the `snapshot`, the allowed `actions` and the `action` name taken.

`actions` may replace the engine's own actions with named choices that each pick an
engine action from the state (`family_actions`: "run this family", and the family's product
with the fewest days of stock runs). A table over a grouped view cannot tell which product
in a family is short (the aliasing the RL repo's notebook 2 shows), so a learner that picks
products from such a view gets stuck on one product; family-level choices remove that.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from twinflow.engines.contract import Engine
from twinflow.state.groups import GroupedView
from twinflow.state.records import Move, Snapshot

Observation = npt.NDArray[np.float64]
OTHER = "other"
Choice = Callable[[Snapshot], str]
"""A named action that picks the engine action from the state."""


class Observer(Protocol):
    """Turns a snapshot into the observation vector."""

    def names(self, first: Snapshot) -> tuple[str, ...]: ...

    def __call__(self, snapshot: Snapshot) -> tuple[Observation, dict[str, Any]]: ...


@dataclass
class ViewObservation:
    """One-hot of a view's label (D-154). `labels` lists every label the view can make."""

    label: Callable[[Snapshot], str]
    labels: Sequence[str]

    def __post_init__(self) -> None:
        self._index = {name: i for i, name in enumerate(self.labels)}
        if len(self._index) != len(self.labels):
            raise ValueError("the labels of a view observation must be different")

    def names(self, first: Snapshot) -> tuple[str, ...]:
        return (*self.labels, OTHER)

    def __call__(self, snapshot: Snapshot) -> tuple[Observation, dict[str, Any]]:
        name = self.label(snapshot)
        vector = np.zeros(len(self.labels) + 1)
        vector[self._index.get(name, len(self.labels))] = 1.0
        return vector, {"label": name}


@dataclass
class FeatureObservation:
    """Feature values as a vector, in `names` order (default: the first state's names,
    sorted). A missing feature reads 0."""

    features: Callable[[Snapshot], Mapping[str, float]]
    order: Sequence[str] | None = None

    def names(self, first: Snapshot) -> tuple[str, ...]:
        if self.order is None:
            self.order = tuple(sorted(self.features(first)))
        return tuple(self.order)

    def __call__(self, snapshot: Snapshot) -> tuple[Observation, dict[str, Any]]:
        values = self.features(snapshot)
        order = self.order if self.order is not None else tuple(sorted(values))
        return np.array([float(values.get(n, 0.0)) for n in order]), {"features": dict(values)}


def grouped_labels(
    view: GroupedView, products: Sequence[str], families: Mapping[str, str]
) -> tuple[str, ...]:
    """Every label a grouped view can make for these products, in a fixed order (setup
    slowest, then each stock key, first key slowest). The exact `full` view has too many to
    list and raises `ValueError`."""
    if view.exact:
        raise ValueError(f"the {view.name} view labels exact stock; it has too many labels")
    stock_part: list[str] = [""]
    groups = view.stock
    if groups is not None:
        things = groups.things if groups.things is not None else tuple(products)
        keys = list(
            dict.fromkeys(families.get(t, t) if groups.aggregate == "family" else t for t in things)
        )
        names = [level.name for level in groups.levels]
        stock_part = [
            " ".join(f"{key}:{level}" for key, level in zip(keys, combo, strict=True))
            for combo in itertools.product(names, repeat=len(keys))
        ]
    setups: list[str] = [""]
    if view.setup == "thing":
        setups = list(products)
    elif view.setup == "family":
        setups = list(dict.fromkeys(families.get(p, p) for p in products))
    return tuple(
        " | ".join(part for part in (stock, setup) if part)
        for setup in setups
        for stock in stock_part
    )


def family_actions(
    products: Sequence[str], families: Mapping[str, str], demand_rate: Mapping[str, float]
) -> dict[str, Choice]:
    """One action per family: run the family's product with the fewest days of stock (the
    first one on a tie). A product with no demand is never the shortest."""

    def choice(members: tuple[str, ...]) -> Choice:
        def pick(state: Snapshot) -> str:
            def cover(product: str) -> float:
                rate = demand_rate.get(product, 0.0)
                return state.stock.get(product, 0.0) / rate if rate > 0 else float("inf")

            return min(members, key=cover)

        return pick

    groups: dict[str, list[str]] = {}
    for product in products:
        groups.setdefault(families.get(product, product), []).append(product)
    return {family: choice(tuple(members)) for family, members in groups.items()}


class Environment:
    """An engine in Gymnasium's reset/step shape (D-154)."""

    def __init__(
        self,
        engine: Engine,
        observe: Observer,
        *,
        reward: Callable[[Move], float] | None = None,
        actions: Mapping[str, Choice] | None = None,
        max_steps: int | None = None,
        seed: int = 0,
    ) -> None:
        self.engine = engine
        self.observe = observe
        self.reward = reward
        self.choices = dict(actions) if actions is not None else None
        self.max_steps = max_steps
        self._seed = seed
        first = engine.start(seed)
        names = self.choices if self.choices is not None else engine.actions(first)
        self.action_names: tuple[str, ...] = tuple(names)
        self.observation_names: tuple[str, ...] = observe.names(first)
        self._state: Snapshot | None = None
        self._steps = 0

    @property
    def action_count(self) -> int:
        return len(self.action_names)

    def reset(
        self, seed: int | None = None, options: Mapping[str, Any] | None = None
    ) -> tuple[Observation, dict[str, Any]]:
        """Start an episode. The same seed gives the same episode; without one, the next
        seed after the last one is used."""
        if seed is None:
            seed = self._seed + 1
        self._seed = seed
        from_state = (options or {}).get("from_state")
        self._state = self.engine.start(seed, from_state)
        self._steps = 0
        obs, info = self.observe(self._state)
        return obs, {**info, "snapshot": self._state, "actions": self._allowed(self._state)}

    def step(self, action: int | str) -> tuple[Observation, float, bool, bool, dict[str, Any]]:
        """Apply one action (a name, or its number in `action_names`)."""
        state = self._state
        if state is None:
            raise RuntimeError("call reset() before step(), and again after an episode ends")
        name = self._name(action)
        allowed = self._allowed(state)
        if name not in allowed:
            raise ValueError(
                f"action {name!r} is not allowed here; choose one of {', '.join(allowed)}"
            )
        chosen = name if self.choices is None else self.choices[name](state)
        move = self.engine.step(chosen)
        self._steps += 1
        reward = float(move.ledger.total if self.reward is None else self.reward(move))
        terminated = bool(self.engine.done())
        truncated = not terminated and self.max_steps is not None and self._steps >= self.max_steps
        self._state = None if terminated or truncated else move.after
        obs, info = self.observe(move.after)
        allowed_next = self._allowed(move.after)
        info = {
            **info,
            "move": move,
            "snapshot": move.after,
            "actions": allowed_next,
            "action": name,
        }
        return obs, reward, terminated, truncated, info

    def _allowed(self, state: Snapshot) -> tuple[str, ...]:
        if self.choices is not None:
            return self.action_names
        return tuple(self.engine.actions(state))

    def _name(self, action: int | str) -> str:
        if isinstance(action, str):
            return action
        index = int(action)
        if not 0 <= index < self.action_count:
            raise ValueError(
                f"action number {index} is out of range; use 0 to {self.action_count - 1}"
            )
        return self.action_names[index]


__all__ = [
    "Choice",
    "Environment",
    "FeatureObservation",
    "Observer",
    "ViewObservation",
    "family_actions",
    "grouped_labels",
]
