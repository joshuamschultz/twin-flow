"""Scaffold guardrails (T-001): the trust-boundary and no-eval rules enforced by grep.

These are cheap structural invariants that must hold from the first commit and forever
after (D-001, D-002). They are not tied to any one component's behaviour.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

import pytest

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


# --- TF-005: ERP contract, planning service and MCP boundaries (T-192, REQ-174) ----------------

ERP = SRC / "erp"
MCP = SRC / "mcp"
PLANNING = SRC / "application" / "planning.py"

# `twinflow.erp` is a pure contract package: stdlib, pydantic and the scheduling model only,
# plus exactly one TF-004 module: the pure Pydantic `[simulation]` types (SDD import rules).
_ERP_ALLOWED_PREFIXES = (
    "pydantic",
    "twinflow.scheduling",
    "twinflow.erp",
    "twinflow.execution.policy",
)
# No outbound calls from the ERP, planning or MCP code (REQ-174); MCP never goes via the SDK.
_NETWORK_PREFIXES = (
    "httpx",
    "requests",
    "urllib.request",
    "urllib3",
    "aiohttp",
    "socket",
    "http.client",
    "twinflow.sdk",
)


def _existing_py(*paths: pathlib.Path) -> list[pathlib.Path]:
    """Python files at or under `paths`; a path that does not exist yet contributes none."""
    files: list[pathlib.Path] = []
    for path in paths:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
    return files


def _all_imports(path: pathlib.Path) -> list[tuple[int, str]]:
    """Static and `import_module("...")` imports of a file, as absolute dotted names."""
    package = _package_of(path)
    return [
        (node.lineno, name)
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path)))
        for name in [*_imported_names(node, package), *_dynamic_import_name(node)]
    ]


def _under(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _erp_allows(name: str) -> bool:
    top = name.split(".", 1)[0]
    return top in sys.stdlib_module_names or _under(name, _ERP_ALLOWED_PREFIXES)


def _network_offenders(files: list[pathlib.Path]) -> list[str]:
    return [
        f"{p.relative_to(ROOT)}:{line} -> {name}"
        for p in files
        for line, name in _all_imports(p)
        if _under(name, _NETWORK_PREFIXES)
    ]


def test_erp_imports_only_stdlib_pydantic_and_scheduling() -> None:
    offenders = [
        f"{p.relative_to(ROOT)}:{line} -> {name}"
        for p in _existing_py(ERP)
        for line, name in _all_imports(p)
        if not _erp_allows(name)
    ]
    assert offenders == [], offenders


def test_scheduling_does_not_import_erp() -> None:
    offenders = [
        f"{p.relative_to(ROOT)}:{line} -> {name}"
        for p in _existing_py(SCHEDULING)
        for line, name in _all_imports(p)
        if _under(name, ("twinflow.erp",))
    ]
    assert offenders == [], offenders


def test_erp_and_planning_make_no_outbound_calls() -> None:
    offenders = _network_offenders(_existing_py(ERP, PLANNING))
    assert offenders == [], offenders


def test_mcp_makes_no_outbound_calls_and_never_uses_the_sdk() -> None:
    offenders = _network_offenders(_existing_py(MCP))
    assert offenders == [], offenders


# The `mcp` SDK (the optional `agent` extra) stays at the transport edge: the tool guard,
# specs, models and handlers import none of it, so REST reuses the guard without the extra.
_MCP_SDK_IMPORTERS = frozenset({"tools.py", "http.py", "server.py"})
_MCP_SDK_FREE = frozenset({"__init__.py", "_handlers.py", "guard.py", "models.py", "spec.py"})


def test_only_the_mcp_transport_modules_import_the_mcp_sdk() -> None:
    importers = {
        path.name
        for path in _existing_py(MCP)
        if any(_under(name, ("mcp",)) for _, name in _all_imports(path))
    }
    files = {path.name for path in _existing_py(MCP)}

    assert importers <= _MCP_SDK_IMPORTERS, sorted(importers - _MCP_SDK_IMPORTERS)
    assert _MCP_SDK_FREE <= files, sorted(_MCP_SDK_FREE - files)
    assert importers.isdisjoint(_MCP_SDK_FREE)


def test_boundary_import_scanner_sees_network_and_sdk_imports(tmp_path: pathlib.Path) -> None:
    """Guard the guard: the scanner must flag every forbidden form, or the tests above lie."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import socket\n"
        "import httpx as h\n"
        "from urllib import request\n"
        "from http import client\n"
        "from twinflow import sdk\n"
        "from importlib import import_module\n"
        "import_module('requests')\n"
        "from urllib.parse import quote\n"
    )
    names = [name for _, name in _all_imports(probe) if _under(name, _NETWORK_PREFIXES)]
    assert set(names) >= {
        "socket",
        "httpx",
        "urllib.request",
        "http.client",
        "twinflow.sdk",
        "requests",
    }
    assert not any(name.startswith("urllib.parse") for name in names)
    assert not _erp_allows("fastapi")
    assert not _erp_allows("twinflow.application.workspace")
    assert _erp_allows("zoneinfo")
    assert _erp_allows("twinflow.scheduling.model")


def test_service_app_starts_without_the_mcp_package(tmp_path: pathlib.Path) -> None:
    """`mcp` is the optional `agent` extra: the app imports and builds without it (SDD COMP-119).

    Runs in a subprocess so the `sys.modules` patch cannot leak into this test session.
    """
    script = (
        "import sys\n"
        "sys.modules['mcp'] = None\n"
        "import twinflow.service.app as app\n"
        "app.create_app(sys.argv[1], workspace_root=sys.argv[2])\n"
        "assert sys.modules['mcp'] is None\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and script, no shell
        [sys.executable, "-c", script, str(ROOT / "examples"), str(tmp_path / "ws")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


# TF-003 reaches the service through exactly one adapter (`application/portfolio_port.py`),
# so a TF-003 signature change touches one file. Its modules and the portfolio functions
# (which `twinflow.scheduling` re-exports once TF-003 merges) are named nowhere else.
_TF003_MODULES = ("twinflow.scheduling.portfolio", "twinflow.scheduling.learning")
_TF003_SYMBOLS = frozenset(
    {
        "solve_portfolio",
        "resolve_with_overrides",
        "insert_rush_order",
        "get_pareto_menu",
        "PortfolioResult",
        "MenuEntry",
        "KTable",
        "as_k_lookup",
    }
)
_PORTFOLIO_PORT = SRC / "application" / "portfolio_port.py"


def _tf003_references(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found = [
        f"{line} -> {name}" for line, name in _all_imports(path) if _under(name, _TF003_MODULES)
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _under(node.value, _TF003_MODULES):
                found.append(f"{node.lineno} -> {node.value!r}")
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("twinflow"):
            found += [
                f"{node.lineno} -> {alias.name}"
                for alias in node.names
                if alias.name in _TF003_SYMBOLS
            ]
    return found


def test_only_the_portfolio_port_references_tf003() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{reference}"
        for path in _py_files()
        if path != _PORTFOLIO_PORT
        for reference in _tf003_references(path)
    ]
    assert offenders == [], offenders
    assert _tf003_references(_PORTFOLIO_PORT), "the scanner must see the adapter's own imports"


# --- TF-004: `twinflow.execution` is a pure driver package (T-169, SDD import rules) ---------

EXECUTION = SRC / "execution"
_EXECUTION_ALLOWED_PREFIXES = (
    "pydantic",
    "numpy",
    "scipy",
    "simpy",
    "twinflow.engine",
    "twinflow.scheduling",
    "twinflow.policy",
    "twinflow.execution",
)
# Packages that must never depend on the executor (it sits above them, D-085).
_EXECUTION_FREE_PACKAGES = ("scheduling", "engine", "primitives", "plan", "model")
# No network and no filesystem in the executor: it runs on data handed to it.
_EXECUTION_IO_PREFIXES = (
    *_NETWORK_PREFIXES,
    "urllib",
    "pathlib",
    "tempfile",
    "shutil",
)
# File I/O calls: builtin `open(`, `io.open(`/`os.open(`/`os.fdopen(`, and pathlib reads/writes.
# A bare `.open()` method is not flagged: executor objects (gates) may legitimately have one.
_IO_MODULE_CALLS = frozenset({("io", "open"), ("os", "open"), ("os", "fdopen")})
_PATH_IO_METHODS = frozenset({"write_text", "write_bytes", "read_text", "read_bytes"})


def _execution_files() -> list[pathlib.Path]:
    """Every module of `twinflow.execution`; fails loudly while the package does not exist."""
    assert (EXECUTION / "__init__.py").is_file(), "src/twinflow/execution/ is missing"
    return _existing_py(EXECUTION)


def _execution_allows(name: str) -> bool:
    top = name.split(".", 1)[0]
    return top in sys.stdlib_module_names or _under(name, _EXECUTION_ALLOWED_PREFIXES)


def _io_call(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == "open":
        return "open("
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in _PATH_IO_METHODS:
        return f".{func.attr}("
    owner = func.value.id if isinstance(func.value, ast.Name) else None
    return f"{owner}.{func.attr}(" if (owner, func.attr) in _IO_MODULE_CALLS else None


def _io_offenders(path: pathlib.Path) -> list[str]:
    found = [
        f"{path.relative_to(ROOT)}:{line} -> {name}"
        for line, name in _all_imports(path)
        if _under(name, _EXECUTION_IO_PREFIXES)
    ]
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        call = _io_call(node)
        if call is not None:
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {call}")
    return found


def test_execution_package_exists() -> None:
    assert _execution_files()


def test_execution_imports_only_the_sdd_allow_list() -> None:
    offenders = [
        f"{p.relative_to(ROOT)}:{line} -> {name}"
        for p in _execution_files()
        for line, name in _all_imports(p)
        if not _execution_allows(name)
    ]
    assert offenders == [], offenders


def test_execution_makes_no_network_or_file_io() -> None:
    offenders = [o for p in _execution_files() for o in _io_offenders(p)]
    assert offenders == [], offenders


@pytest.mark.parametrize("package", _EXECUTION_FREE_PACKAGES)
def test_lower_packages_never_import_execution(package: str) -> None:
    files = _existing_py(SRC / package)
    assert files, f"src/twinflow/{package}/ has no modules to scan"
    offenders = [
        f"{p.relative_to(ROOT)}:{line} -> {name}"
        for p in files
        for line, name in _all_imports(p)
        if _under(name, ("twinflow.execution",))
    ]
    assert offenders == [], offenders


def test_erp_imports_only_the_policy_module_from_execution() -> None:
    offenders = [
        f"{p.relative_to(ROOT)}:{line} -> {name}"
        for p in _existing_py(ERP)
        for line, name in _all_imports(p)
        if _under(name, ("twinflow.execution",))
        and not _under(name, ("twinflow.execution.policy",))
    ]
    assert offenders == [], offenders


def test_execution_boundary_scanners_see_forbidden_forms(tmp_path: pathlib.Path) -> None:
    """Guard the guard: the execution scanners must flag every forbidden form."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import socket\n"
        "import urllib.parse\n"
        "from pathlib import Path\n"
        "from twinflow.primitives import location\n"
        "from twinflow.execution import engine\n"
        "open('x')\n"
        "Path('x').write_text('y')\n"
        "io.open('x')\n"
        "os.open('x', 0)\n"
        "gate.open()\n"
    )
    imports = [name for _, name in _all_imports(probe)]
    io_found = _io_offenders_outside_root(probe)

    assert not _execution_allows("twinflow.primitives.location")
    assert not _execution_allows("twinflow.model")
    assert not _execution_allows("twinflow.erp.profile")
    assert not _execution_allows("httpx")
    assert _execution_allows("twinflow.scheduling.setups")
    assert _execution_allows("twinflow.policy.DISPATCH_NAMES")
    assert _execution_allows("simpy")
    assert _execution_allows("statistics")
    assert {"socket", "urllib.parse", "pathlib"} <= {
        name for name in imports if _under(name, _EXECUTION_IO_PREFIXES)
    }
    assert any(_under(name, ("twinflow.execution",)) for name in imports)
    assert sorted(io_found) == sorted(["open(", ".write_text(", "io.open(", "os.open("])
    assert _erp_allows("twinflow.execution.policy")
    assert _erp_allows("twinflow.execution.policy.SimulationPolicy")
    assert not _erp_allows("twinflow.execution")
    assert not _erp_allows("twinflow.execution.engine")
    assert not _erp_allows("twinflow.execution.policy_extra")


def _io_offenders_outside_root(path: pathlib.Path) -> list[str]:
    """The I/O calls `_io_offenders` flags, for a probe file outside the repo."""
    tree = ast.parse(path.read_text(), filename=str(path))
    return [call for node in ast.walk(tree) if (call := _io_call(node)) is not None]
