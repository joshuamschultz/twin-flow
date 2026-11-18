"""K table: model, seed table, lookup and store (T-163, COMP-025, REQ-123, D-082, DEV-15).

- `KEntry` / `KTable` (Pydantic, frozen, extra forbid; SDD Data Model KTable).
- `seed_table()`: tau {0.2, 0.4, 0.6, 0.8} x R {0.2, 0.5, 0.8, 1.2} x eta {0.05, 0.2, 0.5,
  1.0} x m {1, 2, 4, 8}, K from the p.376 formulas and clamps (`kernel.stats.atcs_k`).
- `lookup(table, stats)`: an exact (tau, R, eta, m) match returns that entry, else
  inverse-distance weighting (power 1) over the 4 nearest entries in the space
  (tau, R, eta, log2 m); ties in distance break by table order, so the result is
  deterministic.
- `as_k_lookup(table)`: a picklable `TableLookup`, the `k_lookup` ATCS takes as a
  constructor argument (`registry.get("atcs", k_lookup=...)`, source `table`).
- `KTableStore(path)`: `{"version": 1, "digest", "entries"}` written to a temp file in the
  same directory then `os.replace`; the digest is sha256 of the canonical JSON of the entries
  (`json.dumps(entries, sort_keys=True, separators=(",", ":"))`). It is a corruption check,
  not a signature (SDD: table signing is out of scope); a mismatch raises
  `KTableIntegrityError`.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from twinflow.scheduling.kernel.stats import K1_RANGE, K2_RANGE, atcs_k

SEED_TAUS = (0.2, 0.4, 0.6, 0.8)
SEED_RS = (0.2, 0.5, 0.8, 1.2)
SEED_ETAS = (0.05, 0.2, 0.5, 1.0)
SEED_MS = (1, 2, 4, 8)
NEIGHBORS = 4

Point = tuple[float, float, float, float]
"""A position in the lookup space (tau, R, eta, log2 m)."""


class StatsPoint(Protocol):
    """Anything carrying a center's ATCS statistics (`InstanceStats`, `AtcsRun`, `KEntry`)."""

    @property
    def tau(self) -> float: ...
    @property
    def R(self) -> float: ...  # noqa: N802
    @property
    def eta(self) -> float: ...
    @property
    def m(self) -> int: ...


class KEntry(BaseModel):
    """One learned (or seeded) K pair at a statistics point; `runs` counts its updates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tau: float
    R: float = Field(ge=0)
    eta: float = Field(ge=0)
    m: int = Field(ge=1)
    K1: float = Field(ge=K1_RANGE[0], le=K1_RANGE[1])
    K2: float = Field(ge=K2_RANGE[0], le=K2_RANGE[1])
    runs: int = Field(default=0, ge=0)


class KTable(BaseModel):
    """The K table ATCS reads through `as_k_lookup` (version 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    entries: tuple[KEntry, ...]


class KTableIntegrityError(ValueError):
    """A stored table whose digest is missing or does not match its entries."""


class EmptyKTableError(ValueError):
    """A lookup on a table without entries."""


def seed_table() -> KTable:
    """The 256-entry p.376 seed table."""
    entries = []
    for tau, r, eta, m in itertools.product(SEED_TAUS, SEED_RS, SEED_ETAS, SEED_MS):
        k1, k2 = atcs_k(tau, r, eta)
        entries.append(KEntry(tau=tau, R=r, eta=eta, m=m, K1=k1, K2=k2))
    return KTable(entries=tuple(entries))


def point_of(stats: StatsPoint) -> Point:
    """(tau, R, eta, log2 m); m below 1 (a center without machines) sits at m = 1."""
    return (stats.tau, stats.R, stats.eta, math.log2(max(stats.m, 1)))


def lookup(table: KTable, stats: StatsPoint) -> tuple[float, float]:
    """(K1, K2) at the stats point: the exact entry, else IDW over the 4 nearest."""
    return _lookup_at(table, stats.tau, stats.R, stats.eta, stats.m)


def _lookup_at(table: KTable, tau: float, R: float, eta: float, m: int) -> tuple[float, float]:  # noqa: N803
    if not table.entries:
        raise EmptyKTableError("the K table has no entries")
    query = (tau, R, eta, math.log2(max(m, 1)))
    distances = ((math.dist(point_of(e), query), i) for i, e in enumerate(table.entries))
    nearest = heapq.nsmallest(NEIGHBORS, distances)
    closest_distance, closest = nearest[0]
    if closest_distance == 0.0:
        entry = table.entries[closest]
        return entry.K1, entry.K2
    weights = [(1.0 / d, table.entries[i]) for d, i in nearest]
    total = sum(w for w, _ in weights)
    k1 = sum(w * e.K1 for w, e in weights) / total
    k2 = sum(w * e.K2 for w, e in weights) / total
    return k1, k2


@dataclass(frozen=True, slots=True)
class TableLookup:
    """`k_lookup(tau, R, eta, m) -> (K1, K2)` over a table; picklable for the process pool."""

    table: KTable

    def __call__(self, tau: float, R: float, eta: float, m: int) -> tuple[float, float]:  # noqa: N803
        return _lookup_at(self.table, tau, R, eta, m)


def as_k_lookup(table: KTable) -> TableLookup:
    """The `k_lookup` ATCS accepts (`registry.get("atcs", k_lookup=as_k_lookup(table))`)."""
    return TableLookup(table)


def _digest(entries: Any) -> str:
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class KTableStore:
    """A K table as one JSON file: atomic replace on save, digest check on load."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def save(self, table: KTable) -> None:
        """Write to a temp file in the same directory, then `os.replace` it over the path.

        A failure before the replace leaves the previous file untouched and no temp file.
        """
        entries = [e.model_dump(mode="json") for e in table.entries]
        document = {"version": table.version, "digest": _digest(entries), "entries": entries}
        fd, temp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def load(self) -> KTable:
        """The stored table; `KTableIntegrityError` when the digest is absent or wrong."""
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "entries" not in data:
            raise KTableIntegrityError(f"{self._path} is not a K table document")
        if data.get("digest") != _digest(data["entries"]):
            raise KTableIntegrityError(f"{self._path} digest does not match its entries")
        return KTable.model_validate({"version": data.get("version"), "entries": data["entries"]})
