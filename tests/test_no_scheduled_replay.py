"""TF-004 D-086: the material-flow DES has no scheduled-replay path left in src/."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from twinflow.primitives.location import Location

SRC = Path(__file__).resolve().parent.parent / "src"
DELETED_SYMBOLS = (
    "ScheduledFiring",
    "execution_schedule",
    "check_execution_schedule",
    "firings_from_plan",
    "schedule_execution",
    "submit_schedule_plan",
    "_simulation_blocker",
    "_plan_firings",
    "schedule_trace",
    "_piece_firings",
    "verified_schedule",
    "enforced_verified_plan",
    "_run_scheduled_machine",
    "scheduled_firings",
    "schedule_plan_simulate",
)
PATTERN = re.compile(r"\b(" + "|".join(DELETED_SYMBOLS) + r")\b")


def test_no_deleted_replay_symbol_remains_in_src() -> None:
    hits = [
        f"{path.relative_to(SRC)}:{number}: {line.strip()}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if PATTERN.search(line)
    ]
    assert not hits, "scheduled-replay symbols remain:\n" + "\n".join(hits)


def test_location_fire_takes_no_scheduled_parameter() -> None:
    assert "scheduled" not in inspect.signature(Location._fire).parameters
