"""`twinflow extensions list|check <project>` (TF-006 T-262, REQ-222, REQ-213, D-145).

`list` shows every loaded extension by kind with its source and problems; `check` runs the
project checker and prints each problem. Exit codes: 0 ok, 1 a problem the user can fix,
2 an internal failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TypeVar

from twinflow.extensions.checker import check_project
from twinflow.extensions.project import Project, ProjectError

ParserT = TypeVar("ParserT", bound=argparse.ArgumentParser)

_COLUMNS = ("kind", "name", "source", "overrides", "problems")


def add_extensions_subcommands(subparsers: argparse._SubParsersAction[ParserT]) -> None:
    """Attach `extensions list|check` to an application's subparser collection."""
    parser = subparsers.add_parser("extensions")
    nested = parser.add_subparsers(dest="extensions_command", required=True)

    listing = nested.add_parser("list")
    listing.add_argument("project")
    listing.add_argument("--json", action="store_true", dest="as_json")
    listing.set_defaults(handler=lambda args: _guarded(_handle_list, args))

    check = nested.add_parser("check")
    check.add_argument("project")
    check.set_defaults(handler=lambda args: _guarded(_handle_check, args))


class _UserError(Exception):
    """A problem the user can fix. The message says where and how."""


def _guarded(handler: Any, args: argparse.Namespace) -> int:
    try:
        return int(handler(args))
    except (ProjectError, _UserError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # an internal failure: say so and use exit code 2
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _open(path: str) -> Project:
    if not Path(path).exists():
        raise _UserError(
            f"{path} does not exist. Fix: give the project folder (it holds one *.twin.yaml file)."
        )
    return Project.open(path)


def _row(entry: dict[str, Any]) -> dict[str, str]:
    return {
        "kind": str(entry["kind"]),
        "name": str(entry["name"]),
        "source": "builtin" if entry["source"] == "builtin" else "project",
        "overrides": str(entry["overrides"] or "-"),
        "problems": str(len(entry["problems"])),
    }


def _handle_list(args: argparse.Namespace) -> int:
    project = _open(args.project)
    entries = project.extensions.listing()
    if args.as_json:
        print(json.dumps(entries, indent=2))
        return 0
    rows = [_row(entry) for entry in entries]
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in _COLUMNS}
    print("  ".join(c.ljust(widths[c]) for c in _COLUMNS).rstrip())
    for row in rows:
        print("  ".join(row[c].ljust(widths[c]) for c in _COLUMNS).rstrip())
    return 0


def _handle_check(args: argparse.Namespace) -> int:
    problems = check_project(_open(args.project))
    if not problems:
        print("No problems found.")
        return 0
    for problem in problems:
        print(str(problem))
    errors = sum(1 for p in problems if p.severity == "error")
    print(f"{len(problems)} problem(s), {errors} of them errors.")
    return 1 if errors else 0
