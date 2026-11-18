"""Plain-word problems found while reading a state-layer config section (REQ-213).

`twinflow.state` imports no other twinflow package (D-131), so it carries its own small
issue record instead of `twinflow.model.validate.ValidationError`.
"""

from __future__ import annotations

import difflib
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "info"]


@dataclass(frozen=True)
class Issue:
    """One problem: where (`path`), what is wrong, and how to fix it."""

    path: str
    message: str
    severity: Severity = "error"
    suggestion: str = ""


def did_you_mean(word: str, choices: Iterable[str]) -> str:
    """A 'Did you mean ...?' hint, or a list of the valid choices."""
    options = sorted(choices)
    close = difflib.get_close_matches(word, options, n=1)
    if close:
        return f"Did you mean '{close[0]}'?"
    return f"Use one of: {', '.join(options)}." if options else ""


def unknown_keys(raw: dict[object, object], allowed: Iterable[str], path: str) -> list[Issue]:
    """One error per key of `raw` that is not in `allowed`."""
    allowed = tuple(allowed)
    return [
        Issue(
            f"{path}.{key}",
            f"'{key}' is not a known field here.",
            "error",
            did_you_mean(str(key), allowed),
        )
        for key in raw
        if key not in allowed
    ]


def number_or_issue(value: object, path: str) -> tuple[float | None, Issue | None]:
    """A finite, non-negative number, or an error naming `path`."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None, Issue(
            path, f"{value!r} is not a number.", "error", "Write a number, like 12.5."
        )
    if not math.isfinite(value):
        return None, Issue(
            path, f"{value!r} is not a real number.", "error", "Write a number, like 12.5."
        )
    if value < 0:
        return None, Issue(path, f"{value} is below zero.", "error", "Use 0 or more.")
    return float(value), None
