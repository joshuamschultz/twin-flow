"""COMP-161: the table of extension kinds (REQ-216, D-150).

Every drop-in kind is one row: the folder its files live in, the one function (or, for an
engine, the class) each file must define, how `SETTINGS` is read, and what a good return
value looks like. The loader and the checker read this table, so a new kind is one row.

- policy       `policies/`      `decide(state)`            one of `state.actions`
- reward       `rewards/`       `reward(move)`             a finite number
- view         `views/`         `label(state)`             a short, non-empty text
- feature      `features/`      `features(state)`          names mapped to finite numbers
- demand       `demand/`        `demand(period, rng, model)`  product mapped to units >= 0
- observation  `observations/`  `observe(state)`           a policy state (REQ-226)
- engine       `engines/`       `class Engine`             meets the engine contract (REQ-223)

A policy's `SETTINGS` is a grid of values the race may try (REQ-201). Every other kind's
`SETTINGS` holds the values its function is called with; a company changes them by copying
the file (D-151).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

SettingsStyle = Literal["grid", "values"]
MAX_LABEL = 80


@dataclass(frozen=True)
class Kind:
    """One drop-in kind (D-150)."""

    name: str
    folder: str
    function: str
    call: str  # how the function is called, in plain words for messages
    returns: str  # what it must return, in plain words
    settings: SettingsStyle = "values"
    is_class: bool = False

    @property
    def example(self) -> str:
        """A first line the user can copy."""
        if self.is_class:
            return f"class {self.function}:"
        return f"def {self.call}:"


KINDS: Mapping[str, Kind] = {
    kind.name: kind
    for kind in (
        Kind("policy", "policies", "decide", "decide(state)", "one of state.actions", "grid"),
        Kind("reward", "rewards", "reward", "reward(move)", "a number"),
        Kind("view", "views", "label", "label(state)", f"a short text (under {MAX_LABEL} letters)"),
        Kind("feature", "features", "features", "features(state)", "names mapped to numbers"),
        Kind(
            "demand",
            "demand",
            "demand",
            "demand(period, rng, model)",
            "each product mapped to a whole number of units, 0 or more",
        ),
        Kind("observation", "observations", "observe", "observe(state)", "a policy state"),
        Kind(
            "engine",
            "engines",
            "Engine",
            "Engine(model)",
            "an engine with start, actions, step and done",
            is_class=True,
        ),
    )
}


def _is_number(value: object) -> bool:
    return isinstance(value, int | float | np.integer | np.floating) and not isinstance(
        value, bool | np.bool_
    )


def _is_whole(value: object) -> bool:
    return isinstance(value, int | np.integer) and not isinstance(value, bool)


def _finite(value: object) -> bool:
    return _is_number(value) and math.isfinite(float(value))  # type: ignore[arg-type]


def _policy(value: object, allowed: Sequence[str]) -> str | None:
    if isinstance(value, str) and value in allowed:
        return None
    return f"decide returned {value!r}, which is not one of: {', '.join(allowed)}."


def _reward(value: object) -> str | None:
    if _finite(value):
        return None
    return f"reward returned {value!r}; it must be a real number, like 12.5."


def _view(value: object) -> str | None:
    if isinstance(value, str) and value and len(value) < MAX_LABEL:
        return None
    if isinstance(value, str):
        size = "empty" if not value else f"{len(value)} letters long"
        return f"label returned a text that is {size}; keep it 1 to {MAX_LABEL - 1} letters."
    return f"label returned {value!r}; it must be a short text, like 'grey:low'."


def _feature(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return f"features returned {value!r}; it must be names mapped to numbers."
    for key, number in value.items():
        if not isinstance(key, str) or not key:
            return f"features returned the name {key!r}; every name must be a text."
        if not _finite(number):
            return f"features returned {number!r} for {key!r}; it must be a real number."
    return None


def _demand(value: object, products: Sequence[str]) -> str | None:
    if not isinstance(value, Mapping):
        return f"demand returned {value!r}; it must map each product to units."
    missing = [p for p in products if p not in value]
    unknown = [str(p) for p in value if p not in products]
    if missing:
        return f"demand gave no units for {', '.join(missing)}."
    if unknown:
        return f"demand named {', '.join(unknown)}, which are not products of this model."
    for product, units in value.items():
        if not _is_whole(units) or int(units) < 0:
            return f"demand gave {units!r} units of {product}; use a whole number, 0 or more."
    return None


def check_return(
    kind: str,
    value: object,
    *,
    allowed: Sequence[str] = (),
    products: Sequence[str] = (),
) -> str | None:
    """None when `value` is a good return for `kind`, else what is wrong in plain words.

    `allowed` is the action list (policies); `products` the model's products (demand).
    Observations and engines are checked by the checker, which knows their types.
    """
    if kind == "policy":
        return _policy(value, allowed)
    if kind == "reward":
        return _reward(value)
    if kind == "view":
        return _view(value)
    if kind == "feature":
        return _feature(value)
    if kind == "demand":
        return _demand(value, products)
    return None
