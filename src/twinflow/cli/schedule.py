"""Standalone JSON scheduling command; application wiring is intentionally separate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from twinflow.scheduling import SchedulingProblem, problem_from_dict, solve
from twinflow.scheduling.ortools import OptionalDependencyError, ORToolsSolver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="twinflow-schedule")
    parser.add_argument("problem", type=Path)
    parser.add_argument("--solver", choices=("baseline", "ortools"), default="baseline")
    parser.add_argument("--time-limit", type=float)
    args = parser.parse_args(argv)
    problem = _load(args.problem)
    try:
        selected = ORToolsSolver() if args.solver == "ortools" else None
        result = solve(problem, solver=selected, time_limit_seconds=args.time_limit)
    except OptionalDependencyError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "status": result.status,
                "starts": result.starts,
                "ends": result.ends,
                "resources": result.resources,
                "objective_value": result.objective_value,
                "verified": result.verified,
            },
            sort_keys=True,
        )
    )
    return 0 if result.status in {"FEASIBLE", "OPTIMAL"} else 1


def _load(path: Path) -> SchedulingProblem:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return problem_from_dict(raw)


if __name__ == "__main__":
    raise SystemExit(main())
