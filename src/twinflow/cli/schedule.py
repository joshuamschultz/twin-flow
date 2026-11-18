"""Standalone JSON scheduling command; application wiring is intentionally separate.

`twinflow schedule request.json --solver NAME --time-limit S` reads a v2 `SolveRequest`
and prints the `SolveResult` JSON (COMP-014, REQ-083). `NAME` is any solver the registry
knows (`twinflow.application.solver_port`, COMP-028, REQ-088); another name exits 2 with
one error line naming the registered solvers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import NoReturn

from twinflow.application.solver_port import UnsupportedSolver, build_solver
from twinflow.scheduling import ScheduleIssue, parse_request, solve
from twinflow.scheduling.seam import OptionalDependencyError

MAX_TIME_LIMIT_SECONDS = 30.0  # the same cap the REST and MCP surfaces enforce


def _refuse(parser: argparse.ArgumentParser, message: str, code: str, path: str) -> NoReturn:
    issue = ScheduleIssue(code=code, path=path, message=message)
    parser.error(
        f"invalid SolveRequest: {json.dumps([issue.model_dump(mode='json')], sort_keys=True)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="twinflow-schedule")
    parser.add_argument("request", type=Path, help="a schema_version 2.0 SolveRequest JSON file")
    parser.add_argument("--solver", default="baseline", help="a registered solver name")
    parser.add_argument("--time-limit", type=float, help="seconds; default: the profile budget")
    args = parser.parse_args(argv)
    if args.time_limit is not None and args.time_limit > MAX_TIME_LIMIT_SECONDS:
        message = (
            f"budget_seconds must be at most {MAX_TIME_LIMIT_SECONDS:g}, got {args.time_limit}"
        )
        _refuse(parser, message, "budget", "budget_seconds")
    try:
        content = args.request.read_bytes()
    except OSError as exc:
        _refuse(parser, f"cannot read {args.request}: {exc.strerror}", "unreadable", "request")
    parsed = parse_request(content)
    if isinstance(parsed, list):
        issues = [issue.model_dump(mode="json") for issue in parsed]
        parser.error(f"invalid SolveRequest: {json.dumps(issues, sort_keys=True)}")
    try:
        selected = build_solver(args.solver)
    except (UnsupportedSolver, OptionalDependencyError) as exc:
        parser.error(str(exc))
    result = solve(parsed, solver=selected, budget_seconds=args.time_limit)
    print(result.model_dump_json())
    return 0 if result.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
