"""COMP-007 Location + COMP-013 PullRule.

Location is the floor node and executor of the fixed eight-step order of operations:
pull from queue, acquire machine then operator, consume material, charge setup,
charge run time, apply scrap rate, emit outputs, release operator then machine.

PullRule decides what a Location takes from its queue and how much (batch threshold in
the thing's own uom); with no rule declared, arrival order applies and the fact is
recorded for the Assumptions block.
"""

from __future__ import annotations

import math
from collections.abc import Generator, Sequence
from typing import Protocol

import simpy
from simpy.resources.resource import PriorityRequest

from twinflow.engine.rng import SOURCE_CYCLE_TIME, SOURCE_ROUTING, RngRegistry
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.cell import Machine, SetupPolicy
from twinflow.primitives.labor import PRIORITY_LOAD, LaborPool
from twinflow.primitives.part import PartTypeRegistry
from twinflow.primitives.stock import Stock
from twinflow.primitives.time_model import TimeModel
from twinflow.primitives.transform import Transform

# One ProcessExecution record per firing, in EVENT_LOG_SCHEMA column order
# (instrumentation/event_log.py). Defined here Polars-free so that primitives never
# imports instrumentation (structure.md boundary): Location produces plain tuples;
# the Layer-4 EventLog owns the schema/Enum and the Polars conversion at flush time.
RecordTuple = tuple[
    str, str, str, str, float, float, float | None, float, float, float, str, float
]


class EventSink(Protocol):
    """Minimal structural sink Location appends ProcessExecution records to.

    COMP-021 `EventLog` satisfies this by having `append`. Depending on the Protocol
    rather than importing `instrumentation.event_log` keeps primitives free of
    instrumentation and Polars (structure.md Module Boundaries), while still honouring
    the SDD's COMP-007→COMP-021 data dependency: the run *writes* records, the event
    table is the only interface to instrumentation (D-017), and the writer is injected.
    """

    def append(self, record: RecordTuple) -> None: ...


class PullRule:
    """Selects the list[Bundle] to process as one job; empty when nothing is eligible.

    With no ``spec`` declared, arrival order applies (D-037/D-040: a
    dispatch-rule engine chosen on the client's behalf moves their promised
    dates on the strength of your pick — arrival order, always). The fact
    that the default applied is recorded on ``default_applied`` so the
    report's Assumptions block can name it (D-027).

    A declared ``spec`` is a batch threshold expressed in the thing's own
    unit of measure, e.g. ``"200 pieces"`` or ``"500 lb"``: eligible bundles
    accumulate in arrival order until the summed qty reaches the threshold.
    """

    def __init__(self, setup_key_of: dict[str, str], spec: str | None = None) -> None:
        self.setup_key_of = setup_key_of
        self.default_applied = spec is None
        self._threshold: float = 0.0
        if spec is not None:
            value_str, _uom = spec.split(maxsplit=1)
            self._threshold = float(value_str)

    def select(self, queue: Sequence[Bundle], current_setup: str | None) -> list[Bundle]:
        """Return the eligible bundles to pull, per D-051 order-of-operations step 1.

        Eligibility (D-047/D-012): a bundle is eligible only if
        ``setup_key_of[bundle.thing]`` matches ``current_setup``. When
        ``current_setup`` is ``None``, nothing is eligible. An ineligible
        bundle is skipped entirely — never selected, never counted toward a
        batch total — and arrival order among the remaining bundles holds.
        Never mutates ``queue``.
        """
        if current_setup is None:
            return []
        eligible = [b for b in queue if self.setup_key_of[b.thing] == current_setup]
        if self.default_applied:
            # No batch declared: a work center pulls the NEXT job only (one bundle,
            # arrival order), then loops for the next. Taking the whole queue as one
            # firing would collapse real queue dynamics - the bottleneck would never
            # build a backlog and nothing downstream would starve. Batching is opt-in
            # via `batch_size`, never the default.
            return eligible[:1]
        selected: list[Bundle] = []
        total = 0.0
        for bundle in eligible:
            if total >= self._threshold:
                break
            selected.append(bundle)
            total += bundle.qty
        return selected


class RoutingPolicy:
    """Probabilistic quality gate: routes one output `thing` among weighted sinks.

    Distinct from a fixed scrap rate (D-043): a scrap rate splits a firing's qty
    into good/scrap bundles every time, whereas a quality gate sends the WHOLE
    output down a single pass/fail branch by chance (inspection, rework). One draw
    from the SOURCE_ROUTING stream per firing (CRN, D-033) picks the branch; the
    caller (Location) owns the draw so the primitive stays a pure decision function.

    Branch probabilities are validated to sum to 1.0 at Layer 2 (model/validate.py),
    so `choose` treats the declared order as an exhaustive cumulative partition.
    """

    def __init__(self, branches: list[tuple[float, Stock | list[Bundle]]]) -> None:
        self._branches = branches

    def choose(self, u: float) -> Stock | list[Bundle]:
        """Return the sink for a uniform draw `u` in [0, 1) by cumulative probability.
        A draw landing exactly on the upper edge falls to the last branch."""
        cumulative = 0.0
        for prob, sink in self._branches:
            cumulative += prob
            if u < cumulative:
                return sink
        return self._branches[-1][1]


class MaterialRequirementLike(Protocol):
    """Structural shape `spec.material_requirement` must have (COMP-007 contract).

    An additional Stock pulled from at step 3, distinct from the step-1 queue —
    e.g. glue, fasteners, feedstock. `LocationCompiler` (COMP-016) does not exist
    yet, so this is a duck-typed contract, not a class Location constructs itself.
    """

    stock: Stock
    qty: float
    thing: str
    uom: str


class LocationSpecLike(Protocol):
    """Structural shape `spec` must have (COMP-007 contract; see COMP-016 note above).

    `destinations` maps an output bundle's `thing` to where it routes: a `Stock`
    (put back into it, e.g. a scrap destination) or a plain list (a sink standing
    in for "routed onward" until a downstream Location graph exists).
    """

    location_id: str
    machine: Machine
    setup_policy: SetupPolicy
    pull_rule: PullRule
    time_model: TimeModel
    transform: Transform
    registry: PartTypeRegistry
    labor_skill: str
    material_requirement: MaterialRequirementLike | None
    destinations: dict[str, Stock | list[Bundle]]


class Location:
    """Executes the fixed eight-step order of operations; one event record per firing.

    Step order (structure.md D-045; the "fixed eight-step order of operations"):
    pull from queue, acquire machine then operator, consume material, charge setup,
    charge run time, apply scrap rate, emit outputs, release operator then machine.
    """

    def __init__(
        self,
        spec: LocationSpecLike,
        env: simpy.Environment,
        machine_pool: simpy.PriorityResource,
        labor_pool: LaborPool,
        event_log: EventSink,
        rng: RngRegistry,
    ) -> None:
        self.spec = spec
        self.env = env
        self.machine_pool = machine_pool
        self.labor_pool = labor_pool
        self.event_log = event_log
        self.rng = rng
        self._queue: list[Bundle] = []
        self._arrival_times: dict[int, float] = {}
        self._arrival_event: simpy.Event = env.event()
        self._lot_counter = 0
        self._capacity: int = getattr(spec, "capacity", 1)
        self._in_flight = 0
        self._free_event: simpy.Event | None = None
        # Active-control dispatch rule (A1): which queued job this center runs next.
        # Default "fifo" reproduces arrival order exactly. The four policies mirror
        # `twinflow.adapters.dispatch` (that is the external-facing surface of the
        # same rules); the ordering is implemented natively here so a lower engine
        # layer never imports a higher one (layering purity beats DRY, D-044).
        self._dispatch: str = getattr(spec, "dispatch", "fifo")

    def enqueue(self, bundle: Bundle) -> None:
        """Append `bundle` to the queue, stamping its queue_arrival_time. Non-blocking."""
        self._queue.append(bundle)
        self._arrival_times[id(bundle)] = self.env.now
        if not self._arrival_event.triggered:
            self._arrival_event.succeed()

    def run(self) -> Generator[simpy.Event, None, None]:
        """Dispatch loop: pull the next job (a single puller, so no queue race),
        then run its firing concurrently on one of the center's `capacity`
        machines. With capacity 1 this is exactly the old one-job-at-a-time loop;
        with capacity N up to N firings run in parallel (COMP-030)."""
        while True:
            selected = yield from self._wait_for_selection()
            self.env.process(self._fire_and_release(selected))
            self._in_flight += 1
            while self._in_flight >= self._capacity:
                self._free_event = self.env.event()
                yield self._free_event

    def _fire_and_release(self, selected: list[Bundle]) -> Generator[simpy.Event, None, None]:
        """Run one firing, then free the machine so the dispatcher can pull again."""
        try:
            yield from self._fire(selected)
        finally:
            self._in_flight -= 1
            if self._free_event is not None and not self._free_event.triggered:
                self._free_event.succeed()
                self._free_event = None

    def _target_setup(self, ordered: list[Bundle]) -> str | None:
        """The setup group this firing should select against.

        A warm machine (`current_setup` already set) interleaves within its own
        group. A cold machine (D-047: "each machine tracks its own last-processed
        part" starts at `None`) has no established group yet, so the group is read
        off the head of the DISPATCH-ORDERED queue — the job the active-control rule
        would run first sets the machine's initial setup.
        """
        current = self.spec.machine.current_setup
        if current is not None:
            return current
        if not ordered:
            return None
        return self.spec.pull_rule.setup_key_of[ordered[0].thing]

    def _dispatch_order(self) -> list[Bundle]:
        """The queue re-sequenced by the active-control dispatch rule (A1).

        Every rule shares one key `(rush_rank, policy_value, arrival_time)`: a job
        with a higher `priority` attribute (a rush order) always sorts first; then
        the policy value (EDD = due date, SPT = estimated processing time, critical
        ratio = slack per unit work, FIFO = arrival time); ties break by arrival
        order. With the default FIFO rule and no priorities stamped this is exactly
        the original arrival order (a stable sort on the insertion sequence), so a
        model that declares no `dispatch` behaves identically to before.
        """
        now = self.env.now

        def key(bundle: Bundle) -> tuple[float, float, float]:
            arrival = self._arrival_times.get(id(bundle), 0.0)
            rush_rank = -float(bundle.attrs.get("priority", 0.0))
            due = float(bundle.attrs.get("due_date", math.inf))
            if self._dispatch == "edd":
                policy_value = due
            elif self._dispatch == "spt":
                policy_value = self.spec.time_model.estimate(bundle)
            elif self._dispatch == "critical_ratio":
                processing = max(self.spec.time_model.estimate(bundle), 1e-9)
                policy_value = (due - now) / processing
            else:  # fifo
                policy_value = arrival
            return (rush_rank, policy_value, arrival)

        return sorted(self._queue, key=key)

    def _wait_for_selection(self) -> Generator[simpy.Event, None, list[Bundle]]:
        """Step 1: pull the dispatch-preferred job per the pull rule, or wait."""
        while True:
            ordered = self._dispatch_order()
            selected = self.spec.pull_rule.select(ordered, self._target_setup(ordered))
            if selected:
                selected_ids = {id(bundle) for bundle in selected}
                self._queue = [b for b in self._queue if id(b) not in selected_ids]
                return selected
            if self._arrival_event.triggered:
                self._arrival_event = self.env.event()
            yield self._arrival_event

    def _fire(self, selected: list[Bundle]) -> Generator[simpy.Event, None, None]:
        """Execute steps 2-8 of the fixed order for one selected job."""
        queue_arrival_time = min(self._arrival_times.pop(id(b)) for b in selected)

        # Step 2: acquire machine, THEN operator — fixed global order (D-045).
        # D-053: Location is the sole dual-acquire site in v1 and inlines this
        # order directly (rather than routing through engine/acquire.py's
        # ResourceAcquirer) because LaborPool's richer, shift-gated
        # request(skill, priority) signature does not compose with
        # ResourceAcquirer's raw dual-PriorityResource contract.
        machine_request: PriorityRequest = self.machine_pool.request()
        yield machine_request
        operator_handle: PriorityRequest | None = None
        try:
            operator_handle = yield from self.labor_pool.request(
                self.spec.labor_skill, PRIORITY_LOAD
            )

            # Step 3: pull and consume required input material, if any is declared.
            material_ready_time: float | None = None
            material_requirement = self.spec.material_requirement
            if material_requirement is not None:
                yield from material_requirement.stock.pull(
                    material_requirement.thing, material_requirement.qty, material_requirement.uom
                )
                material_ready_time = self.env.now

            # T-021 v1 simplification: use the first selected bundle as the job
            # for setup/time sampling. A batch spanning multiple `thing`s inside
            # one setup group (D-047) is out of scope until a real batch case
            # exercises it.
            job_bundle = selected[0]

            # Step 4: charge setup, if the job's setup group differs from the
            # machine's current one (D-045: setup sits after material pull).
            setup_seconds, new_setup = self.spec.setup_policy.changeover(
                self.spec.machine.current_setup, job_bundle
            )
            yield self.env.timeout(setup_seconds)
            self.spec.machine.current_setup = new_setup

            actual_start = self.env.now

            # Step 5: charge run time.
            phase_times = self.spec.time_model.sample(
                job_bundle, self.rng.generator(SOURCE_CYCLE_TIME)
            )
            yield self.env.timeout(phase_times.load + phase_times.run + phase_times.unload)

            actual_end = self.env.now

            # Steps 6-7: scrap is an ordinary output bundle, never a special case
            # (D-043) — apply the transform, then emit every output to its
            # declared destination (a Stock is put() into; a list sink appended).
            outputs = self.spec.transform.apply(selected, self.spec.registry)
            routers = getattr(self.spec, "routers", {})
            for output_bundle in outputs:
                router = routers.get(output_bundle.thing)
                if router is not None:
                    # Probabilistic quality gate: one draw from the dedicated
                    # SOURCE_ROUTING stream picks a whole-unit pass/fail branch.
                    draw = float(self.rng.generator(SOURCE_ROUTING).random())
                    destination = router.choose(draw)
                else:
                    destination = self.spec.destinations[output_bundle.thing]
                if isinstance(destination, Stock):
                    destination.put(output_bundle)
                else:
                    destination.append(output_bundle)

            release_time = self.env.now

            self._lot_counter += 1
            self.event_log.append(
                (
                    self.spec.location_id,
                    job_bundle.thing,
                    f"{self.spec.location_id}-{self._lot_counter}",
                    "transform",
                    sum(bundle.qty for bundle in selected),
                    queue_arrival_time,
                    material_ready_time,
                    actual_start,
                    actual_end,
                    release_time,
                    "complete",
                    setup_seconds,
                )
            )
        finally:
            # Step 8: release operator, THEN machine — the reverse of the
            # acquisition order, and symmetric on any mid-firing failure
            # (D-053): release only whatever leg was actually acquired.
            if operator_handle is not None:
                self.labor_pool.release(operator_handle)
            self.machine_pool.release(machine_request)
