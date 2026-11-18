"""The one adapter from every surface to the scheduling solver registry (COMP-028, REQ-088).

REST, MCP, the SDK, the CLI and the capability payloads resolve a solver by name here and
nowhere else. `registry()` returns TF-003's registry (`available_solvers`, `get_solver`,
`UnknownSolverError` on the `twinflow.scheduling` facade, D-069), so every registered
solver name is accepted on every surface (TF-003 T-119). The pre-TF-003 built-in table of
`baseline` and `ortools` was deleted when TF-003 merged.

Refusal messages never echo the requested name (REQ-170); they name the registered solvers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from twinflow.scheduling import (
    Feature,
    Solver,
    UnknownSolverError,
    get_solver,
)
from twinflow.scheduling import available_solvers as registered_solvers

__all__ = [
    "SolverDescription",
    "SolverRegistry",
    "UnsupportedSolver",
    "available_solvers",
    "build_solver",
    "registry",
]


class SolverDescription(BaseModel):
    """One registered solver as every capability payload lists it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    family: str
    features: tuple[Feature, ...]
    deterministic: bool


class UnsupportedSolver(ValueError):  # noqa: N818 - names the refusal, like its code
    """A solver name that is not registered; `registered` lists the names that are."""

    code: ClassVar[str] = "unsupported_solver"

    def __init__(self, name: str, registered: tuple[str, ...]) -> None:
        # `name` is caller input: kept for logs by the caller, never put in the message.
        super().__init__(f"unknown solver; registered: {', '.join(registered)}")
        self.name = name
        self.registered = registered


class SolverRegistry(Protocol):
    """What the surfaces need from a registry: list solvers, build one by name."""

    def available(self) -> tuple[SolverDescription, ...]: ...

    def get(self, name: str) -> Solver: ...


def _features(capabilities: frozenset[Feature]) -> tuple[Feature, ...]:
    return tuple(sorted(capabilities, key=lambda feature: feature.value))


class _SchedulingRegistry:
    """TF-003's registry behind this port's contract."""

    def __init__(
        self,
        listing: Callable[[], tuple[Any, ...]],
        factory: Callable[[str], Solver],
        unknown: type[Exception],
    ) -> None:
        self._listing = listing
        self._factory = factory
        self._unknown = unknown

    def available(self) -> tuple[SolverDescription, ...]:
        return tuple(
            SolverDescription(
                name=info.name,
                family=info.family,
                features=_features(info.capabilities),
                deterministic=info.deterministic,
            )
            for info in self._listing()
        )

    def get(self, name: str) -> Solver:
        try:
            return self._factory(name)
        except self._unknown as exc:
            registered = tuple(sorted(info.name for info in self._listing()))
            raise UnsupportedSolver(name, registered) from exc


def registry() -> SolverRegistry:
    """TF-003's scheduling registry behind this port's contract."""
    return _SchedulingRegistry(registered_solvers, get_solver, UnknownSolverError)


def available_solvers() -> tuple[SolverDescription, ...]:
    """Every registered solver, sorted by name."""
    return tuple(sorted(registry().available(), key=lambda description: description.name))


def build_solver(name: str) -> Solver:
    """A fresh instance of the registered solver `name`; raises `UnsupportedSolver` if none.

    Building `ortools` without the optional `scheduling` extra raises
    `OptionalDependencyError`.
    """
    return registry().get(name)
