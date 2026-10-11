"""Operation rules of the problem builder (COMP-116 steps 4 to 7).

Shared by production-order jobs and rush jobs, so both follow one set of rules:

* options (step 4): a machine-level ERP option gives one `ProcessingOption`; a
  work-center-level option gives one per ACTIVE machine of that center; times divide by the
  machine's efficiency; the ERP setup is added to `fixed_seconds` unless the MACHINE's own
  work center has a profile setup function (D-124; that is the function TF-002 applies);
  `preference_cost = preference * penalty`. An ERP option on a substitute center sets
  `ProcessingOption.work_center_id` to that center (None when it is the op's center). When
  two ERP options reach one machine the cheapest wins, so option order never matters;
* precedence (step 5, D-123): `walk_route` links each in-plant op to the previous one with
  `min_lag = move + queue + outside lead between`; outside lead before the first in-plant op
  shifts the release, outside lead after the last shifts the due date;
* status (step 6): an in-process op runs on its machine (or an inferred free one, see
  `_jobs.running_machines`) for `qty_remaining * unit_seconds`, with no operator needs (its
  crew is already committed);
* shift crossing and crews (TF-005 ruling): a batch op (one with a `BatchSpec`) takes
  `batch_shift_crossing`, every other op `shift_crossing_default`; a `finish_unattended`
  op holds its crew for the setup phase only, any other op for setup and run;
* batch lots (orchestrator ruling, SDD deviation): a batch op whose lot exceeds its
  `BatchSpec.capacity` gets no `batch_family`, so it runs alone for its full time.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from twinflow.erp.profile import ErpPolicy, ShiftCrossing
from twinflow.erp.schema import ErpSnapshot, Machine, OperationOption, RoutingOperation
from twinflow.scheduling.model import (
    Operation,
    OperationStatus,
    OperatorNeed,
    Precedence,
    ProcessingOption,
)

__all__ = [
    "RouteStep",
    "RouteWalk",
    "ShopIndex",
    "build_shop_index",
    "lot_fits_batch",
    "make_operation",
    "preferred_machines",
    "processing_options",
    "walk_route",
]


@dataclass(frozen=True)
class ShopIndex:
    """Machine lookups and the option policy, built once per build."""

    machines: dict[str, Machine]
    active_by_center: dict[str, tuple[Machine, ...]]
    penalty: float
    setup_centers: frozenset[str]


@dataclass(frozen=True)
class RouteStep:
    """One in-plant op with its own queue contribution and link to the previous one."""

    op: RoutingOperation
    queue: float
    predecessor: str | None
    lag: float


@dataclass(frozen=True)
class RouteWalk:
    """In-plant steps in sequence order plus the outside lead before and after them."""

    steps: tuple[RouteStep, ...]
    release_lead: float
    due_lead: float
    trailing_outside: tuple[RoutingOperation, ...]


def build_shop_index(snapshot: ErpSnapshot, policy: ErpPolicy) -> ShopIndex:
    """Index machines by id (first wins) and active machines per center (sorted by id)."""
    machines: dict[str, Machine] = {}
    active: defaultdict[str, list[Machine]] = defaultdict(list)
    for machine in sorted(snapshot.machines, key=lambda m: m.id):
        machines.setdefault(machine.id, machine)
        if machine.active:
            active[machine.work_center_id].append(machine)
    return ShopIndex(
        machines=machines,
        active_by_center={center: tuple(found) for center, found in active.items()},
        penalty=policy.alternate_penalty_seconds,
        setup_centers=frozenset(policy.setup_functions),
    )


def processing_options(op: RoutingOperation, shop: ShopIndex) -> tuple[ProcessingOption, ...]:
    """Expand the op's ERP options onto active machines, one option per machine, by id."""
    best: dict[str, ProcessingOption] = {}
    for option in op.options:
        substitute = None if option.work_center_id == op.work_center_id else option.work_center_id
        for machine in _targets(option, shop):
            with_setup = machine.work_center_id not in shop.setup_centers
            candidate = _processing_option(option, machine, with_setup, shop.penalty, substitute)
            current = best.get(machine.id)
            if current is None or _cost(candidate) < _cost(current):
                best[machine.id] = candidate
    return tuple(best[machine_id] for machine_id in sorted(best))


def preferred_machines(op: RoutingOperation, shop: ShopIndex) -> tuple[str, ...]:
    """The op's active option machines, most preferred first (a center option: by id)."""
    ranked = sorted(op.options, key=lambda o: (o.preference, o.machine_id or "", o.work_center_id))
    found: dict[str, None] = {}
    for option in ranked:
        found.update(dict.fromkeys(machine.id for machine in _targets(option, shop)))
    return tuple(found)


def walk_route(operations: Iterable[RoutingOperation]) -> RouteWalk:
    """Link the live ops (done ones already removed) in `sequence` order (D-123)."""
    steps: list[RouteStep] = []
    trailing: list[RoutingOperation] = []
    previous: RoutingOperation | None = None
    lead = release_lead = 0.0
    for op in sorted(operations, key=lambda o: (o.sequence, o.op_id)):
        if op.outside is not None:
            lead += op.outside.lead_seconds
            trailing.append(op)
            continue
        if previous is None:
            release_lead = lead
            steps.append(RouteStep(op=op, queue=op.queue_seconds, predecessor=None, lag=0.0))
        else:
            lag = previous.move_seconds + op.queue_seconds + lead
            steps.append(RouteStep(op=op, queue=0.0, predecessor=previous.op_id, lag=lag))
        previous, lead, trailing = op, 0.0, []
    return RouteWalk(
        steps=tuple(steps),
        release_lead=release_lead,
        due_lead=lead,
        trailing_outside=tuple(trailing),
    )


def make_operation(
    job_id: str,
    step: RouteStep,
    options: tuple[ProcessingOption, ...],
    *,
    quantity: float,
    batch_default: str,
    ready_time: float,
    status: OperationStatus,
    policy: ErpPolicy,
) -> Operation:
    """Assemble the TF-002 operation `job_id:op_id` for one route step."""
    op = step.op
    family = _setup_family(op, policy)
    predecessors = (
        ()
        if step.predecessor is None
        else (Precedence(operation_id=f"{job_id}:{step.predecessor}", min_lag=step.lag),)
    )
    crossing = policy.shift_crossing_default if op.batch is None else policy.batch_shift_crossing
    return Operation(
        id=f"{job_id}:{op.op_id}",
        work_center_id=op.work_center_id,
        quantity=quantity,
        options=options,
        predecessors=predecessors,
        ready_time=ready_time,
        setup_params=dict(op.setup_params),
        setup_family=family,
        batch_family=(family or batch_default) if lot_fits_batch(op, quantity) else None,
        operator_needs=_operator_needs(op, crossing, status),
        shift_crossing=crossing,
        status=status,
    )


def _operator_needs(
    op: RoutingOperation, crossing: ShiftCrossing, status: OperationStatus
) -> tuple[OperatorNeed, ...]:
    """The op's crew; none while running, the setup phase only when it finishes unattended."""
    if op.crew is None or status.state == "in_progress":
        return ()
    return (
        OperatorNeed(
            pool_id=op.crew.labor_pool_id,
            quantity=op.crew.size,
            phase="setup" if crossing == "finish_unattended" else "both",
        ),
    )


def lot_fits_batch(op: RoutingOperation, quantity: float) -> bool:
    """A batch op is batched only when its lot fits the capacity; a bigger lot runs alone."""
    return op.batch is not None and quantity <= op.batch.capacity


def _targets(option: OperationOption, shop: ShopIndex) -> tuple[Machine, ...]:
    """Active machines an ERP option can run on (unknown or inactive: none)."""
    if option.machine_id is None:
        return shop.active_by_center.get(option.work_center_id, ())
    machine = shop.machines.get(option.machine_id)
    return (machine,) if machine is not None and machine.active else ()


def _processing_option(
    option: OperationOption,
    machine: Machine,
    with_setup: bool,
    penalty: float,
    substitute: str | None,
) -> ProcessingOption:
    setup = option.setup_seconds if with_setup else 0.0
    return ProcessingOption(
        machine_id=machine.id,
        fixed_seconds=(option.fixed_run_seconds + setup) / machine.efficiency,
        unit_seconds=option.run_seconds_per_unit / machine.efficiency,
        preference_cost=option.preference * penalty,
        work_center_id=substitute,
    )


def _cost(option: ProcessingOption) -> tuple[float, float, float]:
    return (option.preference_cost, option.fixed_seconds, option.unit_seconds)


def _setup_family(op: RoutingOperation, policy: ErpPolicy) -> str | None:
    """The op's value of the profile's `setup_family_param`, as text."""
    if policy.setup_family_param is None:
        return None
    value = op.setup_params.get(policy.setup_family_param)
    return None if value is None else str(value)
