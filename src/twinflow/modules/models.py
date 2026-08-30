"""Surrogate models — learn the scoring surface so search can look before it leaps.

Simulating a scenario is expensive; a surrogate is a cheap stand-in that, once fit on
a handful of real evaluations, predicts an objective's score for any unseen lever
point in microseconds. That powers two things: ranking a whole grid to pick the few
scenarios worth really simulating, and giving an agent a fast inner model.

Two are built in, both numpy-only (no new dependency): a least-squares **linear**
surrogate and a **nearest-neighbour** one. They share a featuriser that turns a lever
point into a numeric vector (numbers pass through; categoricals become stable indices
learned at fit time). New surrogates register in `MODELS`.

A surrogate is an approximation, never the truth — its ranking chooses *what to
simulate*, and the twin still produces the honest, uncertainty-carrying score.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np

from twinflow.modules.objectives import Objective
from twinflow.modules.registry import Registry
from twinflow.modules.space import LeverSpace
from twinflow.modules.surface import Evaluation


class _Featurizer:
    """Turns lever points into a fixed numeric matrix, learned from a fit set.

    Keys are fixed at fit time (sorted). A numeric value is used directly; a
    non-numeric value is mapped to the index of its first appearance among that
    key's fit values, so the same category always yields the same number and an
    unseen category falls back to 0.
    """

    def __init__(self) -> None:
        self._keys: list[str] = []
        self._category_index: dict[str, dict[Any, int]] = {}

    def fit(self, points: Sequence[dict[str, Any]]) -> None:
        self._keys = sorted({key for point in points for key in point})
        self._category_index = {}
        for key in self._keys:
            values = [point.get(key) for point in points]
            if any(not _is_number(value) for value in values):
                seen: dict[Any, int] = {}
                for value in values:
                    if value not in seen:
                        seen[value] = len(seen)
                self._category_index[key] = seen

    def transform(self, points: Sequence[dict[str, Any]]) -> np.ndarray:
        rows = [[self._feature(point.get(key), key) for key in self._keys] for point in points]
        return np.array(rows, dtype=float)

    def _feature(self, value: Any, key: str) -> float:
        if key in self._category_index:
            return float(self._category_index[key].get(value, 0))
        return float(value) if _is_number(value) else 0.0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@runtime_checkable
class SurrogateModel(Protocol):
    """Fit on scored evaluations, then predict an objective score for any levers.

    `name` is a read-only protocol member (a property) so the concrete models
    satisfy it with a plain field.
    """

    @property
    def name(self) -> str: ...

    def fit(self, evaluations: Sequence[Evaluation], objective: Objective) -> None: ...

    def predict(self, levers: dict[str, Any]) -> float: ...


@dataclass
class LinearSurrogate:
    """Ordinary least squares over the featurised levers (with an intercept)."""

    name: str = "linear"
    _featurizer: _Featurizer = field(default_factory=_Featurizer, repr=False)
    _weights: np.ndarray | None = field(default=None, repr=False)

    def fit(self, evaluations: Sequence[Evaluation], objective: Objective) -> None:
        if not evaluations:
            raise ValueError("cannot fit a surrogate on zero evaluations")
        points = [evaluation.scenario.levers for evaluation in evaluations]
        targets = np.array([objective.score(evaluation) for evaluation in evaluations])
        self._featurizer.fit(points)
        design = self._with_intercept(self._featurizer.transform(points))
        self._weights, *_ = np.linalg.lstsq(design, targets, rcond=None)

    def predict(self, levers: dict[str, Any]) -> float:
        if self._weights is None:
            raise RuntimeError("LinearSurrogate.predict called before fit")
        design = self._with_intercept(self._featurizer.transform([levers]))
        return float((design @ self._weights)[0])

    @staticmethod
    def _with_intercept(features: np.ndarray) -> np.ndarray:
        ones = np.ones((features.shape[0], 1))
        return np.hstack([ones, features])


@dataclass
class NearestNeighborSurrogate:
    """Predict the score of the closest fit point (Euclidean over features)."""

    name: str = "nearest_neighbor"
    _featurizer: _Featurizer = field(default_factory=_Featurizer, repr=False)
    _train_x: np.ndarray | None = field(default=None, repr=False)
    _train_y: np.ndarray | None = field(default=None, repr=False)

    def fit(self, evaluations: Sequence[Evaluation], objective: Objective) -> None:
        if not evaluations:
            raise ValueError("cannot fit a surrogate on zero evaluations")
        points = [evaluation.scenario.levers for evaluation in evaluations]
        self._featurizer.fit(points)
        self._train_x = self._featurizer.transform(points)
        self._train_y = np.array([objective.score(evaluation) for evaluation in evaluations])

    def predict(self, levers: dict[str, Any]) -> float:
        if self._train_x is None or self._train_y is None:
            raise RuntimeError("NearestNeighborSurrogate.predict called before fit")
        query = self._featurizer.transform([levers])[0]
        distances = np.linalg.norm(self._train_x - query, axis=1)
        return float(self._train_y[int(np.argmin(distances))])


def predicted_ranking(
    model: SurrogateModel, space: LeverSpace
) -> list[tuple[dict[str, Any], float]]:
    """Rank every grid point of `space` by the surrogate's predicted score, best
    first is left to the caller (it does not know the objective's direction here —
    scores are returned raw). The cheap screen that decides what to simulate next.
    """
    return [(point, model.predict(point)) for point in space.grid()]


MODELS: Registry[SurrogateModel] = Registry("model")
MODELS.register("linear", LinearSurrogate)
MODELS.register("nearest_neighbor", NearestNeighborSurrogate)
