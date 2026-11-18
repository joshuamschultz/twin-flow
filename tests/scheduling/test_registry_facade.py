"""The scheduling facade exposes the registry, not a concrete algorithm (T-118, D-069).

Facade names: `available_solvers` (= `registry.available`), `get_solver`
(= `registry.get`), `is_registered_solver` (= `registry.is_registered`) and
`UnknownSolverError`. `ORToolsSolver` and `OptionalDependencyError` leave the facade with
the move of `cpsat.py` into `algorithms/`.
"""

from __future__ import annotations

import importlib

import pytest

import twinflow.scheduling as facade

REGISTRY_EXPORTS = {
    "available_solvers": "available",
    "get_solver": "get",
    "is_registered_solver": "is_registered",
    "UnknownSolverError": "UnknownSolverError",
}


@pytest.mark.parametrize("name", ["ORToolsSolver", "OptionalDependencyError"])
def test_facade_no_longer_exports_cpsat_names(name: str) -> None:
    assert name not in facade.__all__
    assert not hasattr(facade, name)


@pytest.mark.parametrize(("facade_name", "registry_name"), sorted(REGISTRY_EXPORTS.items()))
def test_facade_exports_registry_functions(facade_name: str, registry_name: str) -> None:
    registry = importlib.import_module("twinflow.scheduling.registry")

    assert facade_name in facade.__all__
    assert getattr(facade, facade_name) is getattr(registry, registry_name)


def test_facade_get_solver_builds_the_baseline() -> None:
    assert facade.get_solver("baseline").name == "baseline"  # type: ignore[attr-defined]  # T-118 adds it
