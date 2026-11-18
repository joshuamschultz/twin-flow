"""Shop model built from the compiled v2 problem (T-171, COMP-134, REQ-127, D-085, D-107).

Contract pinned here (the implementation follows these tests):

    from twinflow.execution.shop import (
        DAY_SECONDS, ShopJob, ShopMachine, ShopModel, ShopOperation, ShopPool,
        build_shop, censor_time,
    )

    build_shop(compiled: CompiledProblem) -> ShopModel        # pure, deterministic, no I/O
    censor_time(shop: ShopModel, planned_makespan: float) -> float   # T_c (D-107)

Every type is a frozen dataclass (assignment raises `FrozenInstanceError`).

* `ShopMachine{id, work_center_id, calendar, setup_function, batch_capacity, restart, state}`:
  `calendar` is the machine calendar's intervals as a tuple of `(start, end)` tuples, or None
  (always working); `setup_function` is resolved machine first, then the machine's OWN work
  center (D-064), None when neither has one; `batch_capacity`, `restart`, `state` are the
  model values (`Machine.batch`, `Machine.restart`, `Machine.state`).
* `ShopPool{id, size, calendar}` (calendar as for machines).
* `ShopJob{id, weight, release, due}`: `weight` is the effective weight, `weight_override`
  when set, else `profile.class_weights[weight_class]`.
* `ShopOperation{id, job_id, work_center_id, quantity, options, predecessors, ready_time,
  setup_params, setup_family, batch_family, detached, operator_needs, shift_crossing, pin,
  status}`: the model values; `detached` is `Operation.setup_detached`; `work_center_id` is
  the operation's primary center (a cross-center option keeps its own center).
* `ShopModel{problem_id, now, horizon_end, machines, pools, jobs, operations, held_job_ids,
  done_operation_ids, calendar_end, setups}` plus the method
  `effective_calendar(machine_id, operation_id) -> Intervals`.
  - `machines`, `pools`, `jobs`, `operations` are mappings keyed by id.
  - `held_job_ids: frozenset[str]` = `compiled.held_job_ids` union jobs with status `held`.
  - `jobs` excludes held jobs. `operations` holds the PENDING operations: those of non-held
    jobs whose status is not `done` and whose job is not `done` (in-progress ops included).
  - `done_operation_ids: frozenset[str]` = ops with status `done` plus every op of a `done`
    job (for predecessor checks).
  - `setups` is a `scheduling.setups.SetupTable` for the compiled problem.
  - `effective_calendar` = machine calendar intersected (`calendar_math.intersect`, folded
    left in need order) with each needed pool's calendar, memoized by the calendar-id tuple
    `(machine.calendar_id, *pool calendar ids)`: equal id tuples return the SAME object.
  - `calendar_end: float | None` = the latest working-interval end over the calendars the
    shop uses (machine and pool calendars; an unused calendar does not count); None when
    the shop uses no calendar.
  - `setups` and the memo cache are excluded from equality (`field(compare=False)`), so two
    builds of one compiled problem compare equal.
* `censor_time(shop, planned_makespan)`: `shop.calendar_end` when it is not None, else
  `shop.now + max(10 * planned_makespan, 30 * DAY_SECONDS)` (D-107). `planned_makespan` is
  a duration in seconds (planned last end minus `now`); the shop is built without a plan, so
  the plan-dependent half of T_c lives in this helper. `DAY_SECONDS == 86_400.0`.
* The module imports neither `twinflow.model` nor `twinflow.scenario` (checked in a fresh
  interpreter) and opens no file or socket while building.
"""

from __future__ import annotations

import dataclasses
import pickle
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from twinflow.application.schedule_problem import build_request
from twinflow.erp.build import build_problem_from_erp
from twinflow.erp.profile import load_planning_profile
from twinflow.erp.schema import ErpSnapshot
from twinflow.execution.shop import (
    DAY_SECONDS,
    ShopModel,
    build_shop,
    censor_time,
)
from twinflow.scenario import ScenarioCapsule
from twinflow.scheduling import calendar_math
from twinflow.scheduling.calendar_math import Intervals
from twinflow.scheduling.model import (
    BatchCapacity,
    Calendar,
    CompiledProblem,
    HoldOverride,
    Job,
    Machine,
    MachineState,
    MismatchTerm,
    Operation,
    OperationStatus,
    OperatorNeed,
    OperatorPool,
    Problem,
    ProcessingOption,
    RestartRule,
    SchedulingProfile,
    SetupFunction,
    SolveRequest,
    WorkCenter,
)
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.setups import SetupTable, setup_seconds

ROOT = Path(__file__).resolve().parents[2]
SMALL_SHOP = ROOT / "tests" / "erp" / "fixtures" / "small_shop.json"
HMLV = ROOT / "examples" / "hmlv-calendar" / "hmlv-calendar.twin.yaml"
PROFILE_TOML = '[erp.class_weights]\n"A-KEY" = 4\n"B-CORE" = 2\n"C-SPOT" = 1\n'
THIRTY_DAYS = 30 * 86_400.0


# --- problem builders -------------------------------------------------------------------------


def _small_shop_request() -> SolveRequest:
    """The TF-005 ERP small shop (3 jobs held by status, 7 in progress, cross-center options)."""
    snapshot = ErpSnapshot.model_validate_json(SMALL_SHOP.read_text(encoding="utf-8"))
    built = build_problem_from_erp(snapshot, load_planning_profile(PROFILE_TOML))
    assert built.status == "ready", built.issues
    assert built.request is not None
    return built.request


def _scenario_request() -> SolveRequest:
    """The hmlv-calendar example scenario: calendar-less mills, a labor calendar, a WC setup."""
    capsule = ScenarioCapsule.load(HMLV)
    scenario = {"id": "scenario", "digest": capsule.digest, "capsule": capsule.to_dict()}
    orders = [
        {"order_id": oid, "product": "finished", "quantity": qty, "release_second": 0.0}
        | {"due_second": 86_400.0}
        for oid, qty in (("A", 2), ("B", 1))
    ]
    snapshot = {
        "id": "schedule-snapshot",
        "scenario_id": "scenario",
        "scenario_digest": capsule.digest,
        "orders": orders,
    }
    translation = build_request(scenario, snapshot)
    assert translation.status == "ready", translation.issues
    assert translation.request is not None
    return translation.request


def _option(machine_id: str, work_center_id: str | None = None) -> ProcessingOption:
    return ProcessingOption(
        machine_id=machine_id,
        fixed_seconds=10.0,
        unit_seconds=1.0,
        preference_cost=0.0,
        work_center_id=work_center_id,
    )


def _op(op_id: str, center: str, *options: ProcessingOption, **fields: Any) -> Operation:
    return Operation(id=op_id, work_center_id=center, quantity=5.0, options=options, **fields)


def _hand_problem(**changes: Any) -> Problem:
    """Two centers with their own setup functions, a cross-center substitute, a pool, calendars.

    * `grind` center function `f-grind`; machine `grind-1` has its own `f-own` (machine first).
    * `form` center function `f-form`; `form-1` has none (falls back to its own center), a
      restart rule, a batch capacity and a non-default state.
    * `op-x` (center `grind`) may run on `form-1` as a substitute (`work_center_id="form"`).
    * calendars: `day` on `grind-1`, `crew` on pool `crew`; `form-1` and `free-1` have none;
      `spare` is defined but used by nothing.
    """
    functions = (
        SetupFunction(id="f-own", terms=(MismatchTerm(param="tool", seconds=11.0),)),
        SetupFunction(id="f-grind", terms=(MismatchTerm(param="tool", seconds=22.0),)),
        SetupFunction(id="f-form", terms=(MismatchTerm(param="tool", seconds=33.0),)),
    )
    machines = (
        Machine(id="grind-1", work_center_id="grind", calendar_id="day", setup_function_id="f-own"),
        Machine(
            id="form-1",
            work_center_id="form",
            batch=BatchCapacity(capacity=4.0, unit="ea"),
            restart=RestartRule(idle_threshold_seconds=600.0, warmup_seconds=120.0),
            state=MachineState(available_from=50.0, last_setup_params={"tool": "b"}),
        ),
        Machine(id="free-1", work_center_id="free"),
    )
    centers = (
        WorkCenter(id="grind", setup_function_id="f-grind"),
        WorkCenter(id="form", setup_function_id="f-form"),
        WorkCenter(id="free"),
    )
    calendars = (
        Calendar(id="day", intervals=((0.0, 1_000.0), (2_000.0, 3_000.0))),
        Calendar(id="crew", intervals=((500.0, 2_500.0),)),
        Calendar(id="spare", intervals=((0.0, 9_000_000.0),)),
    )
    crew = (OperatorNeed(pool_id="crew", quantity=1, phase="both"),)
    jobs = (
        Job(
            id="job-a",
            weight_class=2,
            release=5.0,
            due=4_000.0,
            operations=(
                _op(
                    "op-x",
                    "grind",
                    _option("grind-1"),
                    _option("form-1", "form"),
                    setup_params={"tool": "a"},
                    operator_needs=crew,
                ),
                _op(
                    "op-y",
                    "grind",
                    _option("grind-1"),
                    setup_params={"tool": "b"},
                    operator_needs=crew,
                ),
            ),
        ),
        Job(
            id="job-b",
            weight_class=4,
            weight_override=7.5,
            operations=(_op("op-z", "free", _option("free-1")),),
        ),
    )
    problem = Problem(
        id="hand",
        now=100.0,
        jobs=jobs,
        machines=machines,
        work_centers=centers,
        calendars=calendars,
        operator_pools=(OperatorPool(id="crew", size=1, calendar_id="crew"),),
        setup_functions=functions,
    )
    return problem.model_copy(update=changes)


def _compiled_hand(**changes: Any) -> CompiledProblem:
    """The hand problem compiled directly (no `apply_overrides`): weights stay un-resolved."""
    profile = SchedulingProfile(class_weights={1: 1.0, 2: 3.0, 4: 9.0})
    return CompiledProblem(problem=_hand_problem(**changes), profile=profile)


def _no_calendar_compiled() -> CompiledProblem:
    """The hand problem with every calendar removed from machines and pools."""
    problem = _hand_problem()
    machines = tuple(m.model_copy(update={"calendar_id": None}) for m in problem.machines)
    pools = tuple(p.model_copy(update={"calendar_id": None}) for p in problem.operator_pools)
    stripped = problem.model_copy(update={"machines": machines, "operator_pools": pools})
    return CompiledProblem(problem=stripped, profile=SchedulingProfile())


# --- oracles (independent of the implementation) -----------------------------------------------


def _intervals(calendar_id: str | None, problem: Problem) -> tuple[tuple[float, float], ...] | None:
    if calendar_id is None:
        return None
    calendars = {c.id: c for c in problem.calendars}
    return tuple(calendars[calendar_id].intervals)


def _normalized(intervals: Intervals) -> tuple[tuple[float, float], ...] | None:
    return None if intervals is None else tuple((lo, hi) for lo, hi in intervals)


def _fold(problem: Problem, machine_id: str, op: Operation) -> Intervals:
    machine = {m.id: m for m in problem.machines}[machine_id]
    pools = {p.id: p for p in problem.operator_pools}
    result: Intervals = _intervals(machine.calendar_id, problem)
    for need in op.operator_needs:
        result = calendar_math.intersect(
            result, _intervals(pools[need.pool_id].calendar_id, problem)
        )
    return result


def _expected_function(problem: Problem, machine: Machine) -> SetupFunction | None:
    centers = {c.id: c for c in problem.work_centers}
    function_id = machine.setup_function_id or centers[machine.work_center_id].setup_function_id
    return {f.id: f for f in problem.setup_functions}[function_id] if function_id else None


def _status_held(problem: Problem) -> set[str]:
    return {job.id for job in problem.jobs if job.status == "held"}


def _pending_ops(problem: Problem, held: set[str]) -> dict[str, tuple[Job, Operation]]:
    return {
        op.id: (job, op)
        for job in problem.jobs
        if job.id not in held and job.status != "done"
        for op in job.operations
        if op.status.state != "done"
    }


def _used_calendar_ends(problem: Problem) -> list[float]:
    used = {m.calendar_id for m in problem.machines} | {
        p.calendar_id for p in problem.operator_pools
    }
    return [c.intervals[-1][1] for c in problem.calendars if c.id in used and c.intervals]


# --- fixtures -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def small_compiled() -> CompiledProblem:
    return apply_overrides(_small_shop_request())


@pytest.fixture(scope="module")
def scenario_compiled() -> CompiledProblem:
    return apply_overrides(_scenario_request())


@pytest.fixture(params=["small_shop", "scenario"])
def compiled(request: pytest.FixtureRequest) -> CompiledProblem:
    name = "small_compiled" if request.param == "small_shop" else "scenario_compiled"
    value: CompiledProblem = request.getfixturevalue(name)
    return value


def _ops_of(compiled: CompiledProblem) -> Iterator[Operation]:
    return (op for job in compiled.problem.jobs for op in job.operations)


# --- machines, pools, setups --------------------------------------------------------------------


def test_build_shop_machines_carry_center_calendar_setup_batch_restart_state(
    compiled: CompiledProblem,
) -> None:
    problem = compiled.problem

    shop = build_shop(compiled)

    assert set(shop.machines) == {m.id for m in problem.machines}
    for machine in problem.machines:
        built = shop.machines[machine.id]
        assert built.id == machine.id
        assert built.work_center_id == machine.work_center_id
        assert built.calendar == _intervals(machine.calendar_id, problem)
        assert built.setup_function == _expected_function(problem, machine)
        assert built.batch_capacity == machine.batch
        assert built.restart == machine.restart
        assert built.state == machine.state


def test_build_shop_setup_function_resolves_machine_first_then_own_work_center() -> None:
    shop = build_shop(_compiled_hand())

    assert shop.machines["grind-1"].setup_function is not None
    assert shop.machines["grind-1"].setup_function.id == "f-own"
    assert shop.machines["form-1"].setup_function is not None
    assert shop.machines["form-1"].setup_function.id == "f-form"
    assert shop.machines["free-1"].setup_function is None


def test_build_shop_machine_restart_batch_and_state_kept_from_model() -> None:
    shop = build_shop(_compiled_hand())

    form = shop.machines["form-1"]
    assert form.restart == RestartRule(idle_threshold_seconds=600.0, warmup_seconds=120.0)
    assert form.batch_capacity == BatchCapacity(capacity=4.0, unit="ea")
    assert form.state.available_from == 50.0
    assert form.state.last_setup_params == {"tool": "b"}
    assert shop.machines["grind-1"].restart is None
    assert shop.machines["grind-1"].batch_capacity is None


def test_build_shop_setups_is_a_setup_table_matching_setup_seconds() -> None:
    compiled = _compiled_hand()
    ops = {op.id: op for op in _ops_of(compiled)}
    state = compiled.problem.machines[1].state

    shop = build_shop(compiled)

    assert isinstance(shop.setups, SetupTable)
    expected = setup_seconds(shop.machines["form-1"].setup_function, state, ops["op-x"])
    assert expected == 33.0
    assert shop.setups.seconds("form-1", None, "op-x") == expected
    assert shop.setups.seconds("grind-1", "op-y", "op-x") == 11.0


def test_build_shop_pools_carry_size_and_calendar(compiled: CompiledProblem) -> None:
    problem = compiled.problem

    shop = build_shop(compiled)

    assert set(shop.pools) == {p.id for p in problem.operator_pools}
    assert shop.pools, "both fixtures have an operator pool"
    for pool in problem.operator_pools:
        built = shop.pools[pool.id]
        assert (built.id, built.size) == (pool.id, pool.size)
        assert built.calendar == _intervals(pool.calendar_id, problem)


# --- former guard: no machine shared across locations ------------------------------------------


def test_build_shop_cross_center_substitute_resolves_to_its_own_centers_machine() -> None:
    compiled = _compiled_hand()

    shop = build_shop(compiled)

    op = shop.operations["op-x"]
    substitute = next(o for o in op.options if o.work_center_id is not None)
    machine = shop.machines[substitute.machine_id]
    assert machine.id == "form-1"
    assert machine.work_center_id == "form" == substitute.work_center_id
    assert op.work_center_id == "grind"
    assert machine.setup_function is not None
    assert machine.setup_function.id == "f-form"
    assert list(shop.machines).count("form-1") == 1


def test_build_shop_small_shop_cross_center_options_keep_machine_centers(
    small_compiled: CompiledProblem,
) -> None:
    substitutes = [
        (op, option)
        for op in _ops_of(small_compiled)
        for option in op.options
        if option.work_center_id is not None and option.work_center_id != op.work_center_id
    ]
    assert substitutes, "the small shop has cross-center substitute options"

    shop = build_shop(small_compiled)

    for op, option in substitutes:
        assert shop.machines[option.machine_id].work_center_id == option.work_center_id
        if op.id in shop.operations:
            assert shop.operations[op.id].work_center_id == op.work_center_id


# --- jobs and weights ---------------------------------------------------------------------------


def test_build_shop_job_weight_is_override_else_class_weight() -> None:
    shop = build_shop(_compiled_hand())

    assert shop.jobs["job-a"].weight == 3.0
    assert shop.jobs["job-b"].weight == 7.5


def test_build_shop_jobs_carry_release_and_due(compiled: CompiledProblem) -> None:
    held = _status_held(compiled.problem) | set(compiled.held_job_ids)
    weights = compiled.profile.class_weights

    shop = build_shop(compiled)

    expected = {job.id: job for job in compiled.problem.jobs if job.id not in held}
    assert set(shop.jobs) == set(expected)
    for job_id, job in expected.items():
        built = shop.jobs[job_id]
        assert built.id == job_id
        assert built.release == job.release
        assert built.due == job.due
        assert built.weight == (job.weight_override or weights[job.weight_class])


def test_build_shop_priority_override_weight_reaches_the_job(
    small_compiled: CompiledProblem,
) -> None:
    request = _small_shop_request()
    target = next(job for job in request.problem.jobs if job.status == "open")
    priority = {
        "kind": "priority",
        "id": "ov-w",
        "fact_ref": "planner",
        "reason": "test",
        "hardness": "hard",
        "job_id": target.id,
        "weight": 5.5,
    }
    with_priority = SolveRequest.model_validate(
        request.model_dump(mode="json") | {"overrides": [priority]}
    )

    shop = build_shop(apply_overrides(with_priority))

    assert shop.jobs[target.id].weight == 5.5
    assert shop.jobs[target.id].weight != build_shop(small_compiled).jobs[target.id].weight


# --- held jobs ----------------------------------------------------------------------------------


def _hold(job_id: str, until: float | None) -> HoldOverride:
    return HoldOverride(
        id=f"hold-{job_id}",
        fact_ref="planner",
        reason="test",
        hardness="hard",
        job_id=job_id,
        until=until,
    )


def test_build_shop_held_jobs_union_compiled_holds_and_status_held() -> None:
    request = _small_shop_request()
    status_held = _status_held(request.problem)
    assert len(status_held) == 3
    target = next(job for job in request.problem.jobs if job.status == "open")
    compiled = apply_overrides(request.model_copy(update={"overrides": (_hold(target.id, None),)}))
    assert compiled.held_job_ids == (target.id,)

    shop = build_shop(compiled)

    assert shop.held_job_ids == frozenset(status_held | {target.id})
    held_ops = {
        op.id
        for job in request.problem.jobs
        if job.id in shop.held_job_ids
        for op in job.operations
    }
    assert held_ops
    assert not held_ops & set(shop.operations)
    assert not held_ops & shop.done_operation_ids
    assert not shop.held_job_ids & set(shop.jobs)


def test_build_shop_hold_with_until_keeps_job_with_later_release() -> None:
    request = _small_shop_request()
    target = next(job for job in request.problem.jobs if job.status == "open")
    compiled = apply_overrides(
        request.model_copy(update={"overrides": (_hold(target.id, 123_456.0),)})
    )

    shop = build_shop(compiled)

    assert target.id not in shop.held_job_ids
    assert shop.jobs[target.id].release == max(target.release, 123_456.0)
    assert {op.id for op in target.operations} <= set(shop.operations)


# --- done operations ----------------------------------------------------------------------------


def _with_done(request: SolveRequest) -> tuple[SolveRequest, str, str]:
    """Mark the first pending op of one open job done and a second open job done."""
    open_jobs = [job for job in request.problem.jobs if job.status == "open"]
    first, second = open_jobs[0], open_jobs[1]
    done_op = first.operations[0]
    marked = done_op.model_copy(update={"status": OperationStatus(state="done")})
    first_new = first.model_copy(update={"operations": (marked, *first.operations[1:])})
    second_new = second.model_copy(update={"status": "done"})
    replace = {first.id: first_new, second.id: second_new}
    jobs = tuple(replace.get(job.id, job) for job in request.problem.jobs)
    problem = request.problem.model_copy(update={"jobs": jobs})
    return request.model_copy(update={"problem": problem}), done_op.id, second.id


def test_build_shop_done_ops_in_done_set_and_absent_from_pending() -> None:
    request, done_op_id, done_job_id = _with_done(_small_shop_request())
    compiled = apply_overrides(request)
    done_job = next(job for job in compiled.problem.jobs if job.id == done_job_id)
    expected_done = {done_op_id} | {op.id for op in done_job.operations}

    shop = build_shop(compiled)

    assert shop.done_operation_ids == frozenset(expected_done)
    assert not expected_done & set(shop.operations)


def test_build_shop_without_done_ops_has_empty_done_set(small_compiled: CompiledProblem) -> None:
    shop = build_shop(small_compiled)

    assert shop.done_operation_ids == frozenset()


# --- operations ---------------------------------------------------------------------------------


def test_build_shop_operations_are_pending_ops_with_every_field(compiled: CompiledProblem) -> None:
    held = _status_held(compiled.problem) | set(compiled.held_job_ids)
    expected = _pending_ops(compiled.problem, held)

    shop = build_shop(compiled)

    assert set(shop.operations) == set(expected)
    for op_id, (job, op) in expected.items():
        built = shop.operations[op_id]
        assert built.id == op.id
        assert built.job_id == job.id
        assert built.work_center_id == op.work_center_id
        assert built.quantity == op.quantity
        assert tuple(built.options) == op.options
        assert tuple(built.predecessors) == op.predecessors
        assert built.ready_time == op.ready_time
        assert dict(built.setup_params) == op.setup_params
        assert built.setup_family == op.setup_family
        assert built.batch_family == op.batch_family
        assert built.detached == op.setup_detached
        assert tuple(built.operator_needs) == op.operator_needs
        assert built.shift_crossing == op.shift_crossing
        assert built.pin == op.pin
        assert built.status == op.status


def test_build_shop_in_progress_ops_stay_pending_with_status(
    small_compiled: CompiledProblem,
) -> None:
    held = _status_held(small_compiled.problem)
    in_progress = {
        op.id: op
        for job in small_compiled.problem.jobs
        if job.id not in held
        for op in job.operations
        if op.status.state == "in_progress"
    }
    assert in_progress

    shop = build_shop(small_compiled)

    for op_id, op in in_progress.items():
        assert shop.operations[op_id].status == op.status


# --- effective calendars ------------------------------------------------------------------------


def test_build_shop_effective_calendar_equals_intersect_fold(compiled: CompiledProblem) -> None:
    shop = build_shop(compiled)

    for op_id in shop.operations:
        op = next(o for o in _ops_of(compiled) if o.id == op_id)
        for option in op.options:
            actual = shop.effective_calendar(option.machine_id, op_id)
            expected = _fold(compiled.problem, option.machine_id, op)
            assert _normalized(actual) == _normalized(expected), (option.machine_id, op_id)


def test_build_shop_effective_calendar_memoized_by_calendar_id_tuple() -> None:
    shop = build_shop(_compiled_hand())

    first = shop.effective_calendar("grind-1", "op-x")
    second = shop.effective_calendar("grind-1", "op-y")

    assert first is second
    assert _normalized(first) == ((500.0, 1_000.0), (2_000.0, 2_500.0))


def test_build_shop_effective_calendar_shared_across_machines_with_same_ids(
    small_compiled: CompiledProblem,
) -> None:
    shop = build_shop(small_compiled)
    plain = [
        (op.id, option.machine_id)
        for op in shop.operations.values()
        if not op.operator_needs
        for option in op.options
        if option.machine_id in ("grind-1", "grind-2")
    ]
    by_machine = {machine: op_id for op_id, machine in plain}
    assert set(by_machine) == {"grind-1", "grind-2"}

    one = shop.effective_calendar("grind-1", by_machine["grind-1"])
    two = shop.effective_calendar("grind-2", by_machine["grind-2"])

    assert one is two


def test_build_shop_effective_calendar_none_without_any_calendar() -> None:
    shop = build_shop(_compiled_hand())

    assert shop.effective_calendar("free-1", "op-z") is None


def test_build_shop_effective_calendar_pool_only_for_calendar_less_machine() -> None:
    shop = build_shop(_compiled_hand())

    assert _normalized(shop.effective_calendar("form-1", "op-x")) == ((500.0, 2_500.0),)


# --- censoring time T_c (D-107) -----------------------------------------------------------------


def test_build_shop_calendar_end_is_latest_used_working_end(compiled: CompiledProblem) -> None:
    expected = max(_used_calendar_ends(compiled.problem))

    shop = build_shop(compiled)

    assert shop.calendar_end == expected
    assert censor_time(shop, planned_makespan=1.0) == expected
    assert censor_time(shop, planned_makespan=1e12) == expected


def test_build_shop_calendar_end_ignores_unused_calendar() -> None:
    shop = build_shop(_compiled_hand())

    assert shop.calendar_end == 3_000.0


def test_censor_time_without_calendar_uses_thirty_day_floor() -> None:
    shop = build_shop(_no_calendar_compiled())

    assert shop.calendar_end is None
    assert DAY_SECONDS == 86_400.0
    assert censor_time(shop, planned_makespan=1_000.0) == 100.0 + THIRTY_DAYS


def test_censor_time_without_calendar_uses_ten_times_planned_makespan() -> None:
    shop = build_shop(_no_calendar_compiled())
    makespan = 400_000.0

    assert 10 * makespan > THIRTY_DAYS
    assert censor_time(shop, planned_makespan=makespan) == 100.0 + 10 * makespan


# --- determinism, immutability, purity ----------------------------------------------------------


def test_build_shop_two_builds_are_equal(compiled: CompiledProblem) -> None:
    first = build_shop(compiled)
    second = build_shop(compiled)

    assert isinstance(first, ShopModel)
    assert first == second
    assert list(first.operations) == list(second.operations)
    assert list(first.machines) == list(second.machines)


def test_build_shop_model_is_frozen(small_compiled: CompiledProblem) -> None:
    shop = build_shop(small_compiled)

    with pytest.raises(dataclasses.FrozenInstanceError):
        shop.now = 1.0  # type: ignore[misc]  # asserting the frozen dataclass refuses writes
    with pytest.raises(dataclasses.FrozenInstanceError):
        next(iter(shop.machines.values())).work_center_id = "x"  # type: ignore[misc]  # same


def test_build_shop_does_no_file_or_socket_io(
    compiled: CompiledProblem, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"I/O attempted: {args!r}")

    monkeypatch.setattr("builtins.open", refuse)
    monkeypatch.setattr("io.open", refuse)
    monkeypatch.setattr(socket, "socket", refuse)

    shop = build_shop(compiled)

    assert shop.problem_id == compiled.problem.id
    assert shop.now == compiled.problem.now
    assert shop.horizon_end == compiled.problem.horizon_end


def test_shop_module_imports_neither_model_nor_scenario() -> None:
    probe = (
        "import sys, twinflow.execution.shop\n"
        "bad = sorted(m for m in sys.modules\n"
        "             if m.split('.')[:2] in (['twinflow', 'model'], ['twinflow', 'scenario']))\n"
        "print(','.join(bad))\n"
    )

    result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a literal probe
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


# --- mixed shops: some options always working (orchestrator ruling on D-107) ------------------


def test_censor_time_mixed_shop_uses_floor_when_an_option_always_works() -> None:
    shop = build_shop(_compiled_hand())

    assert shop.calendar_end == 3_000.0
    assert shop.effective_calendar("free-1", "op-z") is None
    assert censor_time(shop, planned_makespan=1_000.0) == 100.0 + THIRTY_DAYS
    assert censor_time(shop, planned_makespan=400_000.0) == 100.0 + 4_000_000.0


def test_censor_time_mixed_shop_never_earlier_than_calendar_end() -> None:
    long_day = Calendar(id="day", intervals=((0.0, 1_000.0), (2_000.0, 9_000_000.0)))
    compiled = _compiled_hand(
        calendars=(long_day, Calendar(id="crew", intervals=((500.0, 2_500.0),)))
    )

    shop = build_shop(compiled)

    assert censor_time(shop, planned_makespan=1_000.0) == 9_000_000.0


def test_censor_time_calendar_less_machine_bounded_by_pool_calendar() -> None:
    compiled = _compiled_hand()
    jobs = tuple(job for job in compiled.problem.jobs if job.id != "job-b")
    shop = build_shop(_compiled_hand(jobs=jobs))

    assert shop.machines["form-1"].calendar is None
    assert shop.effective_calendar("form-1", "op-x") is not None
    assert censor_time(shop, planned_makespan=1e12) == 3_000.0


# --- index maps and pickling --------------------------------------------------------------------


def test_build_shop_successors_and_job_operations_index_pending_ops(
    small_compiled: CompiledProblem,
) -> None:
    shop = build_shop(small_compiled)
    expected_successors: dict[str, list[str]] = {op_id: [] for op_id in shop.operations}
    expected_by_job: dict[str, list[str]] = {}
    for op in shop.operations.values():
        expected_by_job.setdefault(op.job_id, []).append(op.id)
        for link in op.predecessors:
            if link.operation_id in shop.operations:
                expected_successors[link.operation_id].append(op.id)
    assert any(expected_successors.values()), "the small shop has precedence links"

    assert {k: list(v) for k, v in shop.successors.items()} == expected_successors
    assert {k: list(v) for k, v in shop.job_operations.items()} == expected_by_job


def test_build_shop_model_round_trips_through_pickle(compiled: CompiledProblem) -> None:
    shop = build_shop(compiled)
    for op in shop.operations.values():
        shop.effective_calendar(op.options[0].machine_id, op.id)

    restored = pickle.loads(pickle.dumps(shop))  # noqa: S301 - our own bytes, same process

    assert restored == shop
    assert censor_time(restored, 1_000.0) == censor_time(shop, 1_000.0)
