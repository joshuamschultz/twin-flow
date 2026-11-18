"""T-239 fix: the race streams its statistics, so memory stays small (COMP-157).

A default molding-press race (12 policies, 200 runs each, top 3 kept) keeps only running
sums and the compact moves of the top policies; Python's own allocations must peak under
150 MB above where they started (tracemalloc). Tuning keeps nothing, so it runs on 3
practice runs instead of 30 to keep the test quick under tracemalloc (before the fix this
race grew by about 435 MB; after, about 14).

The race runs in a child process. tracemalloc and the race itself raise the resident
memory of whatever process runs them, and `tests/planning/test_simulation_performance.py`
checks the lifetime peak (`ru_maxrss`) of the pytest process, so measuring here must not
leave that peak behind.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "molding-press"
LIMIT_MB = 150.0

_CHILD = """
import json, sys, tracemalloc
from dataclasses import replace
from twinflow.extensions.project import Project
from twinflow.extensions.race import race

project = Project.open(sys.argv[1])
tracemalloc.start()
start, _ = tracemalloc.get_traced_memory()
result = race(project, settings=replace(project.config.race, tune_episodes=3))
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
print(json.dumps({
    "grown_mb": (peak - start) / 1e6,
    "ranked": len(result.ranking()),
    "policies": len(project.extensions.names("policy")),
    "top_moves": len(result.moves(result.ranking()[0].policy)),
}))
"""


@pytest.mark.slow  # about 70 s: tracemalloc slows Python down about six times
def test_a_default_race_keeps_memory_small() -> None:
    done = subprocess.run(  # noqa: S603 - fixed interpreter and script, no user input
        [sys.executable, "-c", _CHILD, str(EXAMPLE)],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.strip().splitlines()[-1])
    print(f"race peak growth: {report['grown_mb']:.1f} MB")
    assert report["ranked"] == report["policies"]
    assert report["top_moves"] == 200 * 60
    assert report["grown_mb"] < LIMIT_MB, f"the race grew memory by {report['grown_mb']:.0f} MB"
