"""The solver seam is a leaf module (T-114, COMP-001, REQ-087, D-070; TF-003 review item 5).

`Feature`, `Solver` and `SolveContext` live in `twinflow.scheduling.seam`, which imports
only `twinflow.scheduling.model` and the standard library. Solver modules import the seam,
never `solver.py`, so `solver.py` needs no import inside a function to stay acyclic.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import sys
from pathlib import Path

import pytest

import twinflow.scheduling

PACKAGE_DIR = Path(twinflow.scheduling.__file__).parent
SEAM_FILE = PACKAGE_DIR / "seam.py"
SOLVER_MODULE = "twinflow.scheduling.solver"
SOLVER_FILES = ("baseline.py", "cpsat.py")
CONTEXT_FIELDS = {"previous_plan", "churn_tolerance", "seed", "deadline", "warm_start", "params"}


def _tree(path: Path) -> ast.Module:
    assert path.is_file(), f"{path} is missing"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    """Every (module, relative level) imported anywhere in the tree."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def _solver_module_files(name: str) -> list[Path]:
    """`name` wherever it lives under the package (cpsat.py later moves to algorithms/)."""
    return sorted(PACKAGE_DIR.rglob(name))


def test_seam_exports_feature_solver_and_solve_context() -> None:
    seam = importlib.import_module("twinflow.scheduling.seam")
    for name in ("Feature", "Solver", "SolveContext"):
        assert hasattr(seam, name), name


def test_solve_context_is_a_frozen_dataclass_with_the_data_model_fields() -> None:
    from twinflow.scheduling.seam import SolveContext

    assert dataclasses.is_dataclass(SolveContext)
    assert {f.name for f in dataclasses.fields(SolveContext)} == CONTEXT_FIELDS
    context = SolveContext(
        previous_plan=None, churn_tolerance=0.0, deadline=1.0, warm_start=None, params={}
    )
    assert context.seed == 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.seed = 1  # type: ignore[misc]  # proving the dataclass refuses assignment


def test_seam_imports_only_the_model_and_the_standard_library() -> None:
    offending = []
    for module, level in _imported_modules(_tree(SEAM_FILE)):
        root = module.split(".")[0]
        if level > 0:
            offending.append(f"relative import level {level} {module}")
        elif root == "twinflow" and module != "twinflow.scheduling.model":
            offending.append(module)
        elif root not in ("twinflow", "__future__") and root not in sys.stdlib_module_names:
            offending.append(module)
    assert offending == []


@pytest.mark.parametrize("name", SOLVER_FILES)
def test_solver_modules_do_not_import_solver_py(name: str) -> None:
    files = _solver_module_files(name)
    assert files, f"{name} not found under {PACKAGE_DIR}"
    for path in files:
        modules = [module for module, _ in _imported_modules(_tree(path))]
        assert SOLVER_MODULE not in modules, path
        assert not any(module.startswith(f"{SOLVER_MODULE}.") for module in modules), path


def test_solver_py_has_no_import_inside_a_function() -> None:
    tree = _tree(PACKAGE_DIR / "solver.py")
    lazy = [
        f"{function.name}: {module}"
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        for module, _ in _imported_modules(function)
    ]
    assert lazy == []


def test_facade_exports_the_seam_types() -> None:
    seam = importlib.import_module("twinflow.scheduling.seam")
    assert twinflow.scheduling.Feature is seam.Feature
    assert twinflow.scheduling.Solver is seam.Solver
