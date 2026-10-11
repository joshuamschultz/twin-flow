"""Solver registry: find, list and build solvers by name (COMP-002, REQ-088, D-069).

`baseline` registers from core. Every other solver comes from a leaf module of
`twinflow.scheduling.algorithms`, discovered with `pkgutil.iter_modules` on the first
call and cached; importing this module imports no leaf. A leaf whose top level raises
propagates the error: only an optional dependency imported inside a factory is allowed to
be missing, and it surfaces as `OptionalDependencyError` from `get`.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache

from twinflow.scheduling import algorithms
from twinflow.scheduling.baseline import CAPABILITIES as BASELINE_CAPABILITIES
from twinflow.scheduling.baseline import NAME as BASELINE_NAME
from twinflow.scheduling.baseline import BaselineSolver
from twinflow.scheduling.seam import Feature, Solver


class UnknownSolverError(ValueError):
    """Raised by `get` for a name no solver registered; the message lists the registered."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SolverSpec:
    """What a leaf contributes per solver: `factory(**params)` builds a fresh instance.

    `capabilities` is declared here so listing never builds a solver (and never imports an
    optional dependency).
    """

    name: str
    family: str
    factory: Callable[..., Solver]
    deterministic: bool
    capabilities: frozenset[Feature]


@dataclass(frozen=True, slots=True, kw_only=True)
class SolverInfo:
    """One registered solver as surfaces list it; `module` is where its spec came from."""

    name: str
    family: str
    capabilities: frozenset[Feature]
    module: str
    deterministic: bool


_BASELINE = SolverSpec(
    name=BASELINE_NAME,
    family="baseline",
    factory=BaselineSolver,
    deterministic=True,
    capabilities=BASELINE_CAPABILITIES,
)


def _register(entries: dict[str, tuple[SolverSpec, str]], spec: SolverSpec, module: str) -> None:
    if spec.name in entries:
        raise ValueError(
            f"solver {spec.name!r} from {module} is already registered by {entries[spec.name][1]}"
        )
    entries[spec.name] = (spec, module)


@cache
def _entries() -> Mapping[str, tuple[SolverSpec, str]]:
    entries: dict[str, tuple[SolverSpec, str]] = {}
    _register(entries, _BASELINE, BaselineSolver.__module__)
    prefix = algorithms.__name__ + "."
    for leaf in sorted(pkgutil.iter_modules(algorithms.__path__, prefix), key=lambda m: m.name):
        module = importlib.import_module(leaf.name)
        for spec in module.SOLVERS:
            _register(entries, spec, leaf.name)
    return entries


def available() -> tuple[SolverInfo, ...]:
    """Every registered solver, sorted by name."""
    return tuple(
        SolverInfo(
            name=spec.name,
            family=spec.family,
            capabilities=spec.capabilities,
            module=module,
            deterministic=spec.deterministic,
        )
        for _, (spec, module) in sorted(_entries().items())
    )


def is_registered(name: str) -> bool:
    """Whether a solver with this name is registered (its dependencies may still be absent)."""
    return name in _entries()


def get(name: str, **params: object) -> Solver:
    """A fresh solver built with constructor `params`; `UnknownSolverError` for an unknown name.

    Params are the factory's arguments: scalars, or objects such as ATCS's `k_lookup`
    (SDD Data Model "SolveContext"); per-solve scalar settings travel in `SolveContext.params`.
    """
    entry = _entries().get(name)
    if entry is None:
        registered = ", ".join(sorted(_entries()))
        raise UnknownSolverError(f"unknown solver {name!r}; registered: {registered}")
    return entry[0].factory(**params)
