"""COMP-005 PartTypeRegistry — declared part types, their typed attributes and single uom."""

from __future__ import annotations


class PartTypeRegistry:
    """Lookup by part-type id -> {attributes, uom}; raises on unknown id/attribute."""

    def __init__(self) -> None:
        raise NotImplementedError("T-007")
