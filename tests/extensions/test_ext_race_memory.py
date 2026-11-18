"""T-239 fix: the race streams its statistics, so memory stays small (COMP-157).

A default molding-press race (12 policies, 200 runs each, top 3 kept) keeps only running
sums and the compact moves of the top policies; Python's own allocations must peak under
150 MB above where they started (tracemalloc, not the process peak, so other tests do not
count). Tuning keeps nothing, so it runs on 3 practice runs instead of 30 to keep the test
quick under tracemalloc (before the fix this race grew by about 435 MB; after, about 14).
"""

from __future__ import annotations

import tracemalloc
from dataclasses import replace
from pathlib import Path

import pytest

from twinflow.extensions.project import Project
from twinflow.extensions.race import race

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "molding-press"
LIMIT_MB = 150.0


@pytest.mark.slow  # about 70 s: tracemalloc slows Python down about six times
def test_a_default_race_keeps_memory_small() -> None:
    project = Project.open(EXAMPLE)
    tracemalloc.start()
    try:
        start, _ = tracemalloc.get_traced_memory()
        result = race(project, settings=replace(project.config.race, tune_episodes=3))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    grown = (peak - start) / 1e6
    print(f"race peak growth: {grown:.1f} MB")
    assert len(result.ranking()) == len(project.extensions.names("policy"))
    assert len(result.moves(result.ranking()[0].policy)) == 200 * 60
    assert grown < LIMIT_MB, f"the race grew memory by {grown:.0f} MB"
