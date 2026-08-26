"""COMP-003 RngRegistry — one dedicated numpy stream per stochastic source.

Source keys are code-declared module constants, never derived from config (D-033).
Enforces always-draw-sometimes-discard so a stream position never depends on which
branch executed, which is what keeps common random numbers paired across sweep points.
"""

from __future__ import annotations

# Code-declared source keys. Adding a stochastic source means adding a constant here,
# never computing a key from the model's resource count.
SOURCE_CYCLE_TIME = "cycle_time"
SOURCE_SCRAP = "scrap"
SOURCE_ROUTING = "routing"
SOURCE_BREAKDOWN = "breakdown"

SOURCE_KEYS: tuple[str, ...] = (
    SOURCE_CYCLE_TIME,
    SOURCE_SCRAP,
    SOURCE_ROUTING,
    SOURCE_BREAKDOWN,
)


class RngRegistry:
    """Hands out one numpy Generator per declared source for a (base_seed, rep)."""

    def __init__(self, base_seed: int, replication_index: int) -> None:
        raise NotImplementedError("T-003")
