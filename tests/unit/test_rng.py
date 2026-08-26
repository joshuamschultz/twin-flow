"""Unit tests for COMP-003 RngRegistry (engine/rng.py, Layer 0).

Public API under test (design committed by these tests; implementer conforms):

    reg = RngRegistry(base_seed: int, replication_index: int)
    reg.generator(source: str) -> numpy.random.Generator
        Returns the SAME Generator object on every call for a given source,
        so successive calls continue drawing from that source's stream.
    reg.discard(source: str, n: int = 1) -> None
        Advances the named source's stream by n draws without exposing the
        values. Must NOT touch any other source's stream. This is the
        "always draw, sometimes discard" affordance: a branch that decides
        not to use a value still calls discard() so the stream position
        never depends on which branch executed.

Source keys are the code-declared module constants SOURCE_CYCLE_TIME,
SOURCE_SCRAP, SOURCE_ROUTING, SOURCE_BREAKDOWN (never derived from config,
per D-033).
"""

from __future__ import annotations

import numpy as np
import pytest

from twinflow.engine.rng import (
    SOURCE_BREAKDOWN,
    SOURCE_CYCLE_TIME,
    SOURCE_KEYS,
    SOURCE_ROUTING,
    SOURCE_SCRAP,
    RngRegistry,
)

BASE_SEED = 42
REP_INDEX = 0


def draw_n(registry: RngRegistry, source: str, n: int) -> list[float]:
    """Draw n floats from a source's generator, in order."""
    gen = registry.generator(source)
    return [float(gen.random()) for _ in range(n)]


# ---------------------------------------------------------------------------
# Import sanity — confirms the module itself loads cleanly (no ImportError /
# SyntaxError). The failures below must come from NotImplementedError inside
# __init__, not a broken import line.
# ---------------------------------------------------------------------------


def test_module_imports_and_declares_all_source_keys():
    assert SOURCE_KEYS == (
        SOURCE_CYCLE_TIME,
        SOURCE_SCRAP,
        SOURCE_ROUTING,
        SOURCE_BREAKDOWN,
    )


# ---------------------------------------------------------------------------
# Criterion 1 — CRN survival: unrelated extra draws on one source must not
# perturb another source's sequence at the same (base_seed, replication_index).
# ---------------------------------------------------------------------------


def test_unrelated_source_draws_do_not_perturb_other_source_sequence():
    """Two 'configs' with different draw counts on SOURCE_SCRAP must still
    produce an identical SOURCE_CYCLE_TIME sequence at the same seed+rep."""
    reg_light = RngRegistry(BASE_SEED, REP_INDEX)
    reg_heavy = RngRegistry(BASE_SEED, REP_INDEX)

    # reg_heavy simulates a "config" that burns many more unrelated draws
    # from SOURCE_SCRAP before anyone touches SOURCE_CYCLE_TIME.
    draw_n(reg_heavy, SOURCE_SCRAP, 500)

    cycle_light = draw_n(reg_light, SOURCE_CYCLE_TIME, 20)
    cycle_heavy = draw_n(reg_heavy, SOURCE_CYCLE_TIME, 20)

    assert cycle_light == cycle_heavy


def test_unrelated_source_discards_do_not_perturb_other_source_sequence():
    """Same CRN-survival property, but via discard() instead of a real draw
    on the unrelated source."""
    reg_light = RngRegistry(BASE_SEED, REP_INDEX)
    reg_heavy = RngRegistry(BASE_SEED, REP_INDEX)

    reg_heavy.discard(SOURCE_BREAKDOWN, n=1000)

    routing_light = draw_n(reg_light, SOURCE_ROUTING, 20)
    routing_heavy = draw_n(reg_heavy, SOURCE_ROUTING, 20)

    assert routing_light == routing_heavy


# ---------------------------------------------------------------------------
# Criterion 2 — a discarded draw still advances its OWN stream, and no other.
# ---------------------------------------------------------------------------


def test_discard_advances_its_own_source_stream():
    """After discarding one value from SOURCE_ROUTING, the next SOURCE_ROUTING
    draw must differ from a fresh registry's first SOURCE_ROUTING draw."""
    reg_after_discard = RngRegistry(BASE_SEED, REP_INDEX)
    reg_fresh = RngRegistry(BASE_SEED, REP_INDEX)

    reg_after_discard.discard(SOURCE_ROUTING, n=1)

    next_routing_value = draw_n(reg_after_discard, SOURCE_ROUTING, 1)[0]
    fresh_first_routing_value = draw_n(reg_fresh, SOURCE_ROUTING, 1)[0]

    assert next_routing_value != fresh_first_routing_value


def test_discard_on_one_source_leaves_every_other_source_unchanged():
    """Discarding from SOURCE_ROUTING must not move SOURCE_CYCLE_TIME,
    SOURCE_SCRAP, or SOURCE_BREAKDOWN by even one draw."""
    reg_after_discard = RngRegistry(BASE_SEED, REP_INDEX)
    reg_fresh = RngRegistry(BASE_SEED, REP_INDEX)

    reg_after_discard.discard(SOURCE_ROUTING, n=1)

    for source in (SOURCE_CYCLE_TIME, SOURCE_SCRAP, SOURCE_BREAKDOWN):
        untouched = draw_n(reg_after_discard, source, 5)
        fresh = draw_n(reg_fresh, source, 5)
        assert untouched == fresh, f"{source} stream moved after an unrelated discard"


def test_discard_default_count_advances_exactly_one_draw():
    """discard(source) with no n argument advances the stream by exactly one
    draw — verified by comparing against an explicit single generator draw
    consumed on a twin registry."""
    reg_discard = RngRegistry(BASE_SEED, REP_INDEX)
    reg_manual = RngRegistry(BASE_SEED, REP_INDEX)

    reg_discard.discard(SOURCE_SCRAP)
    reg_manual.generator(SOURCE_SCRAP).random()  # consume exactly one value manually

    after_discard = draw_n(reg_discard, SOURCE_SCRAP, 3)
    after_manual = draw_n(reg_manual, SOURCE_SCRAP, 3)

    assert after_discard == after_manual


# ---------------------------------------------------------------------------
# Criterion 3 — independence across sources.
# ---------------------------------------------------------------------------


def test_different_sources_are_independent_streams():
    """Two different sources drawn the same number of times at the same
    seed+rep must not produce the same sequence (they are independent
    child streams, not the same stream re-used)."""
    reg = RngRegistry(BASE_SEED, REP_INDEX)

    cycle_time_values = draw_n(reg, SOURCE_CYCLE_TIME, 10)
    scrap_values = draw_n(reg, SOURCE_SCRAP, 10)

    assert cycle_time_values != scrap_values


def test_generator_returns_a_numpy_generator_instance():
    reg = RngRegistry(BASE_SEED, REP_INDEX)
    gen = reg.generator(SOURCE_CYCLE_TIME)
    assert isinstance(gen, np.random.Generator)


def test_generator_returns_same_object_across_repeated_calls():
    """generator() must hand back the SAME Generator each call for a given
    source, so the stream continues rather than resetting."""
    reg = RngRegistry(BASE_SEED, REP_INDEX)

    first_call = reg.generator(SOURCE_CYCLE_TIME)
    second_call = reg.generator(SOURCE_CYCLE_TIME)

    assert first_call is second_call


def test_all_declared_sources_produce_pairwise_distinct_first_draws():
    """Sanity sweep: the first draw from every declared source key must be
    mutually distinct at a single (seed, rep) — collapsing any pair back to
    the same stream would silently corrupt CRN pairing for that source."""
    reg = RngRegistry(BASE_SEED, REP_INDEX)

    first_draws = {source: draw_n(reg, source, 1)[0] for source in SOURCE_KEYS}

    assert len(set(first_draws.values())) == len(SOURCE_KEYS)


# ---------------------------------------------------------------------------
# Reproducibility and seed/rep sensitivity (adversarial additions beyond the
# three listed criteria).
# ---------------------------------------------------------------------------


def test_same_seed_and_replication_reproduce_identical_full_sequences():
    """The whole point of a pinned seed: two independently constructed
    registries at the same (base_seed, replication_index) must be
    byte-identical across every source, drawn in any order."""
    reg_a = RngRegistry(BASE_SEED, REP_INDEX)
    reg_b = RngRegistry(BASE_SEED, REP_INDEX)

    for source in SOURCE_KEYS:
        assert draw_n(reg_a, source, 15) == draw_n(reg_b, source, 15)


def test_different_replication_index_changes_the_sequence():
    reg_rep0 = RngRegistry(BASE_SEED, 0)
    reg_rep1 = RngRegistry(BASE_SEED, 1)

    assert draw_n(reg_rep0, SOURCE_CYCLE_TIME, 10) != draw_n(reg_rep1, SOURCE_CYCLE_TIME, 10)


def test_different_base_seed_changes_the_sequence():
    reg_seed_a = RngRegistry(1, REP_INDEX)
    reg_seed_b = RngRegistry(2, REP_INDEX)

    assert draw_n(reg_seed_a, SOURCE_CYCLE_TIME, 10) != draw_n(reg_seed_b, SOURCE_CYCLE_TIME, 10)


def test_unknown_source_key_raises():
    """A source key that isn't one of the code-declared constants must be
    rejected rather than silently creating a new stream — keys are never
    derived from config (D-033)."""
    reg = RngRegistry(BASE_SEED, REP_INDEX)

    with pytest.raises((KeyError, ValueError)):
        reg.generator("not_a_real_source")
