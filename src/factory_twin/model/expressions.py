"""COMP-015 ExpressionSandbox — the ONLY importer of simpleeval (D-002), pinned >=1.0.5.

Compiles every config expression behind a whitelist with project-imposed depth and
length limits and the MAX_POWER guard active. Wraps errors naming the offending path.
"""

from __future__ import annotations

import simpleeval  # noqa: F401 — this module is intentionally the sole simpleeval importer.


class ExpressionError(Exception):
    """Raised on parse, depth, length or whitelist violation; names the config path."""


class ExpressionSandbox:
    """Compiles an expression string into a callable over a declared attribute namespace."""

    def __init__(self) -> None:
        raise NotImplementedError("T-025")
