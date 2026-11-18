"""Solver registry: find, list and build solvers by name (COMP-002, REQ-088, D-069).

`baseline` registers from core. Every other solver comes from a leaf module of
`twinflow.scheduling.algorithms`, discovered with `pkgutil.iter_modules` on the first
call and cached; importing this module imports no leaf. A leaf whose top level raises
propagates the error: only an optional dependency imported inside a factory is allowed to
be missing, and it surfaces as `OptionalDependencyError` from `get`.

Priority rules by name (T-149; SDD COMP-002 names no rule registry): a leaf may also export
`RULES: tuple[RuleSpec, ...]`, and `rule(name, **params)` builds a fresh `PriorityRule`.
Construction and beam search compose rules this way: beam's rollout continues a partial
`DispatchState`, which a whole-solver call cannot start from, and leaves never import each
other. The dispatch engine calls the rule's `prepare(index)` once per run.

Analyses by name (T-153; SDD COMP-011 "registered as an analysis entry, not a solver"): a
leaf may export `ANALYSES: tuple[AnalysisSpec, ...]`, and `analysis(name)` returns the
function. An analysis reads a compiled problem and returns a report, never a schedule, so it
is not listed by `available()`.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from types import ModuleType
from typing import TYPE_CHECKING

from twinflow.scheduling import algorithms
from twinflow.scheduling.baseline import CAPABILITIES as BASELINE_CAPABILITIES
from twinflow.scheduling.baseline import NAME as BASELINE_NAME
from twinflow.scheduling.baseline import BaselineSolver
from twinflow.scheduling.seam import Feature, Solver

if TYPE_CHECKING:
    from twinflow.scheduling.kernel.dispatch import PriorityRule


class UnknownSolverError(ValueError):
    """Raised by `get` for a name no solver registered; the message lists the registered."""


class UnknownRuleError(ValueError):
    """Raised by `rule` for a name no leaf registered as a rule; the message lists them."""


class UnknownAnalysisError(ValueError):
    """Raised by `analysis` for a name no leaf registered as an analysis; lists them."""


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
class RuleSpec:
    """A priority rule a leaf contributes: `factory(**params)` builds a fresh rule.

    The factory raises ValueError for an unknown or invalid param.
    """

    name: str
    factory: Callable[..., PriorityRule]


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalysisSpec:
    """An analysis a leaf contributes: `function` is returned as is by `analysis(name)`."""

    name: str
    function: Callable[..., object]


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
def _leaves() -> tuple[ModuleType, ...]:
    prefix = algorithms.__name__ + "."
    found = sorted(pkgutil.iter_modules(algorithms.__path__, prefix), key=lambda m: m.name)
    return tuple(importlib.import_module(leaf.name) for leaf in found)


@cache
def _entries() -> Mapping[str, tuple[SolverSpec, str]]:
    entries: dict[str, tuple[SolverSpec, str]] = {}
    _register(entries, _BASELINE, BaselineSolver.__module__)
    for module in _leaves():
        for spec in module.SOLVERS:
            _register(entries, spec, module.__name__)
    return entries


@cache
def _rules() -> Mapping[str, RuleSpec]:
    rules: dict[str, RuleSpec] = {}
    for module in _leaves():
        for spec in getattr(module, "RULES", ()):
            if spec.name in rules:
                raise ValueError(f"rule {spec.name!r} from {module.__name__} is registered twice")
            rules[spec.name] = spec
    return rules


@cache
def _analyses() -> Mapping[str, AnalysisSpec]:
    analyses: dict[str, AnalysisSpec] = {}
    for module in _leaves():
        for spec in getattr(module, "ANALYSES", ()):
            if spec.name in analyses:
                raise ValueError(
                    f"analysis {spec.name!r} from {module.__name__} is registered twice"
                )
            analyses[spec.name] = spec
    return analyses


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


def rule(name: str, **params: object) -> PriorityRule:
    """A fresh priority rule built with `params`; `UnknownRuleError` for an unknown name.

    An unknown or invalid param raises ValueError from the rule's factory.
    """
    spec = _rules().get(name)
    if spec is None:
        registered = ", ".join(sorted(_rules()))
        raise UnknownRuleError(f"unknown rule {name!r}; registered: {registered}")
    return spec.factory(**params)


def analysis(name: str) -> Callable[..., object]:
    """The analysis function registered as `name`; `UnknownAnalysisError` for an unknown one."""
    spec = _analyses().get(name)
    if spec is None:
        registered = ", ".join(sorted(_analyses()))
        raise UnknownAnalysisError(f"unknown analysis {name!r}; registered: {registered}")
    return spec.function
