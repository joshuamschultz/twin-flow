from __future__ import annotations

import json
from pathlib import Path

from twinflow.benchmark import main


def test_benchmark_reports_bounded_conditions_and_cost_proxies(tmp_path: Path) -> None:
    output = tmp_path / "benchmark.json"
    artifacts = tmp_path / "artifacts"
    assert (
        main(
            [
                "examples/active-control/model.yaml",
                "--plan",
                "examples/active-control/plan.csv",
                "--output",
                str(output),
                "--artifact-dir",
                str(artifacts),
                "--repetitions",
                "1",
                "--max-sim-time",
                "1000000",
                "--max-events",
                "100000",
                "--max-wall-seconds",
                "30",
            ]
        )
        == 0
    )
    report = json.loads(output.read_text())
    assert report["repetitions"] == 1
    assert report["total_events"] > 0
    assert report["artifact_bytes"] > 0
    assert report["limits"]["max_events"] == 100000
    assert report["cost_proxies"]["events_per_wall_second"] > 0
    assert "python" in report["environment"]
