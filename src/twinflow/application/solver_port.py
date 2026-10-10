"""The one adapter from every surface to the scheduling solver registry (COMP-028, REQ-088).

REST, MCP, the SDK, the CLI and the capability payloads resolve a solver by name here and
nowhere else. `registry()` returns TF-003's registry when the `twinflow.scheduling` facade
exposes it (`available_solvers`, `get_solver`, `UnknownSolverError`). Otherwise it returns
the built-in table of `baseline` and `ortools`: that table is the pre-TF-003 fallback and is
deleted when TF-003 merges, which leaves this module the one place to change.

Refusal messages never echo the requested name (REQ-170); they name the registered solvers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from twinflow.scheduling import BaselineSolver, Feature, ORToolsSolver, Solver
from twinflow.scheduling import baseline as _baseline
from twinflow.scheduling import cpsat as _cpsat

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


class _BuiltinRegistry:
    """The pre-TF-003 table. `ortools` is built lazily, so a missing extra fails on build only."""

    def __init__(self, entries: Mapping[str, tuple[SolverDescription, Callable[[], Solver]]]):
        self._entries = dict(entries)

    def available(self) -> tuple[SolverDescription, ...]:
        return tuple(description for description, _ in self._entries.values())

    def get(self, name: str) -> Solver:
        entry = self._entries.get(name)
        if entry is None:
            raise UnsupportedSolver(name, tuple(sorted(self._entries)))
        return entry[1]()


_BUILTIN = _BuiltinRegistry(
    {
        _baseline.NAME: (
            SolverDescription(
                name=_baseline.NAME,
                family="constructive",
                features=_features(_baseline.CAPABILITIES),
                deterministic=True,
            ),
            BaselineSolver,
        ),
        _cpsat.SOLVER_NAME: (
            SolverDescription(
                name=_cpsat.SOLVER_NAME,
                family="exact",
                features=_features(_cpsat.CAPABILITIES),
                deterministic=True,  # one worker, fixed seed (ORToolsSolver defaults)
            ),
            ORToolsSolver,
        ),
    }
)


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
    """TF-003's registry when the facade exposes it, else the built-in fallback table."""
    try:
        # Absent until TF-003 merges; the ignore covers both sides of that merge.
        from twinflow.scheduling import (  # type: ignore[attr-defined, unused-ignore]
            UnknownSolverError,
            get_solver,
        )
        from twinflow.scheduling import (  # type: ignore[attr-defined, unused-ignore]
            available_solvers as registered_solvers,
        )
    except ImportError:
        return _BUILTIN
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
