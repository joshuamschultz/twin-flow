"""Standalone JSON scheduling command; application wiring is intentionally separate.

`twinflow schedule request.json --solver baseline|ortools --time-limit S` reads a v2
`SolveRequest` and prints the `SolveResult` JSON (COMP-014, REQ-083).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from twinflow.scheduling import (
    OptionalDependencyError,
    ORToolsSolver,
    Solver,
    parse_request,
    solve,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="twinflow-schedule")
    parser.add_argument("request", type=Path, help="a schema_version 2.0 SolveRequest JSON file")
    parser.add_argument("--solver", choices=("baseline", "ortools"), default="baseline")
    parser.add_argument("--time-limit", type=float, help="seconds; default: the profile budget")
    args = parser.parse_args(argv)
    parsed = parse_request(args.request.read_bytes())
    if isinstance(parsed, list):
        issues = [issue.model_dump(mode="json") for issue in parsed]
        parser.error(f"invalid SolveRequest: {json.dumps(issues, sort_keys=True)}")
    try:
        selected: Solver | None = ORToolsSolver() if args.solver == "ortools" else None
    except OptionalDependencyError as exc:
        parser.error(str(exc))
    result = solve(parsed, solver=selected, budget_seconds=args.time_limit)
    print(result.model_dump_json())
    return 0 if result.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
