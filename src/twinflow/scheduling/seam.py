"""Solver seam types: `Feature`, `Solver` and `SolveContext` (COMP-001, REQ-072, REQ-087, D-070).

A leaf module: it imports only `twinflow.scheduling.model` and the standard library, so
solver modules (`baseline.py`, `algorithms/*`) import their contract from here and
`solver.py` can import the default solver at module top without a cycle.
`OptionalDependencyError` lives here so surfaces can catch it without importing an
algorithm leaf (D-069).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from twinflow.scheduling.model import ObjectiveSpec, PreviousPlan, Problem, Schedule

ParamValue = float | int | str | bool


class OptionalDependencyError(RuntimeError):
    """Raised when a solver is built without the optional dependency it needs (D-069)."""


class Feature(StrEnum):
    """Modeling features a problem can use and a solver can declare support for (D-067)."""

    ALTERNATIVES = "alternatives"
    SETUPS = "setups"
    DETACHED_SETUP = "detached_setup"
    CALENDARS = "calendars"
    PAUSE = "pause"
    OPERATORS = "operators"
    BATCHING = "batching"
    RESTART = "restart"
    LAGS = "lags"
    MAX_LAGS = "max_lags"
    OVERLAP = "overlap"
    PINS = "pins"
    IN_PROGRESS = "in_progress"
    MACHINE_STATE = "machine_state"


@dataclass(frozen=True, slots=True, kw_only=True)
class SolveContext:
    """What `solve()` hands a solver besides the problem and objective (D-070).

    `deadline` is a `time.monotonic()` instant. `params` is the profile's
    `method_params[solver.name]` (empty when absent), held read-only so a solver cannot
    change the profile it came from.
    """

    previous_plan: PreviousPlan | None
    churn_tolerance: float
    seed: int = 0
    deadline: float
    warm_start: Schedule | None
    params: Mapping[str, ParamValue]

    def __post_init__(self) -> None:
        # A frozen dataclass sets fields through object.__setattr__; the copy detaches the
        # caller's dict so later mutation of it cannot reach the solver either.
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


class Solver(Protocol):
    """A scheduling algorithm behind the seam (REQ-072, D-067, D-070).

    `solve` gets the compiled problem, the objective spec and the `SolveContext`, never the
    overrides or the profile. It returns UNKNOWN or INFEASIBLE with named issues when it
    has no plan.
    """

    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[Feature]: ...

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule: ...
