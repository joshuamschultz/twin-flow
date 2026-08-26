"""Unit tests for COMP-011 TimeModel (time_model.py) and COMP-012 LaborPool
(labor.py) — T-014, red phase.

Both are Layer 1 primitives (`primitives/`): pure, no YAML, no file I/O. Any
"compiled expression" (e.g. an attribute-scaling factor) is modeled here as a
plain Python callable, since the config/model-layer sandbox (simpleeval) does
not exist yet.

--------------------------------------------------------------------------
COMP-011 TimeModel — public API committed by these tests (implementer
conforms):

    tm = TimeModel(kind: str, params: dict, load: float = 0.0, unload: float = 0.0)
    phases = tm.sample(bundle: Bundle, generator: numpy.random.Generator) -> PhaseTimes

`PhaseTimes` is a value object exposing float attributes `.load`, `.run`,
`.unload` (seconds). A TimeModel built with no `load`/`unload` collapses to
`run` only (`load == 0.0`, `unload == 0.0`).

Three `kind` values (local constants below mirror the values the
implementer is expected to declare in `time_model.py` itself):

    KIND_DISTRIBUTION     params={"draw": Callable[[np.random.Generator], float]}
                           run = params["draw"](generator) — the draw MUST come
                           from the generator handed in, which the caller sources
                           from RngRegistry.generator(SOURCE_CYCLE_TIME) (COMP-003).
                           Never numpy's global RNG.

    KIND_RATE_BASED        params={"rate": float}
                            run = bundle.qty / params["rate"]

    KIND_ATTRIBUTE_SCALED  params={"base": float, "scale": Callable[[Bundle], float]}
                            run = params["base"] * params["scale"](bundle)
                            (the callable stands in for a compiled expression —
                            D-005/D-049 — that the model-layer sandbox will later
                            produce; primitives never import simpleeval)

--------------------------------------------------------------------------
COMP-012 LaborPool — public API committed by these tests (implementer
conforms):

    pool = LaborPool(
        name: str,
        headcount: int,
        skills: frozenset[str],
        shift_calendar: <ShiftCalendar-like>,
        env: simpy.Environment,
    )
    pool.available -> int   # operators free right now (0 if off-shift or fully held)

    # A SimPy generator function — drive with `yield from` (matches the
    # ResourceAcquirer.acquire() convention in engine/acquire.py). Blocks
    # until the shift calendar says on-shift AND a slot is free, then
    # returns an opaque request handle.
    handle = yield from pool.request(skill: str, priority: int)
    pool.release(handle) -> None

Priority convention (matches engine/acquire.py, simpy.PriorityResource: a
SMALLER int is served first):

    PRIORITY_UNLOAD = 0    # unload-phase re-request — served first
    PRIORITY_LOAD = 10     # fresh load-phase request — served after

`shift_calendar` is a small protocol LaborPool is expected to consult, not a
concrete class primitives owns:

    shift_calendar.is_on_shift(t: float) -> bool
    shift_calendar.next_shift_start(t: float) -> float
        # only ever consulted when is_on_shift(t) is False; returns the
        # absolute sim time (>= t) the next on-shift window begins.

Two fakes below (`AlwaysOnShiftCalendar`, `WindowShiftCalendar`) implement
that protocol for these tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest
import simpy

from twinflow.engine.rng import SOURCE_CYCLE_TIME, RngRegistry
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.labor import LaborPool
from twinflow.primitives.time_model import TimeModel

BASE_SEED = 123
REP_INDEX = 0

KIND_DISTRIBUTION = "distribution"
KIND_RATE_BASED = "rate_based"
KIND_ATTRIBUTE_SCALED = "attribute_scaled"

PRIORITY_UNLOAD = 0
PRIORITY_LOAD = 10


# ---------------------------------------------------------------------------
# Shift calendar test doubles (protocol consumed by LaborPool; see module
# docstring). Not owned by primitives — these are plain test fakes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlwaysOnShiftCalendar:
    """On-shift for the entire horizon — used when shift gating is not under test."""

    def is_on_shift(self, t: float) -> bool:
        return True

    def next_shift_start(self, t: float) -> float:  # pragma: no cover - never off-shift
        return t


@dataclass(frozen=True)
class WindowShiftCalendar:
    """On-shift only inside the given [start, end) windows."""

    windows: tuple[tuple[float, float], ...]

    def is_on_shift(self, t: float) -> bool:
        return any(start <= t < end for start, end in self.windows)

    def next_shift_start(self, t: float) -> float:
        upcoming = [start for start, _end in self.windows if start >= t]
        if upcoming:
            return min(upcoming)
        raise AssertionError("no future shift window — test calendar exhausted")


# ---------------------------------------------------------------------------
# Import sanity — confirms both modules load cleanly (no ImportError /
# SyntaxError). Failures below must come from NotImplementedError / a
# constructor signature mismatch, not a broken import line.
# ---------------------------------------------------------------------------


def test_time_model_and_labor_pool_modules_import_cleanly():
    assert isinstance(TimeModel, type)
    assert isinstance(LaborPool, type)


# ---------------------------------------------------------------------------
# COMP-011 TimeModel — Criterion 1: each kind, pinned-seed exact value.
# ---------------------------------------------------------------------------


def test_distribution_time_model_matches_pinned_seed_draw():
    """The DISTRIBUTION kind's run value must equal exactly what the
    cycle_time generator yields at the pinned (seed, rep) — computed
    independently here the same way, per REQ: every draw via RngRegistry."""

    def draw_fn(generator: np.random.Generator) -> float:
        return float(generator.normal(loc=20.0, scale=5.0))

    expected_registry = RngRegistry(BASE_SEED, REP_INDEX)
    expected_value = draw_fn(expected_registry.generator(SOURCE_CYCLE_TIME))

    actual_registry = RngRegistry(BASE_SEED, REP_INDEX)
    bundle = Bundle(qty=1.0, thing="widget", uom="piece")
    tm = TimeModel(kind=KIND_DISTRIBUTION, params={"draw": draw_fn})

    phases = tm.sample(bundle, actual_registry.generator(SOURCE_CYCLE_TIME))

    assert phases.run == expected_value
    assert phases.load == 0.0
    assert phases.unload == 0.0


def test_distribution_time_model_never_draws_from_numpy_global_rng(monkeypatch):
    """Every draw MUST come from the generator handed to sample(), never
    numpy's global RNG convenience API. Poison the global API; sample()
    must still succeed using only the passed-in generator."""

    def poison(*_args, **_kwargs):
        raise AssertionError("global numpy RNG was used instead of the RngRegistry stream")

    monkeypatch.setattr(np.random, "normal", poison)
    monkeypatch.setattr(np.random, "random", poison)

    registry = RngRegistry(BASE_SEED, REP_INDEX)
    bundle = Bundle(qty=1.0, thing="widget", uom="piece")

    def draw_fn(generator: np.random.Generator) -> float:
        return float(generator.normal(loc=20.0, scale=5.0))

    tm = TimeModel(kind=KIND_DISTRIBUTION, params={"draw": draw_fn})

    phases = tm.sample(bundle, registry.generator(SOURCE_CYCLE_TIME))

    assert isinstance(phases.run, float)


def test_rate_based_time_model_is_qty_over_rate_exact():
    bundle = Bundle(qty=120.0, thing="wire", uom="ft")
    tm = TimeModel(kind=KIND_RATE_BASED, params={"rate": 6.0})
    generator = RngRegistry(BASE_SEED, REP_INDEX).generator(SOURCE_CYCLE_TIME)

    phases = tm.sample(bundle, generator)

    assert phases.run == 120.0 / 6.0
    assert phases.load == 0.0
    assert phases.unload == 0.0


def test_rate_based_time_model_scales_with_bundle_qty():
    tm = TimeModel(kind=KIND_RATE_BASED, params={"rate": 4.0})
    generator = RngRegistry(BASE_SEED, REP_INDEX).generator(SOURCE_CYCLE_TIME)

    small = tm.sample(Bundle(qty=8.0, thing="wire", uom="ft"), generator)
    large = tm.sample(Bundle(qty=40.0, thing="wire", uom="ft"), generator)

    assert small.run == 2.0
    assert large.run == 10.0


def test_attribute_scaled_time_model_is_base_times_attribute_exact():
    bundle = Bundle(qty=1.0, thing="casting", uom="piece", attrs={"length_ft": 8.0})
    tm = TimeModel(
        kind=KIND_ATTRIBUTE_SCALED,
        params={"base": 2.5, "scale": lambda b: b.attrs["length_ft"]},
    )
    generator = RngRegistry(BASE_SEED, REP_INDEX).generator(SOURCE_CYCLE_TIME)

    phases = tm.sample(bundle, generator)

    assert phases.run == 2.5 * 8.0
    assert phases.load == 0.0
    assert phases.unload == 0.0


def test_attribute_scaled_time_model_reflects_a_different_bundle_attribute_value():
    tm = TimeModel(
        kind=KIND_ATTRIBUTE_SCALED,
        params={"base": 1.5, "scale": lambda b: b.attrs["weight_lb"]},
    )
    generator = RngRegistry(BASE_SEED, REP_INDEX).generator(SOURCE_CYCLE_TIME)

    phases = tm.sample(
        Bundle(qty=1.0, thing="casting", uom="piece", attrs={"weight_lb": 12.0}), generator
    )

    assert phases.run == 18.0


# ---------------------------------------------------------------------------
# COMP-011 TimeModel — load/run/unload split; single value collapses to run.
# ---------------------------------------------------------------------------


def test_time_model_without_split_collapses_to_run_only():
    tm = TimeModel(kind=KIND_RATE_BASED, params={"rate": 2.0})
    generator = RngRegistry(BASE_SEED, REP_INDEX).generator(SOURCE_CYCLE_TIME)

    phases = tm.sample(Bundle(qty=10.0, thing="wire", uom="ft"), generator)

    assert phases.load == 0.0
    assert phases.run == 5.0
    assert phases.unload == 0.0


def test_time_model_with_load_and_unload_split_produces_all_three_phases():
    tm = TimeModel(kind=KIND_RATE_BASED, params={"rate": 2.0}, load=3.0, unload=1.5)
    generator = RngRegistry(BASE_SEED, REP_INDEX).generator(SOURCE_CYCLE_TIME)

    phases = tm.sample(Bundle(qty=10.0, thing="wire", uom="ft"), generator)

    assert phases.load == 3.0
    assert phases.run == 5.0
    assert phases.unload == 1.5


# ---------------------------------------------------------------------------
# COMP-012 LaborPool — Criterion 2: held for LOAD/UNLOAD, freed during RUN.
# ---------------------------------------------------------------------------


def test_operator_held_during_load_and_unload_but_free_during_run():
    """A job requests an operator for LOAD, releases it for RUN, then
    re-requests it for UNLOAD. `pool.available` must read 0 (held) during
    the load and unload windows and 1 (free) during the run window."""
    env = simpy.Environment()
    pool = LaborPool(
        name="assembly",
        headcount=1,
        skills=frozenset({"assembler"}),
        shift_calendar=AlwaysOnShiftCalendar(),
        env=env,
    )

    load_time, run_time, unload_time = 3.0, 10.0, 2.0
    observed: dict[str, int] = {}

    def job() -> object:
        handle = yield from pool.request("assembler", PRIORITY_LOAD)
        yield env.timeout(load_time)
        pool.release(handle)  # freed for RUN

        yield env.timeout(run_time)

        handle2 = yield from pool.request("assembler", PRIORITY_UNLOAD)
        yield env.timeout(unload_time)
        pool.release(handle2)

    def monitor() -> object:
        yield env.timeout(1.0)  # inside load window (0, 3)
        observed["during_load"] = pool.available

        yield env.timeout(load_time - 1.0 + 5.0)  # inside run window (3, 13)
        observed["during_run"] = pool.available

        yield env.timeout(run_time - 5.0 + 1.0)  # inside unload window (13, 15)
        observed["during_unload"] = pool.available

    env.process(job())
    env.process(monitor())
    env.run(until=load_time + run_time + unload_time + 1.0)

    assert observed["during_load"] == 0
    assert observed["during_run"] == 1
    assert observed["during_unload"] == 0


def test_labor_pool_available_starts_at_full_headcount():
    env = simpy.Environment()
    pool = LaborPool(
        name="welding",
        headcount=3,
        skills=frozenset({"welder"}),
        shift_calendar=AlwaysOnShiftCalendar(),
        env=env,
    )

    assert pool.available == 3


# ---------------------------------------------------------------------------
# COMP-012 LaborPool — priority: UNLOAD outranks a fresh LOAD request.
# ---------------------------------------------------------------------------


def test_unload_priority_request_is_served_before_a_queued_load_request():
    """One operator total. Operator A holds it. Both a fresh LOAD request
    and an UNLOAD re-request queue up while A holds the slot. When A
    releases, the UNLOAD request (higher priority / lower int) must be
    granted before the LOAD request, even though LOAD queued first."""
    env = simpy.Environment()
    pool = LaborPool(
        name="assembly",
        headcount=1,
        skills=frozenset({"assembler"}),
        shift_calendar=AlwaysOnShiftCalendar(),
        env=env,
    )
    grant_order: list[str] = []

    def holder() -> object:
        handle = yield from pool.request("assembler", PRIORITY_LOAD)
        yield env.timeout(5.0)
        pool.release(handle)

    def fresh_load_requester() -> object:
        yield env.timeout(1.0)  # queues after `holder` already holds the slot
        handle = yield from pool.request("assembler", PRIORITY_LOAD)
        grant_order.append("load")
        yield env.timeout(1.0)
        pool.release(handle)

    def unload_requester() -> object:
        yield env.timeout(2.0)  # queues after fresh_load_requester, still before release
        handle = yield from pool.request("assembler", PRIORITY_UNLOAD)
        grant_order.append("unload")
        yield env.timeout(1.0)
        pool.release(handle)

    env.process(holder())
    env.process(fresh_load_requester())
    env.process(unload_requester())
    env.run(until=20.0)

    assert grant_order == ["unload", "load"]


# ---------------------------------------------------------------------------
# COMP-012 LaborPool — Criterion 3: shift calendar gates availability.
# ---------------------------------------------------------------------------


def test_request_during_off_shift_window_blocks_until_shift_resumes():
    """A request made while off-shift must not be granted until the shift
    calendar's next on-shift window begins, even though a slot is
    physically free the whole time."""
    env = simpy.Environment()
    calendar = WindowShiftCalendar(windows=((100.0, 500.0),))
    pool = LaborPool(
        name="night_crew",
        headcount=1,
        skills=frozenset({"operator"}),
        shift_calendar=calendar,
        env=env,
    )
    granted_at: dict[str, float] = {}

    def requester() -> object:
        handle = yield from pool.request("operator", PRIORITY_LOAD)
        granted_at["time"] = env.now
        pool.release(handle)

    env.process(requester())  # requests at t=0, well before the 100.0 shift start
    env.run(until=150.0)

    assert granted_at["time"] >= 100.0


def test_request_during_active_shift_window_is_granted_immediately():
    env = simpy.Environment()
    calendar = WindowShiftCalendar(windows=((0.0, 500.0),))
    pool = LaborPool(
        name="day_crew",
        headcount=1,
        skills=frozenset({"operator"}),
        shift_calendar=calendar,
        env=env,
    )
    granted_at: dict[str, float] = {}

    def requester() -> object:
        handle = yield from pool.request("operator", PRIORITY_LOAD)
        granted_at["time"] = env.now
        pool.release(handle)

    env.process(requester())
    env.run(until=10.0)

    assert granted_at["time"] == 0.0


def test_labor_pool_available_is_zero_outside_the_shift_window_even_when_no_one_is_holding():
    """No operator is holding the resource, but off-shift still means zero
    available-to-work capacity — shift calendars gate availability
    directly, not just new requests (structure.md: 'available-to-work
    hours enter the math')."""
    env = simpy.Environment()
    calendar = WindowShiftCalendar(windows=((100.0, 500.0),))
    pool = LaborPool(
        name="night_crew",
        headcount=2,
        skills=frozenset({"operator"}),
        shift_calendar=calendar,
        env=env,
    )

    assert pool.available == 0


# ---------------------------------------------------------------------------
# Adversarial additions beyond the three listed acceptance criteria.
# ---------------------------------------------------------------------------


def test_labor_pool_request_for_unknown_skill_raises():
    env = simpy.Environment()
    pool = LaborPool(
        name="assembly",
        headcount=1,
        skills=frozenset({"assembler"}),
        shift_calendar=AlwaysOnShiftCalendar(),
        env=env,
    )

    def bad_requester() -> object:
        yield from pool.request("welder", PRIORITY_LOAD)  # not in pool.skills

    env.process(bad_requester())

    with pytest.raises((ValueError, KeyError)):
        env.run(until=5.0)


def test_time_model_rate_based_with_zero_rate_raises():
    """A rate of zero is a modeling error (infinite time), not a silent
    inf/NaN — the primitive must fail closed."""
    tm = TimeModel(kind=KIND_RATE_BASED, params={"rate": 0.0})
    generator = RngRegistry(BASE_SEED, REP_INDEX).generator(SOURCE_CYCLE_TIME)

    with pytest.raises((ValueError, ZeroDivisionError)):
        tm.sample(Bundle(qty=10.0, thing="wire", uom="ft"), generator)


def test_labor_pool_headcount_greater_than_one_allows_concurrent_holders():
    env = simpy.Environment()
    pool = LaborPool(
        name="cell",
        headcount=2,
        skills=frozenset({"operator"}),
        shift_calendar=AlwaysOnShiftCalendar(),
        env=env,
    )
    observed: dict[str, int] = {}

    def holder(hold_for: float) -> object:
        handle = yield from pool.request("operator", PRIORITY_LOAD)
        yield env.timeout(hold_for)
        pool.release(handle)

    def monitor() -> object:
        yield env.timeout(1.0)  # both holders active
        observed["both_held"] = pool.available

    env.process(holder(5.0))
    env.process(holder(5.0))
    env.process(monitor())
    env.run(until=10.0)

    assert observed["both_held"] == 0
