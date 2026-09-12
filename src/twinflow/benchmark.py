"""Reproducible, bounded benchmark harness for included TwinFlow cases."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Measured conditions and results; this is not a capacity certification."""

    model: str
    plan: str
    repetitions: int
    seed: int
    limits: dict[str, float | int]
    environment: dict[str, str | int | None]
    wall_seconds: float
    total_events: int
    simulated_time_total: float
    artifact_bytes: int
    cost_proxies: dict[str, float]
    outcomes: dict[str, int]


def run_benchmark(
    model_path: Path,
    plan_path: Path,
    artifact_dir: Path,
    *,
    repetitions: int,
    seed: int,
    max_sim_time: float,
    max_events: int,
    max_wall_seconds: float,
) -> BenchmarkResult:
    """Run bounded replications sequentially and retain all measured artifacts."""
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    numeric_limits = (float(max_sim_time), float(max_events), float(max_wall_seconds))
    if min(numeric_limits) <= 0 or not all(math.isfinite(value) for value in numeric_limits):
        raise ValueError("all benchmark limits must be positive and finite")
    compiled = load_model(str(model_path))
    plan = load_plan(str(plan_path), compiled.registry)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    results = [
        RunDriver(compiled).run(
            plan,
            seed=seed,
            replication_index=index,
            artifact_dir=artifact_dir / f"replication-{index:04d}",
            max_sim_time=max_sim_time,
            max_events=max_events,
            max_wall_seconds=max_wall_seconds,
        )
        for index in range(repetitions)
    ]
    wall_seconds = time.perf_counter() - started
    artifact_bytes = sum(path.stat().st_size for path in artifact_dir.rglob("*") if path.is_file())
    total_events = sum(result.event_count for result in results)
    simulated_time = sum(result.horizon for result in results)
    outcomes: dict[str, int] = {}
    for result in results:
        outcomes[result.termination_reason] = outcomes.get(result.termination_reason, 0) + 1
    return BenchmarkResult(
        model=str(model_path.resolve()),
        plan=str(plan_path.resolve()),
        repetitions=repetitions,
        seed=seed,
        limits={
            "max_sim_time": max_sim_time,
            "max_events": max_events,
            "max_wall_seconds_per_replication": max_wall_seconds,
        },
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or None,
            "logical_cpus": os.cpu_count(),
        },
        wall_seconds=wall_seconds,
        total_events=total_events,
        simulated_time_total=simulated_time,
        artifact_bytes=artifact_bytes,
        cost_proxies={
            "wall_seconds_per_replication": wall_seconds / repetitions,
            "events_per_wall_second": total_events / wall_seconds if wall_seconds else 0.0,
            "artifact_bytes_per_event": artifact_bytes / total_events if total_events else 0.0,
        },
        outcomes=outcomes,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-sim-time", type=float, default=1_000_000.0)
    parser.add_argument("--max-events", type=int, default=1_000_000)
    parser.add_argument("--max-wall-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    result = run_benchmark(
        args.model,
        args.plan,
        args.artifact_dir,
        repetitions=args.repetitions,
        seed=args.seed,
        max_sim_time=args.max_sim_time,
        max_events=args.max_events,
        max_wall_seconds=args.max_wall_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(asdict(result), indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
