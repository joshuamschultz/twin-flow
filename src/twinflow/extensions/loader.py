"""COMP-155 / COMP-161: one loader for every extension kind (REQ-200, REQ-201, REQ-216).

`load_project(folder)` gathers the built-ins (`extensions/builtin/<folder>/*.py`, D-142)
and then the project's own folders (`policies/`, `rewards/`, `views/`, `features/`,
`demand/`, `observations/`, `engines/`). A project file with the same `NAME` as a built-in
replaces it, and the listing says so (D-151).

A file is imported with `importlib.util.spec_from_file_location` only (no builtin exec or
eval, D-002). Project modules are named `twinflow_user_<kind>_<stem>_<hash8>`, where
`hash8` is the first 8 hex digits of the sha256 of the resolved path, so two folders with
the same file name never collide (D-146; a policy gets `twinflow_user_policy_...`). Modules
are not added to `sys.modules`.

A file that cannot be used is never an exception: it becomes a `Problem` naming the kind,
file, line (when known), what is wrong and how to fix it (REQ-213).

Loading runs the file's code: only the CLI and in-process Python may call this (D-139).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import inspect
import re
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Literal

from twinflow.extensions.kinds import KINDS, Kind
from twinflow.state.issues import did_you_mean

Source = Literal["builtin", "project"]
Severity = Literal["error", "info"]
DECISIONS = ("make_next", "dispatch")
BUILTIN = Path(__file__).resolve().parent / "builtin"
_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class Problem:
    """One thing wrong, in plain words (REQ-213): where, what, and how to fix it."""

    kind: str
    code: str
    message: str
    fix: str
    file: Path | None = None
    line: int | None = None
    name: str | None = None
    field: str | None = None
    severity: Severity = "error"

    @property
    def where(self) -> str:
        """`policies/rule.py line 4`, `capsule.twin.yaml experiment.score`, or the name."""
        parts = []
        if self.file is not None:
            parts.append(f"{self.file.parent.name}/{self.file.name}")
        if self.line is not None:
            parts.append(f"line {self.line}")
        if self.field is not None:
            parts.append(self.field)
        if not parts and self.name is not None:
            parts.append(f"{self.kind} {self.name}")
        return " ".join(parts) or self.kind

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "code": self.code,
            "message": self.message,
            "fix": self.fix,
            "file": None if self.file is None else str(self.file),
            "line": self.line,
            "name": self.name,
            "field": self.field,
            "severity": self.severity,
        }

    def __str__(self) -> str:
        return f"{self.where}: {self.message} Fix: {self.fix}"


@dataclass(frozen=True)
class Extension:
    """One loaded extension file. Calling it calls its function with its settings."""

    kind: str
    name: str
    file: Path
    source: Source
    target: Any
    description: str = ""
    line: int | None = None
    decision: str = "make_next"
    grid: Mapping[str, tuple[Any, ...]] = field(default=_EMPTY)
    settings: Mapping[str, Any] = field(default=_EMPTY)
    overrides_builtin: bool = False

    @property
    def summary(self) -> str:
        """The first line of the docstring."""
        return self.description.strip().splitlines()[0] if self.description.strip() else ""

    def __call__(self, *args: Any, **settings: Any) -> Any:
        """Call the function (or build the class): fixed `SETTINGS`, then `settings`."""
        return self.target(*args, **{**self.settings, **settings})


class ExtensionNotFound(KeyError):
    """No extension of that kind has that name. `str()` is the plain message."""

    def __init__(self, kind: str, name: str, known: Iterable[str]) -> None:
        options = tuple(known)
        hint = did_you_mean(name, options) if options else f"No {kind} files were found."
        self.message = f"No {kind} named {name!r}. {hint}".strip()
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Extensions:
    """Everything `load_project` found: extensions by kind and name, and the problems."""

    folder: Path | None
    by_kind: Mapping[str, Mapping[str, Extension]]
    problems: tuple[Problem, ...] = ()

    def names(self, kind: str) -> tuple[str, ...]:
        return tuple(sorted(self.by_kind.get(kind, {})))

    def get(self, kind: str, name: str) -> Extension:
        found = self.by_kind.get(kind, {})
        if name not in found:
            raise ExtensionNotFound(kind, name, found)
        return found[name]

    def of(self, kind: str) -> tuple[Extension, ...]:
        return tuple(self.by_kind.get(kind, {})[n] for n in self.names(kind))

    def all(self) -> tuple[Extension, ...]:
        return tuple(ext for kind in KINDS for ext in self.of(kind))

    def listing(self) -> list[dict[str, Any]]:
        """One row per extension and per broken file: kind, name, source, file, override,
        summary and problem codes (REQ-222)."""
        rows = []
        for ext in self.all():
            codes = [p.code for p in self.problems if p.file == ext.file or _same(p, ext)]
            rows.append(
                {
                    "kind": ext.kind,
                    "name": ext.name,
                    "source": ext.source,
                    "file": str(ext.file),
                    "overrides": f"built-in {ext.name}" if ext.overrides_builtin else None,
                    "summary": ext.summary,
                    "problems": codes,
                }
            )
        loaded = {ext.file for ext in self.all()}
        for problem in self.problems:
            if problem.file is not None and problem.file not in loaded:
                rows.append(
                    {
                        "kind": problem.kind,
                        "name": problem.name or problem.file.stem,
                        "source": "project",
                        "file": str(problem.file),
                        "overrides": None,
                        "summary": "",
                        "problems": [problem.code],
                    }
                )
        return rows


def _same(problem: Problem, ext: Extension) -> bool:
    return problem.file is None and problem.kind == ext.kind and problem.name == ext.name


MakeProblem = Callable[[str, str], Problem]


# --- one file ----------------------------------------------------------------------------------


def module_name(path: Path | str, kind: str, source: Source = "project") -> str:
    """The module name for one file (D-146)."""
    resolved = Path(path).resolve()
    stem = re.sub(r"\W", "_", resolved.stem)
    if source == "builtin":
        return f"twinflow_builtin_{kind}_{stem}"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"twinflow_user_{kind}_{stem}_{digest}"


def discover(folder: Path | str, kind: Kind) -> list[Path]:
    """The kind's files in `folder/<kind folder>/`, sorted; `_*` and non-`.py` skipped."""
    where = Path(folder) / kind.folder
    if not where.is_dir():
        return []
    return sorted(
        p
        for p in where.iterdir()
        if p.is_file() and p.suffix == ".py" and not p.name.startswith("_")
    )


def line_in(path: Path, exc: BaseException) -> int | None:
    """The line of the deepest traceback frame inside `path` (or a syntax error's line)."""
    if isinstance(exc, SyntaxError) and exc.lineno is not None and exc.filename is not None:
        if Path(exc.filename).resolve() == path:
            return exc.lineno
    frames = [
        f for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).resolve() == path
    ]
    return frames[-1].lineno if frames else None


def _assignment_lines(path: Path) -> dict[str, int]:
    """Line of each top-level name (assignments, functions, classes)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, ValueError, OSError):
        return {}
    lines: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            lines.setdefault(node.name, node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    lines.setdefault(target.id, node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            lines.setdefault(node.target.id, node.lineno)
    return lines


def _import(path: Path, kind: Kind, source: Source) -> ModuleType | Problem:
    spec = importlib.util.spec_from_file_location(module_name(path, kind.name, source), path)
    if spec is None or spec.loader is None:
        return Problem(
            kind.name, "import_error", "Python cannot load this file.", "Check it is .py.", path
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # any error in user code becomes a problem, never a crash
        return Problem(
            kind.name,
            "import_error",
            f"This file could not be loaded: {type(exc).__name__}: {exc}.",
            "Fix the line named here, then run the check again.",
            path,
            line_in(path, exc),
        )
    return module


def _accepts(target: Any, keys: Iterable[str]) -> list[str]:
    """The keys `target` does not take as keyword arguments."""
    try:
        params = inspect.signature(target).parameters.values()
    except (TypeError, ValueError):
        return []
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return []
    names = {
        p.name
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return [k for k in keys if k not in names]


def _settings(
    module: ModuleType, kind: Kind, target: Any, problem: MakeProblem
) -> tuple[Mapping[str, tuple[Any, ...]], Mapping[str, Any]] | Problem:
    raw = getattr(module, "SETTINGS", None)
    if raw is None:
        return {}, {}
    grid = kind.settings == "grid"
    example = "SETTINGS = {'stop_days': [5, 10, 15]}" if grid else "SETTINGS = {'penalty': 5.0}"
    if not isinstance(raw, Mapping) or not all(isinstance(k, str) for k in raw):
        return problem("SETTINGS must name each setting.", f"Write it like {example}.")
    if grid:
        for key, values in raw.items():
            if not isinstance(values, list | tuple) or not values:
                return problem(
                    f"SETTINGS gives {key!r} no list of values to try.",
                    f"Write it like {example}.",
                )
    missing = _accepts(target, raw)
    if missing:
        call = kind.function
        return problem(
            f"SETTINGS has {missing[0]!r}, but {call} has no setting with that name.",
            f"Add {missing[0]}=<default> to the {call} line, or remove it from SETTINGS.",
        )
    if grid:
        return MappingProxyType({k: tuple(v) for k, v in raw.items()}), {}
    return {}, MappingProxyType(dict(raw))


def load_file(path: Path | str, kind: Kind, source: Source = "project") -> Extension | Problem:
    """Load one extension file, or say in plain words why it cannot be used."""
    file = Path(path).resolve()
    module = _import(file, kind, source)
    if isinstance(module, Problem):
        return module
    lines = _assignment_lines(file)

    def problem(code: str, attribute: str) -> MakeProblem:
        def make(message: str, fix: str) -> Problem:
            return Problem(kind.name, code, message, fix, file, lines.get(attribute))

        return make

    name = getattr(module, "NAME", file.stem)
    if not isinstance(name, str) or not name.strip():
        return problem("bad_name", "NAME")(
            f"NAME is {name!r}; it must be a short text.", "Write NAME = 'my_rule'."
        )
    target = getattr(module, kind.function, None)
    wanted = isinstance(target, type) if kind.is_class else callable(target)
    if not wanted:
        what = f"a class named {kind.function}" if kind.is_class else f"a {kind.function} function"
        return Problem(
            kind.name,
            "no_function",
            f"This file has no {what}.",
            f"Add `{kind.example}` that returns {kind.returns}.",
            file,
            name=name,
        )
    decision = getattr(module, "DECISION", "make_next")
    if kind.name == "policy" and decision not in DECISIONS:
        return problem("bad_decision", "DECISION")(
            f"DECISION is {decision!r}.", "Write DECISION = 'make_next' or DECISION = 'dispatch'."
        )
    settings = _settings(module, kind, target, problem("bad_settings", "SETTINGS"))
    if isinstance(settings, Problem):
        return settings
    return Extension(
        kind=kind.name,
        name=name.strip(),
        file=file,
        source=source,
        target=target,
        description=inspect.cleandoc(module.__doc__ or ""),
        line=lines.get(kind.function),
        decision=str(decision) if kind.name == "policy" else "make_next",
        grid=settings[0],
        settings=settings[1],
    )


# --- a whole project ---------------------------------------------------------------------------


def _gather(
    root: Path, source: Source, found: dict[str, dict[str, Extension]], problems: list[Problem]
) -> None:
    for kind in KINDS.values():
        mine: dict[str, Extension] = {}
        for path in discover(root, kind):
            loaded = load_file(path, kind, source)
            if isinstance(loaded, Problem):
                problems.append(loaded)
                continue
            first = mine.get(loaded.name)
            if first is not None:
                problems.append(
                    Problem(
                        kind.name,
                        "duplicate_name",
                        f"Two files use the name {loaded.name!r}: {first.file.name} and "
                        f"{loaded.file.name}. Only {first.file.name} is used.",
                        "Give each file its own NAME.",
                        loaded.file,
                        name=loaded.name,
                    )
                )
                continue
            mine[loaded.name] = loaded
            if source == "project" and loaded.name in found[kind.name]:
                loaded = dataclasses.replace(loaded, overrides_builtin=True)
            found[kind.name][loaded.name] = loaded


def load_project(folder: Path | str | None, *, builtin: Path | str = BUILTIN) -> Extensions:
    """Built-ins, then the project's own files; same NAME means the project file wins."""
    found: dict[str, dict[str, Extension]] = {kind: {} for kind in KINDS}
    problems: list[Problem] = []
    _gather(Path(builtin), "builtin", found, problems)
    root = None if folder is None else Path(folder).resolve()
    if root is not None and not root.is_dir():
        problems.append(
            Problem(
                "project",
                "no_folder",
                f"The project folder {root} does not exist.",
                "Check the path, or create the folder with `twinflow new`.",
                root,
            )
        )
    elif root is not None:
        _gather(root, "project", found, problems)
    by_kind = MappingProxyType({k: MappingProxyType(v) for k, v in found.items()})
    return Extensions(root, by_kind, tuple(problems))
