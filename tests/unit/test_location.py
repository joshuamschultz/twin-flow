"""COMP-007 Location — unit tests (T-020, red phase).

Location (`primitives/location.py`) is the floor node and the executor of the FIXED
EIGHT-STEP order of operations (structure.md "Fixed order of operations inside a
location", D-045), once per firing:

    1. Pull from the queue per the location's pull rule (PullRule.select).
    2. Acquire machine, THEN operator. Fixed global order, always.
    3. Pull and consume input material (may additionally pull from a Stock).
    4. Charge setup, if the incoming job's setup group differs from the machine's
       current setup (SetupPolicy.changeover).
    5. Charge run time (TimeModel.sample).
    6. Apply scrap rate — scrap is an ORDINARY output bundle, computed by
       Transform.apply alongside the good output(s), never a special case (D-043).
    7. Emit output bundles: routed onward, or to a scrap destination that may be a
       Stock (which the emitted bundle is `put()` back into).
    8. Release operator, THEN machine.

Exactly ONE ProcessExecution record is appended per firing, carrying the five
committed timestamps (`instrumentation/event_log.py` EVENT_LOG_SCHEMA / RecordTuple):
queue_arrival_time, material_ready_time (nullable), actual_start, actual_end,
release_time.

RED-phase note: `Location.__init__` currently accepts only `self` and unconditionally
raises `NotImplementedError("T-021")`. Every test below constructs a real `Location`
with the full designed signature; at RED that raises `TypeError` (signature mismatch —
too many positional arguments for the stub) before ever reaching the stub's
`NotImplementedError` line. Both are the RIGHT reason to fail per the task's own rule
("Location stub raises NotImplementedError("T-021") / signature mismatch (feature
absent), not ImportError/syntax") — never an ImportError, because `Location` and
`PullRule` both import cleanly from `factory_twin.primitives.location` today.

--------------------------------------------------------------------------------
COMMITTED CONTRACT (this test file fixes it; the T-021 implementer conforms).
Since the Layer-2 compiler (COMP-016 LocationCompiler) does not exist yet, a
LocationSpec cannot be produced from YAML — it is HAND-CONSTRUCTED here directly
from real Layer-0/1 primitive OBJECTS (not raw config specs). `LocationSpec` and
`MaterialRequirement` are defined LOCALLY in this test file (mirroring the
precedent in `tests/unit/test_transform.py`, where `OutputSpec`/`TransformSpec`
were test-authored before T-009 landed): they are plain duck-typed value objects
the real `primitives/location.py` should grow to match. `Location` itself, and
`PullRule` (already implemented, T-017), ARE imported from src.

    @dataclass(frozen=True)
    class MaterialRequirement:
        stock: Stock        # an ADDITIONAL Stock pulled from at step 3, distinct
        qty: float           # from step 1's queue — e.g. glue, fasteners, feedstock
        thing: str
        uom: str

    @dataclass(frozen=True)
    class LocationSpec:
        location_id: str
        machine: Machine                      # v1 simplification: ONE machine per
                                               # Location (capacity-N mapping from a
                                               # generic PriorityResource grant to a
                                               # specific Machine instance is an open
                                               # question, out of scope for T-020/021)
        setup_policy: SetupPolicy
        pull_rule: PullRule
        time_model: TimeModel
        transform: Transform                  # an already-constructed Transform,
                                               # not a raw TransformSpec (LocationSpec
                                               # is hand-built here, not compiler output)
        registry: PartTypeRegistry
        labor_skill: str
        material_requirement: MaterialRequirement | None
        destinations: dict[str, Stock | list[Bundle]]
                                               # output bundle.thing -> where it
                                               # routes: a Stock (put() back into it,
                                               # e.g. a scrap destination) or a plain
                                               # list (a sink standing in for "routed
                                               # onward", since no downstream Location
                                               # graph exists yet)

    loc = Location(
        spec: LocationSpec,
        env: simpy.Environment,
        machine_pool: simpy.PriorityResource,  # capacity == 1 for these tests
        labor_pool: LaborPool,                 # NOT a raw resource — Location must
                                                # call LaborPool.request(skill, prio)
                                                # / .release(handle), so skill and
                                                # shift-calendar gating (COMP-012)
                                                # apply to the operator leg
        event_log: EventLog,
        rng: RngRegistry,
    )

    loc.enqueue(bundle: Bundle) -> None
        # Appends `bundle` to the location's internal queue and stamps this
        # bundle's queue_arrival_time = env.now. Does not block.

    proc = env.process(loc.run())
        # A SimPy process (generator method) that loops forever, executing the
        # fixed eight-step order once per firing. Caller starts it explicitly via
        # env.process(...) — construction alone does not start it.

ARCHITECTURE NOTE flagged for review (not resolved here): `engine/acquire.py`'s
`ResourceAcquirer` accepts two raw `simpy.PriorityResource` objects and cannot
compose with `LaborPool.request(skill, priority)` (a different signature, gated by
a shift calendar) without reaching into `LaborPool`'s private `_resource`. Since
COMP-012 (LaborPool) is a declared COMP-007 dependency and the task instructions
name `LaborPool.request+release` as an explicit spy target, this test's contract
has Location perform the machine leg directly against `machine_pool` and the
operator leg via `LaborPool`, preserving the SAME fixed global order (machine,
then operator; released operator, then machine) that `ResourceAcquirer` guarantees,
without literally routing through it. This is a real gap between COMP-002's
generic-resource signature and COMP-012's richer one; it should get a decision-log
entry rather than being silently resolved by either component.

--------------------------------------------------------------------------------
SPYING MECHANISM. Each collaborator is wrapped via a thin subclass that appends
`(label, env.now)` to a SHARED `probe: list[tuple[str, float]]` at the moment its
public method is CALLED (synchronous, before any blocking `yield`/`yield from`
completes) and then delegates to the real implementation via `super()`. Location
must call through these injected objects' public methods — never reimplement their
logic inline — for the probe to observe anything, which is exactly what dependency
injection is for. This works identically whether Location composes the dual
acquire via `ResourceAcquirer` internally or directly against the pools, as long as
it calls `.request()`/`.release()` on the OBJECTS this test hands it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pytest
import simpy

from factory_twin.engine.clock import RunContext
from factory_twin.engine.rng import RngRegistry
from factory_twin.instrumentation.event_log import EVENT_LOG_SCHEMA, PROCESS_NAMES, EventLog
from factory_twin.primitives.bundle import Bundle
from factory_twin.primitives.cell import Machine, SetupPolicy
from factory_twin.primitives.labor import LaborPool
from factory_twin.primitives.location import Location, PullRule
from factory_twin.primitives.part import PartTypeRegistry
from factory_twin.primitives.stock import Stock
from factory_twin.primitives.time_model import TimeModel
from factory_twin.primitives.transform import OutputSpec, Transform, TransformSpec

# ---------------------------------------------------------------------------
# Fixture vocabulary
# ---------------------------------------------------------------------------

LOCATION_ID = "loc_1"
SETUP_GROUP = "grp_a"
INPUT_THING = "blank"
GOOD_THING = "good"
SCRAP_THING = "scrap"
MATERIAL_THING = "glue"
MATERIAL_UOM = "each"
MATERIAL_QTY = 1.0
INPUT_QTY = 100.0
GOOD_FRACTION = 0.95
SCRAP_FRACTION = 0.05
RUN_RATE = 2.0  # qty per second -> run seconds = INPUT_QTY / RUN_RATE
RUN_SECONDS = INPUT_QTY / RUN_RATE
DEFAULT_SETUP_SECONDS = 50.0

STANDARD_FIRING_SEQUENCE = (
    "pull_rule.select",
    "machine.acquire",
    "labor.request",
    "material_stock.pull",
    "setup.changeover",
    "time.sample",
    "transform.apply",
    "scrap_stock.put",
    "labor.release",
    "machine.release",
)

RECORD_FIELDS = (
    "location_id",
    "part_id",
    "lot_id",
    "process_name",
    "qty",
    "queue_arrival_time",
    "material_ready_time",
    "actual_start",
    "actual_end",
    "release_time",
    "outcome",
)


# ---------------------------------------------------------------------------
# The LocationSpec contract — test-authored, implementer conforms (T-021).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialRequirement:
    """An additional Stock pulled from at step 3, distinct from the step-1 queue."""

    stock: Stock
    qty: float
    thing: str
    uom: str


@dataclass(frozen=True)
class LocationSpec:
    """Hand-built stand-in for LocationCompiler's (COMP-016, not yet built) output."""

    location_id: str
    machine: Machine
    setup_policy: SetupPolicy
    pull_rule: PullRule
    time_model: TimeModel
    transform: Transform
    registry: PartTypeRegistry
    labor_skill: str
    material_requirement: MaterialRequirement | None
    destinations: dict[str, object]  # thing -> Stock | list[Bundle]


# ---------------------------------------------------------------------------
# Shift calendar fake (mirrors tests/unit/test_time_labor.py).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlwaysOnShiftCalendar:
    def is_on_shift(self, t: float) -> bool:
        return True

    def next_shift_start(self, t: float) -> float:  # pragma: no cover - never off-shift
        return t


# ---------------------------------------------------------------------------
# Probing subclasses — append (label, env.now) to a shared probe list at call
# time, then delegate to the real implementation via super(). Location must
# call through these injected instances for anything to be observed.
# ---------------------------------------------------------------------------


class ProbingPriorityResource(simpy.PriorityResource):
    def __init__(self, env: simpy.Environment, capacity: int, probe: list[tuple[str, float]]):
        super().__init__(env, capacity=capacity)
        self._probe = probe
        self._probe_env = env

    def request(self, *args: object, **kwargs: object) -> object:
        self._probe.append(("machine.acquire", self._probe_env.now))
        return super().request(*args, **kwargs)

    def release(self, *args: object, **kwargs: object) -> object:
        self._probe.append(("machine.release", self._probe_env.now))
        return super().release(*args, **kwargs)


class ProbingLaborPool(LaborPool):
    def __init__(self, *args: object, probe: list[tuple[str, float]], **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._probe = probe

    def request(self, skill: str, priority: int):  # type: ignore[override]
        self._probe.append(("labor.request", self.env.now))
        return super().request(skill, priority)

    def release(self, handle: object) -> None:
        self._probe.append(("labor.release", self.env.now))
        super().release(handle)  # type: ignore[arg-type]


class ProbingStock(Stock):
    def __init__(
        self,
        thing: str,
        uom: str,
        env: simpy.Environment,
        initial_qty: float,
        *,
        probe: list[tuple[str, float]],
        label: str,
    ) -> None:
        super().__init__(thing=thing, uom=uom, env=env, initial_qty=initial_qty)
        self._probe = probe
        self._label = label

    def pull(self, thing: str, qty: float, uom: str):  # type: ignore[override]
        self._probe.append((f"{self._label}.pull", self.env.now))
        return super().pull(thing, qty, uom)

    def put(self, bundle: Bundle) -> None:
        self._probe.append((f"{self._label}.put", self.env.now))
        super().put(bundle)


class ProbingSetupPolicy(SetupPolicy):
    def __init__(
        self,
        *args: object,
        probe: list[tuple[str, float]],
        env: simpy.Environment,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._probe = probe
        self._env = env

    def changeover(self, current_setup: str | None, bundle: Bundle) -> tuple[float, str]:
        self._probe.append(("setup.changeover", self._env.now))
        return super().changeover(current_setup, bundle)


class ProbingTimeModel(TimeModel):
    def __init__(
        self,
        *args: object,
        probe: list[tuple[str, float]],
        env: simpy.Environment,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._probe = probe
        self._env = env

    def sample(self, bundle: Bundle, generator: object):  # type: ignore[override]
        self._probe.append(("time.sample", self._env.now))
        return super().sample(bundle, generator)  # type: ignore[arg-type]


class ProbingTransform(Transform):
    def __init__(
        self, spec: TransformSpec, *, probe: list[tuple[str, float]], env: simpy.Environment
    ) -> None:
        super().__init__(spec)
        self._probe = probe
        self._env = env

    def apply(self, inputs: list[Bundle], registry: PartTypeRegistry) -> list[Bundle]:
        self._probe.append(("transform.apply", self._env.now))
        return super().apply(inputs, registry)


class ProbingPullRule(PullRule):
    def __init__(
        self,
        *args: object,
        probe: list[tuple[str, float]],
        env: simpy.Environment,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._probe = probe
        self._env = env

    def select(self, queue: Sequence[Bundle], current_setup: str | None) -> list[Bundle]:
        selected = super().select(queue, current_setup)
        if selected:
            self._probe.append(("pull_rule.select", self._env.now))
        return selected


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------


@dataclass
class LocationFixture:
    env: simpy.Environment
    probe: list[tuple[str, float]]
    spec: LocationSpec
    machine_pool: simpy.PriorityResource
    labor_pool: LaborPool
    event_log: EventLog
    rng: RngRegistry
    material_stock: Stock
    scrap_stock: Stock
    onward_sink: list[Bundle]


def _build_fixture(
    *,
    material_initial_qty: float = 1_000.0,
    setup_default_seconds: float = DEFAULT_SETUP_SECONDS,
    batch_spec: str | None = None,
) -> LocationFixture:
    ctx = RunContext(run_id="location-test", seed=1, horizon=10_000.0)
    env = ctx.env
    probe: list[tuple[str, float]] = []

    machine = Machine(machine_id="m1", initial_setup=None)
    machine_pool = ProbingPriorityResource(env, capacity=1, probe=probe)

    labor_pool = ProbingLaborPool(
        name="ops",
        headcount=1,
        skills=frozenset({"operator"}),
        shift_calendar=AlwaysOnShiftCalendar(),
        env=env,
        probe=probe,
    )

    material_stock = ProbingStock(
        thing=MATERIAL_THING,
        uom=MATERIAL_UOM,
        env=env,
        initial_qty=material_initial_qty,
        probe=probe,
        label="material_stock",
    )
    scrap_stock = ProbingStock(
        thing=SCRAP_THING,
        uom="piece",
        env=env,
        initial_qty=0.0,
        probe=probe,
        label="scrap_stock",
    )
    onward_sink: list[Bundle] = []

    setup_policy = ProbingSetupPolicy(
        setup_key_of={INPUT_THING: SETUP_GROUP},
        changeover_matrix={},
        default_seconds=setup_default_seconds,
        probe=probe,
        env=env,
    )
    pull_rule = ProbingPullRule(
        setup_key_of={INPUT_THING: SETUP_GROUP},
        spec=batch_spec,
        probe=probe,
        env=env,
    )
    time_model = ProbingTimeModel(
        kind="rate_based",
        params={"rate": RUN_RATE},
        probe=probe,
        env=env,
    )
    transform_spec = TransformSpec(
        outputs=[
            OutputSpec(thing=GOOD_THING, uom="piece", qty=lambda ins: ins[0].qty * GOOD_FRACTION),
            OutputSpec(thing=SCRAP_THING, uom="piece", qty=lambda ins: ins[0].qty * SCRAP_FRACTION),
        ]
    )
    transform = ProbingTransform(transform_spec, probe=probe, env=env)

    registry = PartTypeRegistry(
        {
            INPUT_THING: {"attributes": {}, "uom": "piece"},
            GOOD_THING: {"attributes": {}, "uom": "piece"},
            SCRAP_THING: {"attributes": {}, "uom": "piece"},
            MATERIAL_THING: {"attributes": {}, "uom": MATERIAL_UOM},
        }
    )

    spec = LocationSpec(
        location_id=LOCATION_ID,
        machine=machine,
        setup_policy=setup_policy,
        pull_rule=pull_rule,
        time_model=time_model,
        transform=transform,
        registry=registry,
        labor_skill="operator",
        material_requirement=MaterialRequirement(
            stock=material_stock, qty=MATERIAL_QTY, thing=MATERIAL_THING, uom=MATERIAL_UOM
        ),
        destinations={GOOD_THING: onward_sink, SCRAP_THING: scrap_stock},
    )

    event_log = EventLog()
    rng = RngRegistry(base_seed=1, replication_index=0)

    return LocationFixture(
        env=env,
        probe=probe,
        spec=spec,
        machine_pool=machine_pool,
        labor_pool=labor_pool,
        event_log=event_log,
        rng=rng,
        material_stock=material_stock,
        scrap_stock=scrap_stock,
        onward_sink=onward_sink,
    )


def _make_location(fx: LocationFixture) -> Location:
    return Location(fx.spec, fx.env, fx.machine_pool, fx.labor_pool, fx.event_log, fx.rng)


def _records(event_log: EventLog) -> list[tuple]:
    # No public accessor exists for buffered (pre-flush) records; reach into the
    # in-memory tuple buffer directly (EventLog.append/flush are the public API).
    return list(event_log._records)  # noqa: SLF001


def _times(probe: list[tuple[str, float]], label: str) -> list[float]:
    return [t for name, t in probe if name == label]


# ---------------------------------------------------------------------------
# Import sanity — Location and PullRule both import cleanly (no ImportError /
# SyntaxError). Every failure below must come from NotImplementedError or a
# constructor signature mismatch (TypeError), not a broken import line.
# ---------------------------------------------------------------------------


def test_location_and_pull_rule_import_cleanly() -> None:
    assert isinstance(Location, type)
    assert isinstance(PullRule, type)


# ---------------------------------------------------------------------------
# Acceptance 1 — the executed step sequence matches the EXACT fixed order.
# ---------------------------------------------------------------------------


def test_single_firing_executes_the_exact_fixed_eight_step_order() -> None:
    fx = _build_fixture()
    loc = _make_location(fx)
    fx.env.process(loc.run())

    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    fx.env.run(until=500.0)

    labels = [label for label, _t in fx.probe]
    assert labels == list(STANDARD_FIRING_SEQUENCE)


def test_acquire_order_is_machine_then_operator() -> None:
    fx = _build_fixture()
    loc = _make_location(fx)
    fx.env.process(loc.run())

    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    fx.env.run(until=500.0)

    labels = [label for label, _t in fx.probe]
    assert labels.index("machine.acquire") < labels.index("labor.request")


def test_release_order_is_operator_then_machine() -> None:
    fx = _build_fixture()
    loc = _make_location(fx)
    fx.env.process(loc.run())

    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    fx.env.run(until=500.0)

    labels = [label for label, _t in fx.probe]
    assert labels.index("labor.release") < labels.index("machine.release")


def test_exactly_one_process_execution_record_is_written_per_firing() -> None:
    fx = _build_fixture()
    loc = _make_location(fx)
    fx.env.process(loc.run())

    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    fx.env.run(until=500.0)

    records = _records(fx.event_log)
    assert len(records) == 1
    record = dict(zip(RECORD_FIELDS, records[0], strict=True))

    assert record["location_id"] == LOCATION_ID
    assert record["process_name"] in PROCESS_NAMES.categories
    assert record["qty"] == pytest.approx(INPUT_QTY)
    assert record["queue_arrival_time"] == pytest.approx(0.0)
    assert record["material_ready_time"] is not None
    assert record["queue_arrival_time"] <= record["material_ready_time"] <= record["actual_start"]
    assert record["actual_start"] < record["actual_end"]
    assert record["actual_end"] <= record["release_time"]
    assert list(EVENT_LOG_SCHEMA.keys()) == list(RECORD_FIELDS)


# ---------------------------------------------------------------------------
# Acceptance 2 — setup is charged AFTER material is consumed, never before.
# ---------------------------------------------------------------------------


def test_setup_is_charged_after_material_consumption_never_before() -> None:
    fx = _build_fixture()
    loc = _make_location(fx)
    fx.env.process(loc.run())

    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    fx.env.run(until=500.0)

    labels = [label for label, _t in fx.probe]
    assert labels.index("material_stock.pull") < labels.index("setup.changeover")


def test_setup_timeout_does_not_start_until_material_becomes_available() -> None:
    """The location's queue item and machine/operator are ready at t=0, but the
    required material stock is EMPTY. Setup must not begin until the stock is
    replenished — proving setup is gated on material consumption in sim time,
    not merely in call order."""
    fx = _build_fixture(material_initial_qty=0.0)
    loc = _make_location(fx)
    fx.env.process(loc.run())
    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))

    # Run up to just before replenishment: the firing must be stuck waiting on
    # the material pull, and setup must not have been charged yet.
    fx.env.run(until=40.0)
    labels_before = [label for label, _t in fx.probe]
    assert labels_before == [
        "pull_rule.select",
        "machine.acquire",
        "labor.request",
        "material_stock.pull",
    ]
    assert len(_records(fx.event_log)) == 0

    def replenish() -> object:
        yield fx.env.timeout(5.0)  # fires at t=45.0
        fx.material_stock.put(Bundle(qty=MATERIAL_QTY, thing=MATERIAL_THING, uom=MATERIAL_UOM))

    fx.env.process(replenish())
    fx.env.run(until=500.0)

    setup_times = _times(fx.probe, "setup.changeover")
    assert len(setup_times) == 1
    assert setup_times[0] >= 45.0
    assert len(_records(fx.event_log)) == 1


# ---------------------------------------------------------------------------
# Acceptance 3 — scrap is applied AFTER run time is charged; a scrapped bundle
# still consumed machine time.
# ---------------------------------------------------------------------------


def test_scrap_is_applied_after_run_time_is_charged() -> None:
    fx = _build_fixture()
    loc = _make_location(fx)
    fx.env.process(loc.run())

    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    fx.env.run(until=500.0)

    labels = [label for label, _t in fx.probe]
    assert labels.index("time.sample") < labels.index("transform.apply")

    time_sample_at = _times(fx.probe, "time.sample")[0]
    transform_apply_at = _times(fx.probe, "transform.apply")[0]
    assert transform_apply_at - time_sample_at == pytest.approx(RUN_SECONDS)


def test_scrapped_firings_actual_interval_still_includes_full_run_time() -> None:
    fx = _build_fixture()
    loc = _make_location(fx)
    fx.env.process(loc.run())

    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    fx.env.run(until=500.0)

    records = _records(fx.event_log)
    assert len(records) == 1
    record = dict(zip(RECORD_FIELDS, records[0], strict=True))

    # This firing DID scrap 5% of its output (see fixture); the run interval
    # must still reflect the full run time charged, proving the time was spent
    # (utilization stays honest) before the scrap was even discovered.
    assert record["actual_end"] - record["actual_start"] == pytest.approx(RUN_SECONDS)
    assert fx.scrap_stock.level > 0.0


# ---------------------------------------------------------------------------
# Acceptance 4 — a scrap destination pointing at a Stock returns material to it.
# ---------------------------------------------------------------------------


def test_scrap_destination_stock_level_increases_by_the_scrap_qty() -> None:
    fx = _build_fixture()
    loc = _make_location(fx)
    fx.env.process(loc.run())

    assert fx.scrap_stock.level == 0.0

    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    fx.env.run(until=500.0)

    assert fx.scrap_stock.level == pytest.approx(INPUT_QTY * SCRAP_FRACTION)


def test_good_output_routes_to_its_declared_sink_not_the_scrap_stock() -> None:
    fx = _build_fixture()
    loc = _make_location(fx)
    fx.env.process(loc.run())

    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    fx.env.run(until=500.0)

    assert len(fx.onward_sink) == 1
    assert fx.onward_sink[0].thing == GOOD_THING
    assert fx.onward_sink[0].qty == pytest.approx(INPUT_QTY * GOOD_FRACTION)
    assert fx.scrap_stock.level == pytest.approx(INPUT_QTY * SCRAP_FRACTION)


# ---------------------------------------------------------------------------
# Adversarial: steady-state — two sequential firings each execute the full
# order exactly once, and each writes exactly one record.
# ---------------------------------------------------------------------------


def test_two_sequential_firings_each_run_the_full_order_and_each_write_one_record() -> None:
    # A batch threshold of "1 piece" forces PullRule to select only ONE queued
    # bundle per firing even though both are enqueued and eligible at t=0 (see
    # PullRule.select: it always takes at least one bundle, then stops before
    # adding a second once the threshold is already met).
    fx = _build_fixture(batch_spec="1 piece")
    loc = _make_location(fx)
    fx.env.process(loc.run())

    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    loc.enqueue(Bundle(qty=INPUT_QTY, thing=INPUT_THING, uom="piece"))
    fx.env.run(until=1_000.0)

    records = _records(fx.event_log)
    assert len(records) == 2

    labels = [label for label, _t in fx.probe]
    assert labels == list(STANDARD_FIRING_SEQUENCE) * 2

    # Second firing is within the same setup group as the first (both blanks),
    # so its setup charge is zero — a real changeover only happens once, from
    # the cold (None) machine state.
    setup_times = _times(fx.probe, "setup.changeover")
    assert len(setup_times) == 2
    assert setup_times[1] > setup_times[0]
