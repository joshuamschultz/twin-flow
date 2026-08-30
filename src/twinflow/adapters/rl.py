"""Reinforcement-learning surface — the twin as a Gymnasium-style environment.

Training an actual RL agent (stable-baselines, a custom policy) is a separate, later
job; what ships here is the environment it plugs into. `TwinEnv` exposes the modern
Gymnasium API — `reset() -> (obs, info)` and `step(action) -> (obs, reward, terminated,
truncated, info)` — over the same `ScoringSurface` the optimizers use, so an RL loop
learns staffing / capacity / reorder-point policies against a fast, faithful simulator.

Kept dependency-free: the action and observation spaces are described by small local
types that mirror Gymnasium's `MultiDiscrete`/`Box`, and `to_gymnasium()` adapts the env
to a real `gymnasium.Env` when that package is installed. Levers must be integer ranges
(`IntRange`) — the staffing/capacity/reorder knobs RL is for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from twinflow.modules.objectives import Objective
from twinflow.modules.space import IntRange, LeverSpace
from twinflow.modules.surface import Scenario, ScoringSurface

# Per-lever action: 0 = step down, 1 = hold, 2 = step up.
_ACTION_CHOICES = 3


@dataclass(frozen=True)
class MultiDiscrete:
    """A vector of independent discrete choices (mirrors gymnasium.spaces.MultiDiscrete)."""

    nvec: tuple[int, ...]

    def sample(self, rng: np.random.Generator) -> tuple[int, ...]:
        return tuple(int(rng.integers(n)) for n in self.nvec)


@dataclass(frozen=True)
class Box:
    """A continuous box (mirrors gymnasium.spaces.Box)."""

    low: tuple[float, ...]
    high: tuple[float, ...]

    @property
    def shape(self) -> tuple[int]:
        return (len(self.low),)


class TwinEnv:
    """A Gymnasium-style environment: an action nudges each lever up/down/hold, the
    twin scores the resulting scenario, and the reward is an objective's score
    (negated for a `min` objective, so the agent always maximises).

    The observation is the current lever vector. An episode runs `max_steps` steps
    then truncates. The env holds no gymnasium dependency; `to_gymnasium()` wraps it
    when gymnasium is present.
    """

    def __init__(
        self,
        surface: ScoringSurface,
        space: LeverSpace,
        objective: Objective,
        max_steps: int = 10,
    ) -> None:
        domains = space.domains
        if not all(isinstance(domain, IntRange) for domain in domains.values()):
            raise ValueError("TwinEnv supports IntRange levers only")
        self._surface = surface
        self._space = space
        self._objective = objective
        self._max_steps = max_steps
        self._keys = list(domains)
        self._domains: dict[str, IntRange] = {
            key: domain for key, domain in domains.items() if isinstance(domain, IntRange)
        }
        self.action_space = MultiDiscrete((_ACTION_CHOICES,) * len(self._keys))
        self.observation_space = Box(
            low=tuple(float(self._domains[k].low) for k in self._keys),
            high=tuple(float(self._domains[k].high) for k in self._keys),
        )
        self._point: dict[str, int] = {}
        self._steps = 0

    def reset(self, seed: int = 0) -> tuple[np.ndarray, dict[str, Any]]:
        """Start an episode at the midpoint of every lever's range."""
        self._steps = 0
        self._point = {
            key: self._domains[key].clamp((self._domains[key].low + self._domains[key].high) // 2)
            for key in self._keys
        }
        reward, info = self._score()
        return self._observe(), info

    def step(self, action: tuple[int, ...]) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        """Apply a per-lever nudge, score the new scenario, return the Gym 5-tuple."""
        for key, choice in zip(self._keys, action, strict=True):
            domain = self._domains[key]
            delta = (choice - 1) * domain.step  # 0->-step, 1->0, 2->+step
            self._point[key] = domain.clamp(self._point[key] + delta)
        self._steps += 1
        reward, info = self._score()
        truncated = self._steps >= self._max_steps
        return self._observe(), reward, False, truncated, info

    def to_gymnasium(self) -> Any:
        """Adapt this env to a real `gymnasium.Env` (raises if gymnasium is absent)."""
        from twinflow.adapters._gym import build_gym_env

        return build_gym_env(self)

    def _score(self) -> tuple[float, dict[str, Any]]:
        evaluation = self._surface.evaluate(Scenario(levers=dict(self._point)))
        raw = self._objective.score(evaluation)
        reward = raw if self._objective.direction == "max" else -raw
        return reward, {"levers": dict(self._point), "objective_score": raw}

    def _observe(self) -> np.ndarray:
        return np.array([float(self._point[key]) for key in self._keys], dtype=float)
