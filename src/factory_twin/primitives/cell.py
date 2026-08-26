"""COMP-008 Machine + COMP-009 SetupPolicy.

A capacity-N Location is N Machines, each with independent current-setup state.
SetupPolicy decides whether a changeover is owed and how long it takes.
"""

from __future__ import annotations


class Machine:
    """One named server carrying its own current-setup state."""

    def __init__(self) -> None:
        raise NotImplementedError("T-013")


class SetupPolicy:
    """Resolves a bundle's setup key and looks up changeover time with a flat default."""

    def __init__(self) -> None:
        raise NotImplementedError("T-013")
