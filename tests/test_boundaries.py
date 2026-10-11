"""Scaffold guardrails (T-001): the trust-boundary and no-eval rules enforced by grep.

These are cheap structural invariants that must hold from the first commit and forever
after (D-001, D-002). They are not tied to any one component's behaviour.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "twinflow"

# Modules removed from the codebase; nothing may import them again, statically or by name.
DELETED_MODULES: frozenset[str] = frozenset(
    {
        "twinflow.engine.acquire",
        "twinflow.scheduling.translate",
        "twinflow.benchmark",
        "twinflow.adapters.rl",
        "twinflow.adapters._gym",
        "twinflow.adapters.dispatch",
        "twinflow.production",
        "twinflow.scheduling.core",
        "twinflow.scheduling.ortools",
        "twinflow.application.linear_schedule",
        "twinflow.scheduling.cpsat",
    }
)

# The builtin eval()/exec() only — NOT a `.eval(` method call. simpleeval's sandboxed
# `.eval()` (the mandated evaluator API, D-006) is a method call and legitimate; the
# forbidden thing (D-002) is Python's builtin eval/exec, which never has a `.` or an
# identifier character immediately before it.
_BUILTIN_EVAL_EXEC = re.compile(r"(?<![\w.])(?:eval|exec)\s*\(")


def _py_files() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def test_pyyaml_has_exactly_one_importer() -> None:
    importers = [p for p in _py_files() if "import yaml" in p.read_text()]
    assert [p.name for p in importers] == ["loader.py"], importers


def test_simpleeval_has_exactly_one_importer() -> None:
    importers = [p for p in _py_files() if "import simpleeval" in p.read_text()]
    assert [p.name for p in importers] == ["expressions.py"], importers


def test_no_eval_or_exec_in_src() -> None:
    offenders = []
    for p in _py_files():
        text = p.read_text()
        if _BUILTIN_EVAL_EXEC.search(text):
            offenders.append((p.name, "builtin eval/exec"))
        if "yaml.load(" in text:
            offenders.append((p.name, "yaml.load("))
    assert offenders == [], offenders


def test_package_imports() -> None:
    import twinflow

    assert twinflow.__version__


def _package_of(path: pathlib.Path) -> list[str] | None:
    """Dotted package parts of a file under src/, or None when it is not under src/."""
    try:
        return list(path.relative_to(ROOT / "src").parent.parts)
    except ValueError:
        return None


def _resolve_from(node: ast.ImportFrom, package: list[str] | None) -> str | None:
    """Absolute module name of a `from` import, or None if a relative one cannot be resolved."""
    if node.level == 0:
        return node.module
    if package is None or node.level - 1 > len(package):
        return None
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def _imported_names(node: ast.AST, package: list[str] | None) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        module = _resolve_from(node, package)
        if module is None:
            return []
        return [module, *(f"{module}.{alias.name}" for alias in node.names)]
    return []


def _dynamic_import_name(node: ast.AST) -> list[str]:
    """The string argument of `import_module("x")` / `importlib.import_module("x")`."""
    if not isinstance(node, ast.Call) or not node.args:
        return []
    func = node.func
    called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    first = node.args[0]
    if called == "import_module" and isinstance(first, ast.Constant):
        return [first.value] if isinstance(first.value, str) else []
    return []


def _is_deleted(name: str) -> bool:
    return any(name == gone or name.startswith(gone + ".") for gone in DELETED_MODULES)


def _offenders_in(path: pathlib.Path) -> list[str]:
    package = _package_of(path)
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        names = [*_imported_names(node, package), *_dynamic_import_name(node)]
        hit = next((name for name in names if _is_deleted(name)), None)
        if hit is not None:
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {hit}")
    return found


def test_no_import_of_deleted_modules() -> None:
    this_file = pathlib.Path(__file__).resolve()
    files = sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "tests").rglob("*.py")])
    offenders = [o for p in files if p.resolve() != this_file for o in _offenders_in(p)]
    assert offenders == [], "imports of deleted modules:\n" + "\n".join(offenders)


SCHEDULING = SRC / "scheduling"
# The scheduling engine is a leaf: it knows nothing about the surfaces that call it.
_SCHEDULING_FORBIDDEN = ("application", "service", "mcp", "plan", "primitives", "cli")


def _absolute_imports(path: pathlib.Path) -> list[tuple[int, str]]:
    package = _package_of(path)
    return [
        (node.lineno, name)
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path)))
        for name in _imported_names(node, package)
    ]


def _imports_under(path: pathlib.Path, prefixes: tuple[str, ...]) -> list[str]:
    return [
        f"{path.relative_to(ROOT)}:{line} -> {name}"
        for line, name in _absolute_imports(path)
        if any(name == p or name.startswith(p + ".") for p in prefixes)
    ]


def test_scheduling_imports_no_surface_package() -> None:
    prefixes = tuple(f"twinflow.{name}" for name in _SCHEDULING_FORBIDDEN)
    offenders = [o for p in sorted(SCHEDULING.rglob("*.py")) for o in _imports_under(p, prefixes)]
    assert offenders == [], offenders


ALGORITHMS = SCHEDULING / "algorithms"
_ALGORITHMS_PACKAGE = "twinflow.scheduling.algorithms"


def _algorithm_module_imports(path: pathlib.Path) -> list[str]:
    """Imports of a leaf `twinflow.scheduling.algorithms.<x>` (static or by constant name).

    Importing the package itself is allowed: the registry needs its `__path__` to discover
    the leaves with `pkgutil.iter_modules` (D-069).
    """
    package = _package_of(path)
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        names = [*_imported_names(node, package), *_dynamic_import_name(node)]
        hit = next((n for n in names if n.startswith(_ALGORITHMS_PACKAGE + ".")), None)
        if hit is not None:
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {hit}")
    return found


def test_algorithms_package_exists() -> None:
    assert (ALGORITHMS / "__init__.py").is_file()


def test_scheduling_facade_imports_no_algorithm_module() -> None:
    assert _algorithm_module_imports(SCHEDULING / "__init__.py") == []


def test_no_module_outside_algorithms_imports_an_algorithm_leaf() -> None:
    outside = [p for p in sorted(SRC.rglob("*.py")) if ALGORITHMS not in p.parents]
    offenders = [o for p in outside for o in _algorithm_module_imports(p)]
    assert offenders == [], "leaf algorithm imports outside algorithms/:\n" + "\n".join(offenders)


KERNEL = SCHEDULING / "kernel"
# SDD import rules (+ DEV-1): the shared kernel sees only these core modules and itself.
_KERNEL_ALLOWED = tuple(
    f"twinflow.scheduling.{name}"
    for name in ("model", "setups", "calendar_math", "timeline", "objectives", "seam", "kernel")
)
_KERNEL_THIRD_PARTY = frozenset({"numpy"})


def _kernel_import_allowed(name: str) -> bool:
    root = name.split(".")[0]
    if root != "twinflow":
        return root in sys.stdlib_module_names or root in _KERNEL_THIRD_PARTY
    if name in ("twinflow", "twinflow.scheduling"):
        return True  # the package parts of an allowed `from twinflow.scheduling import x`
    return any(name == p or name.startswith(p + ".") for p in _KERNEL_ALLOWED)


def test_kernel_package_exists() -> None:
    assert (KERNEL / "__init__.py").is_file()


def test_kernel_imports_only_core_modules_stdlib_and_numpy() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{line} -> {name}"
        for path in sorted(KERNEL.rglob("*.py"))
        for line, name in _absolute_imports(path)
        if not _kernel_import_allowed(name)
    ]
    assert offenders == [], "kernel imports outside its rule:\n" + "\n".join(offenders)


def test_kernel_import_rule_rejects_solver_baseline_and_algorithms() -> None:
    assert not _kernel_import_allowed("twinflow.scheduling.solver")
    assert not _kernel_import_allowed("twinflow.scheduling.baseline")
    assert not _kernel_import_allowed("twinflow.scheduling.verifier")
    assert not _kernel_import_allowed("twinflow.scheduling.algorithms.cpsat")
    assert not _kernel_import_allowed("ortools")
    assert _kernel_import_allowed("twinflow.scheduling.kernel.index")
    assert _kernel_import_allowed("numpy.typing")


LEARNING = SCHEDULING / "learning"
# D-082 + T-163/T-164: K learning sees the kernel, the model, the registry (ATCS by name at
# call time), the solve facade and itself; never an algorithm module.
_LEARNING_ALLOWED = tuple(
    f"twinflow.scheduling.{name}" for name in ("kernel", "model", "registry", "solver", "learning")
)
_LEARNING_THIRD_PARTY = frozenset({"pydantic"})


def _learning_import_allowed(name: str) -> bool:
    root = name.split(".")[0]
    if root != "twinflow":
        return root in sys.stdlib_module_names or root in _LEARNING_THIRD_PARTY
    if name in ("twinflow", "twinflow.scheduling"):
        return True
    return any(name == p or name.startswith(p + ".") for p in _LEARNING_ALLOWED)


def test_learning_package_exists() -> None:
    assert (LEARNING / "__init__.py").is_file()
    assert (LEARNING / "ktable.py").is_file()
    assert (LEARNING / "perturb.py").is_file()


def test_learning_imports_only_kernel_model_registry_solver_stdlib_and_pydantic() -> None:
    files = sorted(LEARNING.rglob("*.py"))
    offenders = [
        f"{path.relative_to(ROOT)}:{line} -> {name}"
        for path in files
        for line, name in _absolute_imports(path)
        if not _learning_import_allowed(name)
    ]
    leaf_imports = [o for p in files for o in _algorithm_module_imports(p)]
    assert files, "learning/ has no modules"
    assert offenders == [], "learning imports outside its rule:\n" + "\n".join(offenders)
    assert leaf_imports == [], leaf_imports


def test_learning_import_rule_rejects_algorithm_modules_and_portfolio() -> None:
    assert not _learning_import_allowed("twinflow.scheduling.algorithms.atcs")
    assert not _learning_import_allowed("twinflow.scheduling.algorithms")
    assert not _learning_import_allowed("twinflow.scheduling.portfolio")
    assert not _learning_import_allowed("ortools")
    assert _learning_import_allowed("twinflow.scheduling.kernel.stats")
    assert _learning_import_allowed("twinflow.scheduling.registry")
