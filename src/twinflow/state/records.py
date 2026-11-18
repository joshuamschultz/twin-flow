"""COMP-144: the plant-state records every engine emits (REQ-188, REQ-189).

One `Move` shape for every engine (D-130): the state before, the action, what happened, the
state after and the dollar ledger. `flatten` turns a move into one flat row with dotted
column names (`before.stock.X`, `after.setup.L`, `events.*`, `ledger.*`); `unflatten` is its
exact inverse, so a moves table read back from disk gives equal `Move` objects.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from types import MappingProxyType
from typing import Any, Literal, cast, get_args

MoveKind = Literal["period", "decision", "event"]
MOVE_KINDS: tuple[str, ...] = get_args(MoveKind)

LEDGER_PARTS: tuple[str, ...] = ("revenue", "production", "setup", "holding", "lost_sales", "late")

_EMPTY: Mapping[str, Any] = MappingProxyType({})


class RecordError(ValueError):
    """A record value is malformed; the message names the field."""


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RecordError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise RecordError(f"{name} is {value!r}; numbers must be finite")
    return number


def _whole(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RecordError(f"{name} must be a whole number, got {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise RecordError(f"{name} must be a whole number, got {value!r}")
    return int(value)


def _frozen(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(values))


def _floats(values: Mapping[str, object], name: str) -> Mapping[str, float]:
    return _frozen({str(k): _finite(v, f"{name}[{k!r}]") for k, v in values.items()})


def _ints(values: Mapping[str, object], name: str) -> Mapping[str, int]:
    return _frozen({str(k): _whole(v, f"{name}[{k!r}]") for k, v in values.items()})


def _setups(values: Mapping[str, object]) -> Mapping[str, str | None]:
    out: dict[str, str | None] = {}
    for key, value in values.items():
        if value is not None and not isinstance(value, str):
            raise RecordError(f"setup[{key!r}] must be a thing id or None, got {value!r}")
        out[str(key)] = value
    return _frozen(out)


def _extras(values: Mapping[str, object]) -> Mapping[str, float | str]:
    return _frozen(
        {
            str(k): v if isinstance(v, str) else _finite(v, f"extra[{k!r}]")
            for k, v in values.items()
        }
    )


@dataclass(frozen=True)
class Snapshot:
    """What the plant looks like at one instant (REQ-189).

    Mappings are copied into read-only views; numbers are normalised (stock and `extra`
    numbers to float, counts to int) so equal plants give equal digests.
    """

    t: float
    period: int
    stock: Mapping[str, float] = field(default=_EMPTY)
    setup: Mapping[str, str | None] = field(default=_EMPTY)
    queue: Mapping[str, int] = field(default=_EMPTY)
    wip: int = 0
    open_orders: int = 0
    labor: Mapping[str, int] = field(default=_EMPTY)
    extra: Mapping[str, float | str] = field(default=_EMPTY)

    def __post_init__(self) -> None:
        put = object.__setattr__
        put(self, "t", _finite(self.t, "t"))
        put(self, "period", _whole(self.period, "period"))
        put(self, "stock", _floats(self.stock, "stock"))
        put(self, "setup", _setups(self.setup))
        put(self, "queue", _ints(self.queue, "queue"))
        put(self, "wip", _whole(self.wip, "wip"))
        put(self, "open_orders", _whole(self.open_orders, "open_orders"))
        put(self, "labor", _ints(self.labor, "labor"))
        put(self, "extra", _extras(self.extra))

    def __hash__(self) -> int:
        return hash(self.digest)

    @cached_property
    def digest(self) -> str:
        """sha256 of the canonical JSON form: stable across mapping order (REQ-189)."""
        text = json.dumps(
            snapshot_to_dict(self), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Ledger:
    """Dollars for one move, split by cause. Costs are positive numbers (D-130)."""

    revenue: float = 0.0
    production: float = 0.0
    setup: float = 0.0
    holding: float = 0.0
    lost_sales: float = 0.0
    late: float = 0.0

    def __post_init__(self) -> None:
        for part in LEDGER_PARTS:
            object.__setattr__(self, part, _finite(getattr(self, part), f"ledger.{part}"))

    @property
    def total(self) -> float:
        """Revenue less every cost."""
        return (
            self.revenue - self.production - self.setup - self.holding - self.lost_sales - self.late
        )

    def __add__(self, other: Ledger) -> Ledger:
        if not isinstance(other, Ledger):
            return NotImplemented
        return Ledger(**{p: getattr(self, p) + getattr(other, p) for p in LEDGER_PARTS})

    def __radd__(self, other: object) -> Ledger:
        if other == 0:  # lets the builtin `sum` start from 0
            return self
        return NotImplemented


@dataclass(frozen=True)
class Move:
    """One step of any engine: state before, action, what happened, state after (REQ-188)."""

    index: int
    before: Snapshot
    action: str | None
    actor: str | None
    kind: MoveKind
    events: Mapping[str, float]
    after: Snapshot
    ledger: Ledger

    def __post_init__(self) -> None:
        if self.kind not in MOVE_KINDS:
            raise RecordError(f"kind must be one of {', '.join(MOVE_KINDS)}, got {self.kind!r}")
        object.__setattr__(self, "index", _whole(self.index, "index"))
        object.__setattr__(self, "events", _floats(self.events, "events"))

    def __hash__(self) -> int:
        return hash((self.index, self.before, self.action, self.actor, self.kind, self.after))


# --- dict and flat-row forms -------------------------------------------------------------------

_SNAPSHOT_SCALARS = ("t", "period", "wip", "open_orders")
_SNAPSHOT_MAPS = ("stock", "setup", "queue", "labor", "extra")


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    """A plain JSON-ready dict of a snapshot."""
    out: dict[str, Any] = {name: getattr(snapshot, name) for name in _SNAPSHOT_SCALARS}
    out.update({name: dict(getattr(snapshot, name)) for name in _SNAPSHOT_MAPS})
    return out


def snapshot_from_dict(data: Mapping[str, Any]) -> Snapshot:
    """Inverse of `snapshot_to_dict`; a missing or malformed field raises `RecordError`."""
    missing = [name for name in (*_SNAPSHOT_SCALARS, *_SNAPSHOT_MAPS) if name not in data]
    if missing:
        raise RecordError(f"snapshot is missing field(s): {', '.join(missing)}")
    for name in _SNAPSHOT_MAPS:
        if not isinstance(data[name], Mapping):
            raise RecordError(f"snapshot field {name} must be a mapping")
    return Snapshot(**{name: data[name] for name in (*_SNAPSHOT_SCALARS, *_SNAPSHOT_MAPS)})


def flatten_snapshot(snapshot: Snapshot, prefix: str) -> dict[str, Any]:
    """`prefix.t`, `prefix.stock.<thing>`, ... for one snapshot."""
    row: dict[str, Any] = {
        f"{prefix}.{name}": getattr(snapshot, name) for name in _SNAPSHOT_SCALARS
    }
    for name in _SNAPSHOT_MAPS:
        for key, value in getattr(snapshot, name).items():
            row[f"{prefix}.{name}.{key}"] = value
    return row


def unflatten_snapshot(row: Mapping[str, Any], prefix: str) -> Snapshot:
    """Inverse of `flatten_snapshot`.

    A null `setup` cell reads back as `None` (an idle line); a null cell in any other
    mapping means the key is absent (a wide table holds the union of keys over many rows).
    """
    maps: dict[str, dict[str, Any]] = {name: {} for name in _SNAPSHOT_MAPS}
    for column, value in row.items():
        head, _, rest = column.partition(".")
        if head != prefix:
            continue
        name, _, key = rest.partition(".")
        if name in maps and key and (value is not None or name == "setup"):
            maps[name][key] = value
    missing = [name for name in _SNAPSHOT_SCALARS if f"{prefix}.{name}" not in row]
    if missing:
        columns = ", ".join(f"{prefix}.{name}" for name in missing)
        raise RecordError(f"row is missing column(s): {columns}")
    data: dict[str, Any] = {name: row[f"{prefix}.{name}"] for name in _SNAPSHOT_SCALARS}
    data.update(maps)
    return snapshot_from_dict(data)


def flatten(move: Move) -> dict[str, Any]:
    """One flat row for a move, with the SDD column names (moves.parquet)."""
    row: dict[str, Any] = {
        "index": move.index,
        "kind": move.kind,
        "actor": move.actor,
        "action": move.action,
    }
    row.update(flatten_snapshot(move.before, "before"))
    row.update({f"events.{key}": value for key, value in move.events.items()})
    row.update(flatten_snapshot(move.after, "after"))
    row.update({f"ledger.{part}": getattr(move.ledger, part) for part in LEDGER_PARTS})
    row["ledger.total"] = move.ledger.total
    return row


def unflatten(row: Mapping[str, Any]) -> Move:
    """Inverse of `flatten`. Columns it does not own (run_id, episode, policy) are ignored."""
    events = {
        column.removeprefix("events."): value
        for column, value in row.items()
        if column.startswith("events.") and value is not None
    }
    ledger = Ledger(**{part: row.get(f"ledger.{part}") or 0.0 for part in LEDGER_PARTS})
    return Move(
        index=row["index"],
        before=unflatten_snapshot(row, "before"),
        action=row.get("action"),
        actor=row.get("actor"),
        kind=cast(MoveKind, row["kind"]),
        events=events,
        after=unflatten_snapshot(row, "after"),
        ledger=ledger,
    )
