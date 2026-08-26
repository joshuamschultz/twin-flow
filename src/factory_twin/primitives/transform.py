"""COMP-010 Transform — the one conversion contract: bundles in -> bundles out.

Absorbs the former Transform/Split/Assemble/Batch/Yield archetypes (D-043).
Remainders and scrap are ordinary output bundles, never special cases.
"""

from __future__ import annotations


class Transform:
    """Applies a compiled TransformSpec to a list of input Bundles."""

    def __init__(self) -> None:
        raise NotImplementedError("T-009")
