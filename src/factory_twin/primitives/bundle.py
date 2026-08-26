"""COMP-004 Bundle — the immutable unit of flow: qty, thing, uom, attributes."""

from __future__ import annotations


class Bundle:
    """Immutable value object; split/merge return new Bundles, never mutate."""

    def __init__(self) -> None:
        raise NotImplementedError("T-007")
