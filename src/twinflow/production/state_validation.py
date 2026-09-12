"""Small validation helpers shared by production state ledgers."""

from __future__ import annotations

import math


def validate_quantity(value: float, *, field: str = "quantity") -> float:
    """Return a finite non-negative quantity or raise ``ValueError``."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return result


def validate_positive(value: float, *, field: str = "quantity") -> float:
    """Return a finite positive quantity or raise ``ValueError``."""

    result = validate_quantity(value, field=field)
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def validate_uom(value: str, *, field: str = "uom") -> str:
    """Return a non-empty unit string."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def require_same_uom(expected: str, actual: str) -> None:
    """Raise when two quantities cannot be combined without conversion."""

    if expected != actual:
        raise ValueError(f"unit mismatch: expected {expected!r}, got {actual!r}")
