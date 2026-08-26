"""Unit tests for COMP-006 Stock (T-010, red).

Stock (primitives/stock.py) is a material level pulled from BY NAME — not a step
in a route (structure.md, "Two primitives, not seven archetypes", D-043). It is
Layer 1: pure, no YAML, no file I/O, over already-parsed values only.

Contract under test:

  * `pull(thing, qty, uom)` against an EMPTY/insufficient stock BLOCKS (SimPy
    yield) rather than letting the level go negative.
  * `material_ready_time` is stamped with the sim time at which the stock was
    replenished (`put`), so a waiting puller can later record when material
    became ready (used for D-034 wait classification).
  * A pull in the WRONG uom, or for the WRONG thing, RAISES — never silently
    returns mismatched material.

Public API committed here (implementer conforms):

    stock = Stock(thing: str, uom: str, env: simpy.Environment, initial_qty: float = 0.0)
        stock.thing               -> str, stored verbatim
        stock.uom                 -> str, stored verbatim
        stock.level               -> float, current qty; never negative
        stock.material_ready_time -> float | None, sim time of the most recent put()

    # A SimPy generator, mirrors ResourceAcquirer.acquire (engine/acquire.py):
    # drive it with `yield from`.
    bundle = yield from stock.pull(thing: str, qty: float, uom: str)
        - Raises ValueError if thing/uom do not match the stock's identity.
          The check must fire on the first drive of the generator, before any
          blocking wait is created (so a wrong-thing/uom pull never queues).
        - Otherwise blocks (does not return / advance the caller) until qty
          is available, then returns a Bundle(qty=qty, thing=thing, uom=uom).

    stock.put(bundle: Bundle) -> None
        - Raises ValueError if bundle.thing/bundle.uom mismatch the stock's
          identity.
        - Increases the level by bundle.qty and stamps
          stock.material_ready_time = env.now (every call, unconditionally —
          "the stock is replenished" is the put itself, not merely the puts
          that happen to unblock someone).
"""

from __future__ import annotations

import pytest

from twinflow.engine.clock import RunContext
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.stock import Stock

THING = "widget"
UOM = "piece"


def _make_context() -> RunContext:
    return RunContext(run_id="test-run", seed=1, horizon=1_000.0)


# ---------------------------------------------------------------------------
# A pull against an empty/insufficient stock blocks, never goes negative
# ---------------------------------------------------------------------------


def test_pull_against_empty_stock_blocks_until_a_put_arrives() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=0.0)

    results: list[Bundle] = []

    def puller():
        bundle = yield from stock.pull(THING, 5.0, UOM)
        results.append(bundle)

    ctx.env.process(puller())

    # Advance the clock with no put ever arriving: the pull must NOT complete.
    ctx.env.run(until=50.0)

    assert results == []
    assert stock.level == 0.0


def test_pull_completes_once_a_matching_put_satisfies_it() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=0.0)

    results: list[Bundle] = []

    def puller():
        bundle = yield from stock.pull(THING, 5.0, UOM)
        results.append(bundle)

    def replenisher():
        yield ctx.env.timeout(10.0)
        stock.put(Bundle(qty=5.0, thing=THING, uom=UOM))

    ctx.env.process(puller())
    ctx.env.process(replenisher())
    ctx.env.run(until=20.0)

    assert len(results) == 1
    assert results[0].qty == 5.0
    assert results[0].thing == THING
    assert results[0].uom == UOM


def test_level_never_goes_negative_when_a_partial_put_arrives() -> None:
    """A pull for 10 against an empty stock is fed a put of only 3. The level
    must sit at 3 (not go negative, not silently complete the pull) until a
    second put brings the total to 10.
    """
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=0.0)

    results: list[Bundle] = []
    levels_after_first_put: list[float] = []

    def puller():
        bundle = yield from stock.pull(THING, 10.0, UOM)
        results.append(bundle)

    def replenisher():
        yield ctx.env.timeout(5.0)
        stock.put(Bundle(qty=3.0, thing=THING, uom=UOM))
        levels_after_first_put.append(stock.level)
        yield ctx.env.timeout(5.0)
        stock.put(Bundle(qty=7.0, thing=THING, uom=UOM))

    ctx.env.process(puller())
    ctx.env.process(replenisher())
    ctx.env.run(until=15.0)

    # Never negative at any observed point, and the partial put alone must
    # not have unblocked the 10-unit pull.
    assert levels_after_first_put == [3.0]
    assert results[0].qty == 10.0
    assert stock.level == 0.0


def test_two_waiting_pulls_only_the_satisfiable_one_completes_and_level_stays_nonnegative() -> None:
    """Two processes each pull 5 against an empty stock. A put of 5 arrives:
    exactly one puller may complete; the level must never dip below zero, and
    the second puller must remain blocked until further material arrives.
    """
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=0.0)

    completed: list[str] = []

    def puller(name: str):
        yield from stock.pull(THING, 5.0, UOM)
        completed.append(name)

    def replenisher():
        yield ctx.env.timeout(3.0)
        stock.put(Bundle(qty=5.0, thing=THING, uom=UOM))

    ctx.env.process(puller("a"))
    ctx.env.process(puller("b"))
    ctx.env.process(replenisher())
    ctx.env.run(until=10.0)

    assert completed == ["a"]
    assert stock.level == 0.0


# ---------------------------------------------------------------------------
# material_ready_time is stamped with the exact sim time of replenishment
# ---------------------------------------------------------------------------


def test_material_ready_time_stamped_at_exact_sim_time_of_the_satisfying_put() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=0.0)

    def puller():
        yield from stock.pull(THING, 5.0, UOM)

    def replenisher():
        yield ctx.env.timeout(17.5)
        stock.put(Bundle(qty=5.0, thing=THING, uom=UOM))

    assert stock.material_ready_time is None

    ctx.env.process(puller())
    ctx.env.process(replenisher())
    ctx.env.run(until=30.0)

    assert stock.material_ready_time == 17.5


def test_material_ready_time_reflects_the_most_recent_put_across_several() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=0.0)

    def replenisher():
        yield ctx.env.timeout(2.0)
        stock.put(Bundle(qty=1.0, thing=THING, uom=UOM))
        yield ctx.env.timeout(8.0)
        stock.put(Bundle(qty=1.0, thing=THING, uom=UOM))

    ctx.env.process(replenisher())
    ctx.env.run(until=15.0)

    assert stock.material_ready_time == 10.0


# ---------------------------------------------------------------------------
# Wrong uom / wrong thing raises
# ---------------------------------------------------------------------------


def test_pull_with_wrong_uom_raises() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=100.0)

    caught: list[BaseException] = []

    def puller():
        try:
            yield from stock.pull(THING, 5.0, "gallon")
        except ValueError as exc:
            caught.append(exc)

    ctx.env.process(puller())
    ctx.env.run(until=1.0)

    assert len(caught) == 1
    assert isinstance(caught[0], ValueError)


def test_pull_with_wrong_thing_raises() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=100.0)

    caught: list[BaseException] = []

    def puller():
        try:
            yield from stock.pull("gadget", 5.0, UOM)
        except ValueError as exc:
            caught.append(exc)

    ctx.env.process(puller())
    ctx.env.run(until=1.0)

    assert len(caught) == 1
    assert isinstance(caught[0], ValueError)


def test_pull_with_wrong_uom_raises_before_blocking_even_when_stock_is_empty() -> None:
    """A mismatched pull against an EMPTY stock must still raise immediately —
    it must never queue behind the empty level waiting for a put that could
    never satisfy it anyway (wrong identity, not merely insufficient qty).
    """
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=0.0)

    caught: list[BaseException] = []

    def puller():
        try:
            yield from stock.pull(THING, 5.0, "gallon")
        except ValueError as exc:
            caught.append(exc)

    ctx.env.process(puller())
    # A vanishingly small horizon: the process gets exactly one chance to
    # start. With a correctly-implemented Stock the ValueError fires on the
    # very first drive of the generator, before any Container.get() event
    # (and therefore before any real blocking wait) is ever created.
    ctx.env.run(until=0.001)

    assert len(caught) == 1


def test_pull_wrong_uom_raises_via_direct_generator_drive_without_env_run() -> None:
    """Belt-and-suspenders on the same guarantee as above, driving the
    generator directly with `next()` rather than through env.run — proves
    the raise is not merely an artifact of how the SimPy scheduler happens
    to drain processes.
    """
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=100.0)

    generator = stock.pull(THING, 5.0, "gallon")
    with pytest.raises(ValueError):
        next(generator)


def test_pull_wrong_thing_raises_via_direct_generator_drive_without_env_run() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=100.0)

    generator = stock.pull("gadget", 5.0, UOM)
    with pytest.raises(ValueError):
        next(generator)


def test_put_with_wrong_thing_raises() -> None:
    """The symmetric guard on put(): a bundle whose identity does not match
    the stock's must not silently corrupt the level.
    """
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=0.0)

    with pytest.raises(ValueError):
        stock.put(Bundle(qty=5.0, thing="gadget", uom=UOM))

    assert stock.level == 0.0


def test_put_with_wrong_uom_raises() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=0.0)

    with pytest.raises(ValueError):
        stock.put(Bundle(qty=5.0, thing=THING, uom="gallon"))

    assert stock.level == 0.0


# ---------------------------------------------------------------------------
# Construction basics
# ---------------------------------------------------------------------------


def test_stock_initial_qty_sets_starting_level() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=42.0)

    assert stock.level == 42.0
    assert stock.thing == THING
    assert stock.uom == UOM
    assert stock.material_ready_time is None


def test_stock_defaults_to_empty_when_initial_qty_omitted() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env)

    assert stock.level == 0.0


def test_pull_from_sufficient_stock_does_not_block() -> None:
    ctx = _make_context()
    stock = Stock(thing=THING, uom=UOM, env=ctx.env, initial_qty=20.0)

    results: list[Bundle] = []

    def puller():
        bundle = yield from stock.pull(THING, 5.0, UOM)
        results.append(bundle)

    ctx.env.process(puller())
    # Vanishingly small horizon: a satisfiable pull must complete without
    # needing any real simulated time to pass.
    ctx.env.run(until=0.001)

    assert len(results) == 1
    assert results[0].qty == 5.0
    assert stock.level == 15.0
