"""TF-004 T-169: keyed counter-based draws in `twinflow.engine.rng` (COMP-130, D-095).

API fixed by these tests (implementer conforms):

    from twinflow.engine.rng import (
        KEYED_SOURCES,
        SOURCE_RUN_FACTOR,      # == "run_factor"
        SOURCE_SETUP_FACTOR,    # == "setup_factor"
        SOURCE_FAILURE,         # == "failure"
        SOURCE_REPAIR,          # == "repair"
        SOURCE_OUTAGE,          # == "outage"
        keyed_uniform,
    )

    KEYED_SOURCES == (SOURCE_RUN_FACTOR, SOURCE_SETUP_FACTOR, SOURCE_FAILURE,
                      SOURCE_REPAIR, SOURCE_OUTAGE)
        A tuple separate from `SOURCE_KEYS`; the two sets are disjoint. `RngRegistry` and
        `SOURCE_KEYS` are unchanged, so `RngRegistry` rejects a keyed source and
        `keyed_uniform` rejects a registry source (D-033 discipline: sources are code).

    keyed_uniform(base_seed: int, replication_index: int, source: str, key: str) -> float
        digest = hashlib.blake2b(
            f"{base_seed}\\x1f{replication_index}\\x1f{source}\\x1f{key}".encode("utf-8"),
            digest_size=8,
        ).digest()
        return ((int.from_bytes(digest, "big") >> 12) + 0.5) / 2**52
        (top 52 bits, so every value is exact and strictly inside (0, 1); D-095 deviation)
        Raises ValueError for a source not in KEYED_SOURCES. The source string values above
        are part of the contract: they feed the hash, so renaming one changes every stored
        replication.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import subprocess
import sys

import numpy as np
import pytest
from scipy import stats

from twinflow.engine.rng import (
    KEYED_SOURCES,
    SOURCE_FAILURE,
    SOURCE_KEYS,
    SOURCE_OUTAGE,
    SOURCE_REPAIR,
    SOURCE_RUN_FACTOR,
    SOURCE_SETUP_FACTOR,
    RngRegistry,
    keyed_uniform,
)

N_KEYS = 100_000
SEED = 42


def _reference(base_seed: int, rep: int, source: str, key: str) -> float:
    """The COMP-130 formula, written out independently of the implementation."""
    canonical = f"{base_seed}\x1f{rep}\x1f{source}\x1f{key}".encode()
    digest = hashlib.blake2b(canonical, digest_size=8).digest()
    return ((int.from_bytes(digest, "big") >> 12) + 0.5) / 2**52


def _draws(source: str) -> np.ndarray:
    return np.array([keyed_uniform(SEED, 0, source, f"op-{i}") for i in range(N_KEYS)])


def test_keyed_sources_are_the_five_code_declared_constants_in_order() -> None:
    assert KEYED_SOURCES == (
        SOURCE_RUN_FACTOR,
        SOURCE_SETUP_FACTOR,
        SOURCE_FAILURE,
        SOURCE_REPAIR,
        SOURCE_OUTAGE,
    )
    assert KEYED_SOURCES == ("run_factor", "setup_factor", "failure", "repair", "outage")


def test_keyed_sources_are_disjoint_from_registry_sources() -> None:
    assert set(KEYED_SOURCES).isdisjoint(SOURCE_KEYS)
    assert SOURCE_KEYS == ("cycle_time", "scrap", "routing", "breakdown", "absence")


def test_rng_registry_still_rejects_keyed_sources() -> None:
    registry = RngRegistry(SEED, 0)
    with pytest.raises(ValueError):
        registry.generator(SOURCE_RUN_FACTOR)


@pytest.mark.parametrize(
    ("seed", "rep", "source", "key"),
    [
        (42, 0, SOURCE_RUN_FACTOR, "op-1"),
        (0, 0, SOURCE_SETUP_FACTOR, "J1/10"),
        (7, 19, SOURCE_FAILURE, "M-3:0"),
        (2**63, 999, SOURCE_REPAIR, "M-3:12"),
        (-5, 3, SOURCE_OUTAGE, "0"),
        (42, 0, SOURCE_RUN_FACTOR, "werkstück-ä"),
    ],
)
def test_keyed_uniform_matches_the_pinned_blake2b_formula(
    seed: int, rep: int, source: str, key: str
) -> None:
    assert keyed_uniform(seed, rep, source, key) == _reference(seed, rep, source, key)


def test_keyed_uniform_pinned_value() -> None:
    assert keyed_uniform(42, 0, "run_factor", "op-1") == pytest.approx(
        0.7263663953266354, abs=1e-15
    )


def test_keyed_uniform_is_in_the_open_unit_interval() -> None:
    values = [
        keyed_uniform(seed, rep, source, f"k{i}")
        for seed in (0, 1, 2**63)
        for rep in (0, 7)
        for source in KEYED_SOURCES
        for i in range(300)
    ]
    assert all(isinstance(value, float) for value in values)
    assert min(values) > 0.0
    assert max(values) < 1.0


def test_keyed_uniform_equal_inputs_give_equal_values() -> None:
    first = [keyed_uniform(SEED, 3, source, "op-9") for source in KEYED_SOURCES]
    second = [keyed_uniform(SEED, 3, source, "op-9") for source in KEYED_SOURCES]
    assert first == second


@pytest.mark.parametrize("hash_seed", ["1", "987654"])
def test_keyed_uniform_equal_across_a_fresh_interpreter(hash_seed: str) -> None:
    """A spawned interpreter with a different PYTHONHASHSEED gets the same draws."""
    inputs = [(SEED, rep, source, f"op-{rep}") for rep in range(3) for source in KEYED_SOURCES]
    script = (
        "import sys\n"
        "from twinflow.engine.rng import keyed_uniform\n"
        f"for args in {inputs!r}:\n"
        "    sys.stdout.write(repr(keyed_uniform(*args)) + '\\n')\n"
    )
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and script, no shell
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    child = [float(line) for line in completed.stdout.split()]
    assert child == [keyed_uniform(*args) for args in inputs]


@pytest.mark.parametrize(
    "changed",
    [
        (43, 0, SOURCE_RUN_FACTOR, "op-1"),
        (42, 1, SOURCE_RUN_FACTOR, "op-1"),
        (42, 0, SOURCE_SETUP_FACTOR, "op-1"),
        (42, 0, SOURCE_RUN_FACTOR, "op-2"),
        (42, 0, SOURCE_RUN_FACTOR, "op-1 "),
    ],
)
def test_keyed_uniform_differs_when_any_input_differs(changed: tuple[int, int, str, str]) -> None:
    assert keyed_uniform(*changed) != keyed_uniform(42, 0, SOURCE_RUN_FACTOR, "op-1")


def test_keyed_uniform_field_separator_prevents_seed_rep_collisions() -> None:
    assert keyed_uniform(1, 23, SOURCE_FAILURE, "M1:0") != keyed_uniform(
        12, 3, SOURCE_FAILURE, "M1:0"
    )


@pytest.mark.parametrize("source", ["", "cycle_time", "breakdown", "RUN_FACTOR", "run_factor "])
def test_keyed_uniform_rejects_an_unknown_source(source: str) -> None:
    with pytest.raises(ValueError):
        keyed_uniform(SEED, 0, source, "op-1")


@pytest.mark.parametrize("source", KEYED_SOURCES)
def test_keyed_uniform_passes_ks_uniformity_on_100k_keys(source: str) -> None:
    result = stats.kstest(_draws(source), "uniform")
    assert result.pvalue > 0.01


@pytest.fixture(scope="module")
def draws_by_source() -> dict[str, np.ndarray]:
    return {source: _draws(source) for source in KEYED_SOURCES}


@pytest.mark.parametrize(("a", "b"), list(itertools.combinations(KEYED_SOURCES, 2)))
def test_two_sources_on_one_key_are_uncorrelated(
    draws_by_source: dict[str, np.ndarray], a: str, b: str
) -> None:
    r = float(np.corrcoef(draws_by_source[a], draws_by_source[b])[0, 1])
    assert abs(r) < 0.01
