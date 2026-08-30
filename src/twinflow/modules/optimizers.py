"""Optimizers — search the lever space for a scenario an objective likes.

Every optimizer takes the same four things (a `ScoringSurface`, a `LeverSpace`, an
`Objective`, an evaluation `budget`) and returns the same `OptimizationResult`, so they
are interchangeable behind one registry. They *propose*; they never commit a change to
a real floor — the README's stance ("agents propose, the twin scores, humans decide").

Four are built in, from simplest to smartest:
- **grid** — enumerate the whole space (bounded by budget);
- **random** — sample the space uniformly;
- **hill_climb** — local search with random restarts;
- **genetic** — a small mutation/crossover GA.

All four share one `_Evaluator` that caches by lever-point and enforces the budget, so
the same scenario is never simulated twice and every optimizer counts evaluations the
same way. New optimizers register in `OPTIMIZERS`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np

from twinflow.modules.objectives import Direction, Objective, is_better
from twinflow.modules.registry import Registry
from twinflow.modules.space import LeverSpace
from twinflow.modules.surface import Evaluation, Scenario, ScoringSurface


@dataclass(frozen=True)
class OptimizationResult:
    """The search outcome: the winning evaluation, its score, and the full trail.

    `history` is every (scenario, score) in evaluation order — the convergence
    trace a UI plots. `evaluations` holds the corresponding `Evaluation`s so a
    caller can inspect any scored scenario's KPIs and confidence band, not just
    the winner. The surface picks no winner on its own; the objective ranks, and
    this records — the decision still belongs to the human reading it.
    """

    best: Evaluation
    best_score: float
    objective_name: str
    direction: str
    history: list[tuple[Scenario, float]]
    evaluations: list[Evaluation] = field(repr=False)

    @property
    def evaluations_used(self) -> int:
        return len(self.history)


class _Evaluator:
    """Caches evaluations by lever-point and enforces the budget once for everyone."""

    # A stochastic optimizer over a discrete space smaller than its budget would
    # never reach `budget` fresh evaluations (every point becomes a cache hit), so
    # a plain `misses >= budget` loop guard could spin forever. This hard ceiling on
    # total attempts (misses + cache hits) guarantees termination for every
    # optimizer with no per-optimizer bookkeeping; cache hits are microsecond-cheap,
    # so spinning up to the ceiling on a tiny space costs nothing.
    _ATTEMPT_MULTIPLIER = 20

    def __init__(self, surface: ScoringSurface, objective: Objective, budget: int) -> None:
        if budget < 1:
            raise ValueError("budget must be a positive integer")
        self._surface = surface
        self._objective = objective
        self._budget = budget
        self._attempt_cap = budget * self._ATTEMPT_MULTIPLIER
        self._attempts = 0
        self._cache: dict[tuple[tuple[str, Any], ...], tuple[Evaluation, float]] = {}
        self.history: list[tuple[Scenario, float]] = []
        self.evaluations: list[Evaluation] = []

    @property
    def exhausted(self) -> bool:
        """True once `budget` fresh evaluations are spent OR the attempt ceiling is
        hit (the space is too small to reach budget) — either way, stop."""
        return len(self.history) >= self._budget or self._attempts >= self._attempt_cap

    def score(self, levers: dict[str, Any]) -> tuple[Evaluation, float]:
        """Evaluate `levers` (or return the cached result). A cache hit does not
        spend budget; a fresh evaluation does and is appended to the trail. Every
        call — hit or miss — counts against the attempt ceiling."""
        self._attempts += 1
        key = tuple(sorted(levers.items()))
        if key in self._cache:
            return self._cache[key]
        evaluation = self._surface.evaluate(Scenario(levers=dict(levers)))
        value = self._objective.score(evaluation)
        self._cache[key] = (evaluation, value)
        self.history.append((evaluation.scenario, value))
        self.evaluations.append(evaluation)
        return evaluation, value

    def result(self) -> OptimizationResult:
        direction = self._objective.direction
        best_index = self._best_index(direction)
        best_evaluation = self.evaluations[best_index]
        best_score = self.history[best_index][1]
        return OptimizationResult(
            best=best_evaluation,
            best_score=best_score,
            objective_name=self._objective.name,
            direction=direction,
            history=list(self.history),
            evaluations=list(self.evaluations),
        )

    def _best_index(self, direction: Direction) -> int:
        best_index = 0
        best_value = self.history[0][1]
        for index, (_scenario, value) in enumerate(self.history):
            if is_better(direction, value, best_value):
                best_value = value
                best_index = index
        return best_index


@runtime_checkable
class Optimizer(Protocol):
    """Search a `LeverSpace` under an `Objective`, within an evaluation `budget`.

    `name` is a read-only protocol member (a property) so `frozen=True`
    optimizer dataclasses satisfy the protocol with a plain field.
    """

    @property
    def name(self) -> str: ...

    def optimize(
        self,
        surface: ScoringSurface,
        space: LeverSpace,
        objective: Objective,
        budget: int,
        seed: int = 0,
    ) -> OptimizationResult: ...


@dataclass(frozen=True)
class GridSearch:
    """Enumerate the space in order, evaluating up to `budget` points."""

    name: str = "grid"

    def optimize(
        self,
        surface: ScoringSurface,
        space: LeverSpace,
        objective: Objective,
        budget: int,
        seed: int = 0,
    ) -> OptimizationResult:
        evaluator = _Evaluator(surface, objective, budget)
        for point in space.grid():
            if evaluator.exhausted:
                break
            evaluator.score(point)
        return evaluator.result()


@dataclass(frozen=True)
class RandomSearch:
    """Draw `budget` points uniformly from the space (seeded, reproducible)."""

    name: str = "random"

    def optimize(
        self,
        surface: ScoringSurface,
        space: LeverSpace,
        objective: Objective,
        budget: int,
        seed: int = 0,
    ) -> OptimizationResult:
        evaluator = _Evaluator(surface, objective, budget)
        rng = np.random.default_rng(seed)
        while not evaluator.exhausted:
            evaluator.score(space.sample(rng))
        return evaluator.result()


@dataclass(frozen=True)
class HillClimb:
    """Greedy local search with random restarts.

    From a random start, evaluate every one-step neighbour and move to the best
    improving one; when no neighbour improves, restart from a fresh random point.
    Simple, and strong on the smooth staffing/capacity surfaces this twin
    produces.
    """

    name: str = "hill_climb"

    def optimize(
        self,
        surface: ScoringSurface,
        space: LeverSpace,
        objective: Objective,
        budget: int,
        seed: int = 0,
    ) -> OptimizationResult:
        evaluator = _Evaluator(surface, objective, budget)
        rng = np.random.default_rng(seed)
        direction = objective.direction
        while not evaluator.exhausted:
            current = space.sample(rng)
            _evaluation, current_score = evaluator.score(current)
            improved = True
            while improved and not evaluator.exhausted:
                improved = False
                best_neighbour = current
                best_score = current_score
                for neighbour in space.neighbors(current):
                    if evaluator.exhausted:
                        break
                    _neighbour_eval, value = evaluator.score(neighbour)
                    if is_better(direction, value, best_score):
                        best_score = value
                        best_neighbour = neighbour
                        improved = True
                current, current_score = best_neighbour, best_score
        return evaluator.result()


@dataclass(frozen=True)
class GeneticSearch:
    """A small generational GA: tournament selection, uniform crossover, one-step
    mutation, elitism. Good when the surface has interacting levers a pure hill
    climb gets stuck on."""

    name: str = "genetic"
    population: int = 8
    mutation_rate: float = 0.3

    def optimize(
        self,
        surface: ScoringSurface,
        space: LeverSpace,
        objective: Objective,
        budget: int,
        seed: int = 0,
    ) -> OptimizationResult:
        evaluator = _Evaluator(surface, objective, budget)
        rng = np.random.default_rng(seed)
        direction = objective.direction

        pop = [space.sample(rng) for _ in range(self.population)]
        while not evaluator.exhausted:
            scored: list[tuple[dict[str, Any], float]] = []
            for individual in pop:
                if evaluator.exhausted:
                    break
                _evaluation, value = evaluator.score(individual)
                scored.append((individual, value))
            if evaluator.exhausted or not scored:
                break
            pop = self._next_generation(scored, space, rng, direction)
        return evaluator.result()

    def _next_generation(
        self,
        scored: list[tuple[dict[str, Any], float]],
        space: LeverSpace,
        rng: np.random.Generator,
        direction: Direction,
    ) -> list[dict[str, Any]]:
        ranked = sorted(
            scored,
            key=lambda item: item[1],
            reverse=(direction == "max"),
        )
        elite = ranked[0][0]
        children = [elite]
        while len(children) < self.population:
            parent_a = self._tournament(scored, rng, direction)
            parent_b = self._tournament(scored, rng, direction)
            child = self._crossover(parent_a, parent_b, space, rng)
            child = self._mutate(child, space, rng)
            children.append(space.clamp(child))
        return children

    def _tournament(
        self,
        scored: list[tuple[dict[str, Any], float]],
        rng: np.random.Generator,
        direction: Direction,
    ) -> dict[str, Any]:
        a = scored[rng.integers(len(scored))]
        b = scored[rng.integers(len(scored))]
        if is_better(direction, a[1], b[1]):
            return a[0]
        return b[0]

    def _crossover(
        self,
        a: dict[str, Any],
        b: dict[str, Any],
        space: LeverSpace,
        rng: np.random.Generator,
    ) -> dict[str, Any]:
        return {key: (a[key] if rng.random() < 0.5 else b[key]) for key in space.domains}

    def _mutate(
        self, individual: dict[str, Any], space: LeverSpace, rng: np.random.Generator
    ) -> dict[str, Any]:
        mutated = dict(individual)
        for key, domain in space.domains.items():
            if rng.random() < self.mutation_rate:
                mutated[key] = domain.sample(rng)
        return mutated


OPTIMIZERS: Registry[Optimizer] = Registry("optimizer")
OPTIMIZERS.register("grid", GridSearch)
OPTIMIZERS.register("random", RandomSearch)
OPTIMIZERS.register("hill_climb", HillClimb)
OPTIMIZERS.register("genetic", GeneticSearch)
