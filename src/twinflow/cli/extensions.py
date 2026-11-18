"""`twinflow extensions list|check <project>` (TF-006 T-262, REQ-222, REQ-213, D-145).

`list` shows every loaded extension by kind with its source and problems; `check` runs the
project checker and prints each problem. Exit codes: 0 ok, 1 a problem the user can fix,
2 an internal failure.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, TypeVar

from twinflow.cli._common import guarded, open_project
from twinflow.extensions.checker import check_project
from twinflow.extensions.project import Project, ProjectError
from twinflow.extensions.project_model import project_error_model, project_view_model
from twinflow.report import decision

ParserT = TypeVar("ParserT", bound=argparse.ArgumentParser)

PROJECT_PAGE = "project.html"
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
    check.add_argument("--html", action="store_true", help="also write project.html")
    check.set_defaults(handler=lambda args: guarded(_handle_check, args))


def _row(entry: dict[str, Any]) -> dict[str, str]:
    return {
        "kind": str(entry["kind"]),
        "name": str(entry["name"]),
        "source": "builtin" if entry["source"] == "builtin" else "project",
        "overrides": str(entry["overrides"] or "-"),
        "problems": str(len(entry["problems"])),
    }


def _handle_list(args: argparse.Namespace) -> int:
    project = open_project(args.project)
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


def _open_for_check(args: argparse.Namespace) -> Project:
    """Open the project. With `--html`, a project that cannot open still gets its page."""
    try:
        return open_project(args.project)
    except ProjectError as error:
        if args.html:
            page = decision.write(
                Path(args.project) / PROJECT_PAGE, project_error_model(args.project, error)
            )
            print(f"Wrote {page}")
        raise


def _handle_check(args: argparse.Namespace) -> int:
    project = _open_for_check(args)
    problems = check_project(project)
    if args.html:
        page = decision.write(project.folder / PROJECT_PAGE, project_view_model(project, problems))
        print(f"Wrote {page}")
    if not problems:
        print("No problems found.")
        return 0
    for problem in problems:
        print(str(problem))
    errors = sum(1 for p in problems if p.severity == "error")
    print(f"{len(problems)} problem(s), {errors} of them errors.")
    return 1 if errors else 0
