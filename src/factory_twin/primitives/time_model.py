"""COMP-011 TimeModel — cycle time per (Location, part type).

Distribution, rate-based, or attribute-scaled, with an optional load/run/unload split.
Every draw comes from the cycle_time source stream (COMP-003).
"""

from __future__ import annotations


class TimeModel:
    """Produces PhaseTimes {load, run, unload}; a single value collapses to run."""

    def __init__(self) -> None:
        raise NotImplementedError("T-015")
