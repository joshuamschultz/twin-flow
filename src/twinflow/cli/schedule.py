"""Standalone JSON scheduling command; application wiring is intentionally separate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from twinflow.scheduling import Operation, ResourceWindow, SchedulingProblem, solve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="twinflow-schedule")
    parser.add_argument("problem", type=Path)
    args = parser.parse_args(argv)
    problem = _load(args.problem)
    result = solve(problem)
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
    operations = tuple(
        Operation(
            item["id"],
            item["duration"],
            tuple(item.get("predecessors", [])),
            tuple(item.get("eligible_resources", [])),
            frozenset(item.get("required_qualifications", [])),
            item.get("release_time", 0),
            item.get("material_ready_time", 0),
            item.get("document_ready_time", 0),
            item.get("frozen_start"),
            item.get("frozen_resource"),
        )
        for item in raw["operations"]
    )
    resources = tuple(
        ResourceWindow(
            item["resource_id"],
            item["start"],
            item["end"],
            frozenset(item.get("qualifications", [])),
        )
        for item in raw["resources"]
    )
    return SchedulingProblem(
        operations, resources, raw.get("objective", "makespan"), raw.get("deadline")
    )


if __name__ == "__main__":
    raise SystemExit(main())
