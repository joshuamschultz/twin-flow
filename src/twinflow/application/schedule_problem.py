"""Translate a scenario capsule and an order snapshot into a v2 `SolveRequest` (COMP-013).

The capsule's compiled model is the shop and the snapshot's orders are the jobs:

- location -> `WorkCenter`; capacity N -> unary machines named as the DES names them
  (`<machine>` for capacity 1, `<machine>-k` for k = 1..N, D-062);
- order x routing step -> one lot `Operation` of the order quantity (D-061), with id
  `order-{i}:stage-{location}`; its options run `unit_seconds` per piece, the time
  model's estimate for a one-piece bundle, with no fixed time;
- routing order -> end-to-gate precedence; `labor_skill` -> one operator from the
  single pool holding that skill, held for setup and run (the DES acquires machine
  then operator and keeps the operator for the whole firing, D-063);
- labor calendars -> explicit working intervals expanded with `WorkingCalendar` over
  [0, latest release + one year] in simulation seconds (D-065);
- `setup_key` plus `changeover_seconds` -> a family-matrix setup function: the same
  setup group costs 0, a cold machine costs the changeover (D-064).

Anything the v2 model cannot express is refused with one named issue per feature and
status `blocked`; nothing is dropped silently. There is no piece or operation cap.
`firings_from_plan` expands each verified lot back into the DES per-piece firings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from twinflow.model import CompiledModel, LaborPoolConfig
from twinflow.model.schema import LocationSpec
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.calendar import CalendarConfig, WorkingCalendar
from twinflow.primitives.location import ScheduledFiring
from twinflow.primitives.time_model import KIND_BATCH_HOLD, KIND_DISTRIBUTION
from twinflow.scenario import ScenarioCapsule
from twinflow.scheduling.model import (
    MAX_TEXT_LENGTH,
    SCHEMA_VERSION,
    Assignment,
    Calendar,
    FamilyMatrixEntry,
    Interval,
    Job,
    Machine,
    Operation,
    OperatorNeed,
    OperatorPool,
    Precedence,
    Problem,
    ProcessingOption,
    ScheduleIssue,
    SchedulingProfile,
    SetupFunction,
    SolveRequest,
    SolveResult,
    WorkCenter,
)
from twinflow.scheduling.profile import default_profile

TRANSLATOR_VERSION = "capsule-ops-2"
PLANNING_WINDOW_SECONDS = 31_536_000.0
"""Calendars are expanded one year past the latest release (the DES horizon, D-065)."""
SOLUTION_STATUSES = frozenset({"FEASIBLE", "OPTIMAL"})


@dataclass(frozen=True)
class Translation:
    """The translator's answer: a ready request with its digest, or named blockers."""

    status: Literal["ready", "blocked"]
    issues: tuple[ScheduleIssue, ...]
    request: SolveRequest | None
    problem_digest: str | None
    translator_version: str = TRANSLATOR_VERSION


def build_request(
    scenario: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    profile: SchedulingProfile | None = None,
) -> Translation:
    """Build the v2 request for a scenario record and its schedule snapshot (REQ-081).

    `profile` None means `default_profile()`. Status is `blocked` with one issue per
    unsupported feature, a scenario revision mismatch, or a capsule that does not compile.
    """
    if (
        scenario["id"] != snapshot["scenario_id"]
        or scenario["digest"] != snapshot["scenario_digest"]
    ):
        return _blocked(
            [_issue("scenario_mismatch", "scenario", "Snapshot belongs to another revision")]
        )
    try:
        model = ScenarioCapsule.from_dict(scenario["capsule"]).compile().model
    except ValueError as exc:
        return _blocked([_issue("invalid_scenario", "scenario.capsule", str(exc))])
    orders: Sequence[Mapping[str, Any]] = snapshot["orders"]
    issues = [*_model_issues(model), *_order_issues(model, orders)]
    if issues:
        return _blocked(issues)
    request = SolveRequest(
        schema_version=SCHEMA_VERSION,
        problem=_problem(str(scenario["id"]), model, orders),
        profile=profile if profile is not None else default_profile(),
    )
    canonical = json.dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return Translation(
        status="ready",
        issues=(),
        request=request,
        problem_digest=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def firings_from_plan(translation: Translation, result: SolveResult) -> list[ScheduledFiring]:
    """Expand each verified lot into DES piece firings, in run order (REQ-082, D-061).

    Piece p of the lot of job `order-{i}` at location L becomes firing
    `order-{i}:piece-{p}:stage-L` (piece id `order-{i}:piece-{p}`) on the assigned
    machine, planned on `[setup_end + p * unit, setup_end + (p + 1) * unit)`.
    """
    request = translation.request
    if request is None:
        raise ValueError("Only a ready translation has a plan to simulate")
    if not result.verified or result.schedule.status not in SOLUTION_STATUSES:
        raise ValueError("Only a verified feasible plan can be simulated")
    by_operation = {a.operation_id: a for a in result.schedule.assignments}
    lots = [(job.id, op) for job in request.problem.jobs for op in job.operations]
    if set(by_operation) != {op.id for _, op in lots}:
        raise ValueError("Solver assignment does not cover every translated operation")
    firings = [
        firing for job_id, op in lots for firing in _piece_firings(job_id, op, by_operation[op.id])
    ]
    return sorted(firings, key=lambda f: (f.planned_start, f.machine_id, f.operation_id))


def _piece_firings(job_id: str, op: Operation, assignment: Assignment) -> list[ScheduledFiring]:
    unit = {o.machine_id: o.unit_seconds for o in op.options}[assignment.machine_id]
    firings = []
    for piece in range(int(op.quantity)):
        start = assignment.setup_end + piece * unit
        piece_id = f"{job_id}:piece-{piece}"
        firings.append(
            ScheduledFiring(
                operation_id=f"{piece_id}:stage-{op.work_center_id}",
                piece_id=piece_id,
                location_id=op.work_center_id,
                machine_id=assignment.machine_id,
                planned_start=start,
                planned_end=start + unit,
            )
        )
    return firings


# --- issues ---------------------------------------------------------------------------------


def _issue(code: str, path: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path=path, message=message[:MAX_TEXT_LENGTH])


def _blocked(issues: Sequence[ScheduleIssue]) -> Translation:
    return Translation(status="blocked", issues=tuple(issues), request=None, problem_digest=None)


def _model_issues(model: CompiledModel) -> Iterator[ScheduleIssue]:
    if model.stocks:
        yield _issue("material_stock", "stocks", "Material stocks are not scheduled")
    if model.release.policy != "plan":
        yield _issue("release_policy", "release", "Only plan release can be scheduled")
    yield from _shared_machines(model.locations)
    yield from _rework_cycles(model.routing)
    yield from _labor_issues(model)
    calendared = {skill for p in model.labor_pools if p.calendar for skill in p.skills}
    for spec in model.locations:
        yield from _location_issues(spec, spec.labor_skill in calendared)


def _shared_machines(locations: Sequence[LocationSpec]) -> Iterator[ScheduleIssue]:
    owners: dict[str, list[str]] = {}
    for spec in locations:
        owners.setdefault(spec.machine.machine_id, []).append(spec.location_id)
    for machine_id, owned_by in owners.items():
        if len(owned_by) > 1:
            message = f"Machine {machine_id} is shared by locations {', '.join(owned_by)}"
            yield _issue("shared_machine", f"machines.{machine_id}", message)


def _rework_cycles(routing: Mapping[str, Sequence[str]]) -> Iterator[ScheduleIssue]:
    for part, steps in routing.items():
        if len(set(steps)) < len(steps):
            message = "A routing that revisits a location (rework) is not scheduled"
            yield _issue("rework_cycle", f"routing.{part}", message)


def _labor_issues(model: CompiledModel) -> Iterator[ScheduleIssue]:
    for skill in sorted({spec.labor_skill for spec in model.locations}):
        holders = [pool.name for pool in model.labor_pools if skill in pool.skills]
        if not holders:
            yield _issue("skill_without_pool", f"labor.{skill}", "No labor pool has this skill")
        elif len(holders) > 1:
            message = f"Skill {skill} is in several pools: {', '.join(holders)}"
            yield _issue("skill_in_several_pools", f"labor.{skill}", message)
    for pool in model.labor_pools:
        if pool.absence_rate > 0:
            message = "Random operator absence is not scheduled"
            yield _issue("labor_absence", f"labor.pools.{pool.name}", message)


def _location_issues(spec: LocationSpec, calendared: bool) -> Iterator[ScheduleIssue]:
    kind = spec.time_model.kind
    crossing = spec.shift_crossing
    checks = (
        (kind == KIND_DISTRIBUTION, "distribution_time_model", "A sampled time model"),
        (kind == KIND_BATCH_HOLD, "batch_hold_time_model", "A batch hold time model"),
        (spec.quality_gate is not None, "quality_gate", "A probabilistic quality gate"),
        (spec.split_output, "split_output", "Splitting output into units"),
        (not spec.pull_rule.default_applied, "batch_size", "A DES batch size"),
        (spec.breakdown is not None, "machine_breakdown", "Random machine breakdowns"),
        (
            crossing == "finish_unattended" or (calendared and crossing == "overtime"),
            "unsupported_shift_crossing",
            f"Shift crossing {crossing}",
        ),
    )
    path = f"locations.{spec.location_id}"
    for present, code, what in checks:
        if present:
            yield _issue(code, path, f"{what} cannot be expressed in the schedule model")
    yield from _output_issues(spec, path)


def _output_issues(spec: LocationSpec, path: str) -> Iterator[ScheduleIssue]:
    """Lots keep the order quantity only when one piece in gives one piece out."""
    outputs = spec.transform.apply([_one_piece(spec)], spec.registry)
    if len(outputs) > 1:
        yield _issue("multiple_outputs", path, "Scrap or co-products change lot quantities")
    elif outputs and outputs[0].qty != 1.0:
        yield _issue("non_unit_ratio", path, "One piece in does not give one piece out")


def _order_issues(
    model: CompiledModel, orders: Sequence[Mapping[str, Any]]
) -> Iterator[ScheduleIssue]:
    for index, order in enumerate(orders):
        if order["product"] not in model.routing:
            message = f"Product {order['product']} has no routing"
            yield _issue("unknown_product", f"orders[{index}].product", message)


# --- problem --------------------------------------------------------------------------------


def _one_piece(spec: LocationSpec) -> Bundle:
    """One unit of the location's basis input, the bundle the DES fires per piece."""
    thing = next(iter(spec.pull_rule.setup_key_of))
    return Bundle(qty=1.0, thing=thing, uom=spec.registry.uom(thing))


@dataclass(frozen=True)
class _Station:
    """What every operation at one location shares: machines, time, setup, operator."""

    location_id: str
    options: tuple[ProcessingOption, ...]
    family: str
    operator_need: OperatorNeed
    shift_crossing: Literal["forbid", "pause"]

    def operation(self, op_id: str, quantity: float, predecessor: str | None) -> Operation:
        links = (Precedence(operation_id=predecessor),) if predecessor is not None else ()
        return Operation(
            id=op_id,
            work_center_id=self.location_id,
            quantity=quantity,
            options=self.options,
            predecessors=links,
            setup_family=self.family,
            operator_needs=(self.operator_need,),
            shift_crossing=self.shift_crossing,
        )


def _machine_ids(spec: LocationSpec) -> list[str]:
    base = spec.machine.machine_id
    if spec.capacity == 1:
        return [base]
    return [f"{base}-{slot + 1}" for slot in range(spec.capacity)]


def _station(spec: LocationSpec, pool: LaborPoolConfig) -> _Station:
    unit = spec.time_model.estimate(_one_piece(spec))
    options = tuple(
        ProcessingOption(machine_id=m, fixed_seconds=0.0, unit_seconds=unit, preference_cost=0.0)
        for m in _machine_ids(spec)
    )
    # The DES pauses only on a declared calendar; without one every policy is the same.
    pauses = pool.calendar is not None and spec.shift_crossing == "pause"
    return _Station(
        location_id=spec.location_id,
        options=options,
        family=_setup_group(spec),
        operator_need=OperatorNeed(pool_id=pool.name, quantity=1, phase="both"),
        shift_crossing="pause" if pauses else "forbid",
    )


def _setup_group(spec: LocationSpec) -> str:
    return spec.setup_policy.setup_key_of[_one_piece(spec).thing]


def _setup_function(spec: LocationSpec) -> SetupFunction | None:
    """Same setup group costs 0; a cold machine costs the changeover, as in the DES."""
    if spec.setup_policy.default_seconds == 0:
        return None
    group = _setup_group(spec)
    return SetupFunction(
        id=f"setup-{spec.location_id}",
        terms=(),
        family_matrix=(FamilyMatrixEntry(from_family=group, to_family=group, seconds=0.0),),
        default_seconds=spec.setup_policy.default_seconds,
    )


def _shop(
    model: CompiledModel,
) -> tuple[tuple[WorkCenter, ...], tuple[Machine, ...], tuple[SetupFunction, ...]]:
    work_centers: list[WorkCenter] = []
    machines: list[Machine] = []
    functions: list[SetupFunction] = []
    for spec in model.locations:
        function = _setup_function(spec)
        if function is not None:
            functions.append(function)
        work_centers.append(
            WorkCenter(
                id=spec.location_id,
                setup_function_id=function.id if function is not None else None,
            )
        )
        machines.extend(Machine(id=m, work_center_id=spec.location_id) for m in _machine_ids(spec))
    return tuple(work_centers), tuple(machines), tuple(functions)


def _calendar_id(pool: LaborPoolConfig) -> str:
    return f"labor-{pool.name}"


def _working_intervals(config: CalendarConfig, horizon: float) -> tuple[Interval, ...]:
    """Working intervals of a labor calendar in [0, horizon), touching ones merged."""
    calendar = WorkingCalendar(config)
    intervals: list[Interval] = []
    t = 0.0
    while t < horizon:
        try:
            start = calendar.next_shift_start(t)
        except ValueError:
            break  # no working time within the calendar's search window
        if start >= horizon:
            break
        end = calendar.current_shift_end(start)
        if intervals and start <= intervals[-1][1]:
            intervals[-1] = (intervals[-1][0], end)
        else:
            intervals.append((start, end))
        t = end
    return tuple(intervals)


def _labor(
    pools: Sequence[LaborPoolConfig], horizon: float
) -> tuple[tuple[OperatorPool, ...], tuple[Calendar, ...]]:
    operator_pools = tuple(
        OperatorPool(
            id=pool.name,
            size=pool.headcount,
            calendar_id=_calendar_id(pool) if pool.calendar is not None else None,
        )
        for pool in pools
    )
    calendars = tuple(
        Calendar(id=_calendar_id(pool), intervals=_working_intervals(pool.calendar, horizon))
        for pool in pools
        if pool.calendar is not None
    )
    return operator_pools, calendars


def _job(index: int, order: Mapping[str, Any], stations: Sequence[_Station]) -> Job:
    job_id = f"order-{index}"
    quantity = float(order["quantity"])
    operations: list[Operation] = []
    previous: str | None = None
    for station in stations:
        op_id = f"{job_id}:stage-{station.location_id}"
        operations.append(station.operation(op_id, quantity, previous))
        previous = op_id
    return Job(
        id=job_id,
        release=float(order["release_second"]),
        due=float(order["due_second"]),
        operations=tuple(operations),
    )


def _problem(problem_id: str, model: CompiledModel, orders: Sequence[Mapping[str, Any]]) -> Problem:
    latest_release = max((float(order["release_second"]) for order in orders), default=0.0)
    operator_pools, calendars = _labor(model.labor_pools, latest_release + PLANNING_WINDOW_SECONDS)
    pool_by_skill = {skill: pool for pool in model.labor_pools for skill in pool.skills}
    stations = {
        spec.location_id: _station(spec, pool_by_skill[spec.labor_skill])
        for spec in model.locations
    }
    jobs = tuple(
        _job(index, order, [stations[step] for step in model.routing[order["product"]]])
        for index, order in enumerate(orders)
    )
    work_centers, machines, setup_functions = _shop(model)
    return Problem(
        id=problem_id,
        jobs=jobs,
        machines=machines,
        work_centers=work_centers,
        calendars=calendars,
        operator_pools=operator_pools,
        setup_functions=setup_functions,
    )
