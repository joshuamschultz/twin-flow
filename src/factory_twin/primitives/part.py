"""COMP-005 PartTypeRegistry — declared part types, their typed attributes and single uom."""

from __future__ import annotations

from typing import TypedDict


class PartTypeSpec(TypedDict):
    """Declaration for one part type: its named, typed attributes and its uom."""

    attributes: dict[str, type]
    uom: str


class PartTypeRegistry:
    """Lookup by part-type id -> {attributes, uom}; raises on unknown id/attribute."""

    def __init__(self, part_types: dict[str, PartTypeSpec]) -> None:
        self._part_types = part_types

    def attributes(self, part_id: str) -> dict[str, type]:
        """Return the declared {attr_name: type} mapping for part_id.

        Raises KeyError if part_id is not declared.
        """
        return self._part_types[part_id]["attributes"]

    def uom(self, part_id: str) -> str:
        """Return the single declared unit of measure for part_id.

        Raises KeyError if part_id is not declared.
        """
        return self._part_types[part_id]["uom"]

    def attribute_type(self, part_id: str, attr_name: str) -> type:
        """Return the declared type of attr_name on part_id.

        Raises KeyError if part_id is not declared or attr_name is not declared
        on it — never returns None, since a validator relies on this to prove an
        expression references a real attribute.
        """
        return self.attributes(part_id)[attr_name]
