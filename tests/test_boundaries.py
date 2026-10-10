"""Scaffold guardrails (T-001): the trust-boundary and no-eval rules enforced by grep.

These are cheap structural invariants that must hold from the first commit and forever
after (D-001, D-002). They are not tied to any one component's behaviour.
"""

from __future__ import annotations

import ast
import pathlib
import re

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
