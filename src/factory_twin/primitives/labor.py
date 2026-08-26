"""COMP-012 LaborPool — a named, skilled pool of operators gated by shift calendars.

Unload requests outrank fresh load requests so machines get unblocked before new lots.
"""

from __future__ import annotations


class LaborPool:
    """request(skill, priority) -> operator handle; shift calendar gates availability."""

    def __init__(self) -> None:
        raise NotImplementedError("T-015")
