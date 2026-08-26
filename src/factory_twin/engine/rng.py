"""COMP-003 RngRegistry — one dedicated numpy stream per stochastic source.

Source keys are code-declared module constants, never derived from config (D-033).
Enforces always-draw-sometimes-discard so a stream position never depends on which
branch executed, which is what keeps common random numbers paired across sweep points.
"""

from __future__ import annotations

import numpy as np

# Code-declared source keys. Adding a stochastic source means adding a constant here,
# never computing a key from the model's resource count.
SOURCE_CYCLE_TIME = "cycle_time"
SOURCE_SCRAP = "scrap"
SOURCE_ROUTING = "routing"
SOURCE_BREAKDOWN = "breakdown"

SOURCE_KEYS: tuple[str, ...] = (
    SOURCE_CYCLE_TIME,
    SOURCE_SCRAP,
    SOURCE_ROUTING,
    SOURCE_BREAKDOWN,
)

# Fixed per-source index used to key each source's independent SeedSequence entropy.
# Never derived from a model/config count — the position in SOURCE_KEYS is stable
# code, not data (D-033).
_SOURCE_INDEX: dict[str, int] = {source: index for index, source in enumerate(SOURCE_KEYS)}


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
