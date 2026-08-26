"""COMP-016 LocationCompiler — plain-language Location config -> transform/location specs.

The single growth point for new config sugar, and therefore the seam that replaces a
plugin system. Primitives never learn what a scrap rate is (D-044).
"""

from __future__ import annotations


class LocationCompiler:
    """RawModel parse tree -> list[LocationSpec] + routing graph + BOM rollup."""

    def __init__(self) -> None:
        raise NotImplementedError("T-027")
