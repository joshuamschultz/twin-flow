"""COMP-007 Location + COMP-013 PullRule.

Location is the floor node and executor of the fixed eight-step order of operations:
pull from queue, acquire machine then operator, consume material, charge setup,
charge run time, apply scrap rate, emit outputs, release operator then machine.

PullRule decides what a Location takes from its queue and how much (batch threshold in
the thing's own uom); with no rule declared, arrival order applies and the fact is
recorded for the Assumptions block.
"""

from __future__ import annotations


class PullRule:
    """Selects the list[Bundle] to process as one job; empty when nothing is eligible."""

    def __init__(self) -> None:
        raise NotImplementedError("T-017")


class Location:
    """Executes the fixed eight-step order of operations; one event record per firing."""

    def __init__(self) -> None:
        raise NotImplementedError("T-021")
