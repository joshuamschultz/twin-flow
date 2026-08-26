"""COMP-004 Bundle — the immutable unit of flow: qty, thing, uom, attributes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

AttrValue = float | str | bool


@dataclass(frozen=True)
class Bundle:
    """Immutable value object; split/merge return new Bundles, never mutate."""

    qty: float
    thing: str
    uom: str
    attrs: Mapping[str, AttrValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Freeze the attrs mapping itself so item assignment also raises.
        object.__setattr__(self, "attrs", MappingProxyType(dict(self.attrs)))

    def with_qty(self, new_qty: float) -> Bundle:
        """Return a new Bundle with new_qty, leaving this one unchanged."""
        return Bundle(qty=new_qty, thing=self.thing, uom=self.uom, attrs=self.attrs)

    def split(self, qty: float) -> tuple[Bundle, Bundle]:
        """Split into two new Bundles of qty and (self.qty - qty).

        Raises ValueError if qty exceeds this bundle's qty.
        """
        if qty > self.qty:
            raise ValueError(f"split qty {qty} exceeds bundle qty {self.qty}")
        first = self.with_qty(qty)
        second = self.with_qty(self.qty - qty)
        return first, second

    def merge(self, other: Bundle) -> Bundle:
        """Return a new Bundle combining this one and other by summing qty.

        Raises ValueError if thing or uom mismatch.
        """
        if self.thing != other.thing:
            raise ValueError(
                f"cannot merge bundles with mismatched thing: {self.thing!r} != {other.thing!r}"
            )
        if self.uom != other.uom:
            raise ValueError(
                f"cannot merge bundles with mismatched uom: {self.uom!r} != {other.uom!r}"
            )
        return self.with_qty(self.qty + other.qty)
