"""COMP-017 ModelValidator — the only safety net, run before any plan exists.

Checks run in order: schema, graph, expression, unit, field-combination; reports every
failure rather than stopping at the first. Each failure names the offending config path.
"""

from __future__ import annotations


class ValidationError:
    """One validation failure, naming the offending config path."""


class ModelValidator:
    def __init__(self) -> None:
        raise NotImplementedError("T-029")
