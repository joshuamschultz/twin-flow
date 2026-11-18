"""`twinflow policies list|check|race <project>` (TF-006 T-250, REQ-209, REQ-213, D-145).

`list` shows the policy files, `check` runs the checker on them, and `race` runs every
policy on the same demand and prints the ranking. Exit codes: 0 ok, 1 a problem the user
can fix, 2 an internal failure.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from twinflow.cli._common import UserError, guarded, open_project, race_settings, staged_project
from twinflow.extensions.checker import check_project
from twinflow.extensions.project import Project
from twinflow.extensions.race import PolicyResult, RaceResult, Stat, race
from twinflow.extensions.report import write_race_report
from twinflow.state.issues import did_you_mean

ParserT = TypeVar("ParserT", bound=argparse.ArgumentParser)

_LIST_COLUMNS = ("name", "source", "decision", "settings", "problems")
_RACE_COLUMNS = ("Rank", "Policy", "Score", "Dollars per run", "Fill rate", "Changeovers")


def add_policies_subcommands(subparsers: argparse._SubParsersAction[ParserT]) -> None:
    """Attach `policies list|check|race` to an application's subparser collection."""
    parser = subparsers.add_parser("policies")
    nested = parser.add_subparsers(dest="policies_command", required=True)

    listing = nested.add_parser("list")
    listing.add_argument("project")
    listing.set_defaults(handler=lambda args: guarded(_handle_list, args))

    check = nested.add_parser("check")
    check.add_argument("project")
    check.set_defaults(handler=lambda args: guarded(_handle_check, args))

    race_parser = nested.add_parser("race")
    race_parser.add_argument("project")
    race_parser.add_argument("--policies", help="a folder of policy files to race instead")
    race_parser.add_argument("--episodes", help="how many runs each policy gets")
    race_parser.add_argument("--seed", help="the random seed for the demand draws")
    race_parser.add_argument("--out", help="where to write race.json and race.html")
    race_parser.add_argument("--observation", help="what the policies may see, like bucketed")
    race_parser.set_defaults(handler=lambda args: guarded(_handle_race, args))


def _table(columns: tuple[str, ...], rows: list[list[str]]) -> list[str]:
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(columns)]
    lines = [columns, *rows]
    return [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in lines
    ]


def _handle_list(args: argparse.Namespace) -> int:
    project = open_project(args.project)
    entries = [e for e in project.extensions.listing() if e["kind"] == "policy"]
    rows = []
    for entry in entries:
        found = project.extensions.names("policy")
        ext = project.extensions.get("policy", entry["name"]) if entry["name"] in found else None
        rows.append(
            [
                str(entry["name"]),
                "builtin" if entry["source"] == "builtin" else "project",
                ext.decision if ext is not None else "-",
                ", ".join(ext.grid) if ext is not None and ext.grid else "-",
                str(len(entry["problems"])),
            ]
        )
    for line in _table(_LIST_COLUMNS, rows):
        print(line)
    return 0


def _handle_check(args: argparse.Namespace) -> int:
    from twinflow.extensions.kinds import KINDS

    others = set(KINDS) - {"policy"}
    problems = [p for p in check_project(open_project(args.project)) if p.kind not in others]
    if not problems:
        print("No problems found.")
        return 0
    for problem in problems:
        print(str(problem))
    errors = sum(1 for p in problems if p.severity == "error")
    print(f"{len(problems)} problem(s), {errors} of them errors.")
    return 1 if errors else 0


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}" if abs(value) >= 1000 else f"{sign}${abs(value):,.2f}"


def _number(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:,.2f}"


def _range(stat: Stat, level: float) -> str:
    if stat.low is None or stat.high is None:
        return f"{_money(stat.mean)} (one run, no range)"
    return f"{_money(stat.mean)} ({level:.0%} range {_number(stat.low)} to {_number(stat.high)})"


def _row(row: PolicyResult, level: float) -> list[str]:
    fill = "-" if row.fill_rate is None else f"{row.fill_rate:.1%}"
    return [
        str(row.rank),
        row.policy,
        _number(row.score.mean),
        _range(row.dollars, level),
        fill,
        f"{row.changeovers:.1f}",
    ]


def _observation(project: Project, text: str | None) -> tuple[str, ...] | None:
    if text is None:
        return None
    known = project.extensions.names("observation")
    for part in text.split("+"):
        if part not in known:
            raise UserError(
                f"--observation {part!r} is not a known observation. "
                f"{did_you_mean(part, sorted(known))} Fix: pick one of {', '.join(sorted(known))}."
            )
    return (text,)


def _handle_race(args: argparse.Namespace) -> int:
    folder = Path(args.policies) if args.policies else None
    if folder is not None and not folder.is_dir():
        raise UserError(
            f"{folder} is not a folder. Fix: give a folder that holds policy .py files."
        )
    with staged_project(args.project, policies=folder) as project:
        settings = race_settings(project, args)
        masks = _observation(project, args.observation)
        original = open_project(args.project).folder
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = Path(args.out) if args.out else original / "runs" / f"race-{stamp}"
        print(f"Racing on {settings.episodes} runs per policy (seed {settings.seed}).", flush=True)
        result = race(
            project,
            settings=settings,
            observations=masks,
            progress=lambda line: print(f"  {line}", flush=True),
        )
        out.mkdir(parents=True, exist_ok=True)
        result.write(out)
        report = write_race_report(result, out / "race.html")
    _print_result(result)
    print(f"Report: {report}")
    return 0


def _print_result(result: RaceResult) -> None:
    level = result.settings.interval
    ranking = result.ranking()
    print()
    print(
        f"Policies ranked by {result.score_name} (dollars are always shown), "
        f"under the {result.observation} view."
    )
    for line in _table(_RACE_COLUMNS, [_row(row, level) for row in ranking]):
        print(line)
    for row in ranking:
        if row.dollar_warning:
            print(f"Warning: {row.dollar_warning}")
    if result.skipped:
        print("Skipped policies:")
        for problem in result.skipped:
            print(f"  {problem}")
