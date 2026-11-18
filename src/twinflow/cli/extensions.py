"""`twinflow extensions list|check <project>` (TF-006 T-262, REQ-222, REQ-213, D-145).

`list` shows every loaded extension by kind with its source and problems; `check` runs the
project checker and prints each problem. Exit codes: 0 ok, 1 a problem the user can fix,
2 an internal failure.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, TypeVar

from twinflow.cli._common import guarded, open_project, problem_count
from twinflow.extensions.checker import check_project

ParserT = TypeVar("ParserT", bound=argparse.ArgumentParser)

_COLUMNS = ("kind", "name", "source", "overrides", "problems")


def add_extensions_subcommands(subparsers: argparse._SubParsersAction[ParserT]) -> None:
    """Attach `extensions list|check` to an application's subparser collection."""
    parser = subparsers.add_parser("extensions")
    nested = parser.add_subparsers(dest="extensions_command", required=True)

    listing = nested.add_parser("list")
    listing.add_argument("project")
    listing.add_argument("--json", action="store_true", dest="as_json")
    listing.set_defaults(handler=lambda args: guarded(_handle_list, args))

    check = nested.add_parser("check")
    check.add_argument("project")
    check.set_defaults(handler=lambda args: guarded(_handle_check, args))


def _row(entry: dict[str, Any], problems: int) -> dict[str, str]:
    return {
        "kind": str(entry["kind"]),
        "name": str(entry["name"]),
        "source": "builtin" if entry["source"] == "builtin" else "project",
        "overrides": str(entry["overrides"] or "-"),
        "problems": str(problems),
    }


def _handle_list(args: argparse.Namespace) -> int:
    project = open_project(args.project)
    entries = project.extensions.listing()
    problems = check_project(project)
    if args.as_json:
        print(json.dumps(entries, indent=2))
        return 0
    rows = [_row(entry, problem_count(problems, entry)) for entry in entries]
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in _COLUMNS}
    print("  ".join(c.ljust(widths[c]) for c in _COLUMNS).rstrip())
    for row in rows:
        print("  ".join(row[c].ljust(widths[c]) for c in _COLUMNS).rstrip())
    return 0


def _handle_check(args: argparse.Namespace) -> int:
    problems = check_project(open_project(args.project))
    if not problems:
        print("No problems found.")
        return 0
    for problem in problems:
        print(str(problem))
    errors = sum(1 for p in problems if p.severity == "error")
    print(f"{len(problems)} problem(s), {errors} of them errors.")
    return 1 if errors else 0
