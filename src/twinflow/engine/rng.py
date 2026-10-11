"""COMP-003 RngRegistry and COMP-130 keyed draws.

`RngRegistry` hands out one dedicated numpy stream per stochastic source. Source keys
are code-declared module constants, never derived from config (D-033). It enforces
always-draw-sometimes-discard so a stream position never depends on which branch
executed, which is what keeps common random numbers paired across sweep points.

`keyed_uniform` is the counter-based alternative for the plan executor: a draw is a hash
of (seed, replication, source, entity key), so an entity gets the same draw whatever
order it is reached in (D-095).
"""

from __future__ import annotations

import hashlib

import numpy as np

# Code-declared source keys. Adding a stochastic source means adding a constant here,
# never computing a key from the model's resource count.
SOURCE_CYCLE_TIME = "cycle_time"
SOURCE_SCRAP = "scrap"
SOURCE_ROUTING = "routing"
SOURCE_BREAKDOWN = "breakdown"
SOURCE_ABSENCE = "absence"

SOURCE_KEYS: tuple[str, ...] = (
    SOURCE_CYCLE_TIME,
    SOURCE_SCRAP,
    SOURCE_ROUTING,
    SOURCE_BREAKDOWN,
    SOURCE_ABSENCE,
)

# Fixed per-source index used to key each source's independent SeedSequence entropy.
# Never derived from a model/config count — the position in SOURCE_KEYS is stable
# code, not data (D-033).
_SOURCE_INDEX: dict[str, int] = {source: index for index, source in enumerate(SOURCE_KEYS)}

# Keyed sources for `keyed_uniform` (D-095), disjoint from SOURCE_KEYS. The string values
# feed the hash: renaming one changes every stored replication.
SOURCE_RUN_FACTOR = "run_factor"
SOURCE_SETUP_FACTOR = "setup_factor"
SOURCE_FAILURE = "failure"
SOURCE_REPAIR = "repair"
SOURCE_OUTAGE = "outage"

KEYED_SOURCES: tuple[str, ...] = (
    SOURCE_RUN_FACTOR,
    SOURCE_SETUP_FACTOR,
    SOURCE_FAILURE,
    SOURCE_REPAIR,
    SOURCE_OUTAGE,
)

_FIELD_SEPARATOR = "\x1f"
# Bits of the digest kept for the mantissa. With 52 bits, (n + 0.5) / 2**52 is exact in a
# double for every n, so the result lies in [2**-53, 1 - 2**-53]: strictly inside (0, 1).
# With 53 or 64 bits the top values round up to exactly 1.0 (D-095 deviation).
_UNIFORM_BITS = 52
_UNIFORM_SCALE = float(1 << _UNIFORM_BITS)


def keyed_uniform(base_seed: int, replication_index: int, source: str, key: str) -> float:
    """Return a uniform draw strictly inside (0, 1) for one keyed entity (COMP-130, D-095).

    The value is a pure function of its four inputs: blake2b (8-byte digest) over
    `seed \\x1f rep \\x1f source \\x1f key` in UTF-8, top 52 bits, then
    `(n + 0.5) / 2**52`. The unit separator stops field collisions such as (1, 23)
    against (12, 3). `source` must be one of `KEYED_SOURCES`; anything else raises
    `ValueError` (sources are code, keys are entity ids).
    """
    if source not in KEYED_SOURCES:
        raise ValueError(f"unknown keyed RNG source: {source!r}")
    canonical = _FIELD_SEPARATOR.join((str(base_seed), str(replication_index), source, key))
    digest = hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).digest()
    top_bits = int.from_bytes(digest, "big") >> (64 - _UNIFORM_BITS)
    return (top_bits + 0.5) / _UNIFORM_SCALE


class RngRegistry:
    """Hands out one numpy Generator per declared source for a (base_seed, rep).

    Each source's stream is derived from SeedSequence entropy keyed on
    (base_seed, replication_index, source_index), so a source's sequence
    depends only on its own draws — never on how many draws another source
    made first (CRN survival, D-033).
    """

    def __init__(self, base_seed: int, replication_index: int) -> None:
        self._base_seed = base_seed
        self._replication_index = replication_index
        self._generators: dict[str, np.random.Generator] = {}

    def generator(self, source: str) -> np.random.Generator:
        """Return the (cached) Generator for `source`, creating it on first use."""
        if source not in _SOURCE_INDEX:
            raise ValueError(f"unknown RNG source key: {source!r}")

        cached = self._generators.get(source)
        if cached is not None:
            return cached

        seed_sequence = np.random.SeedSequence(
            [self._base_seed, self._replication_index, _SOURCE_INDEX[source]]
        )
        generator = np.random.default_rng(seed_sequence)
        self._generators[source] = generator
        return generator

    def discard(self, source: str, n: int = 1) -> None:
        """Advance `source`'s stream by n draws without exposing the values."""
        generator = self.generator(source)
        generator.random(n)
