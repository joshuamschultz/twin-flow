"""An in-process op whose crew is off shift at `as_of` is paused at now (TF-005 review ruling B).

Contract pinned here (the implementation follows these tests):

* Crew off shift = the op has a `CrewNeed` and its labor pool's calendar has no working time
  at `as_of` (working intervals are `[start, end)`: `as_of` on a window start is on shift, on
  a window end is off). A pool with no calendar is always on shift.
* Paused op: emitted PENDING (`OperationStatus()`, no in_progress status) with ONE option, its
  current or assigned (inferred) machine, with `fixed_seconds = qty_remaining x that
  machine's built unit_seconds` (no setup) and `unit_seconds = 0`; it keeps its crew
  `operator_needs` (phase per its shift crossing) and `ready_time = 0`.
* The request carries one hard `LockOverride(operation_id, machine_id, start=None)` per paused
  op, whose `fact_ref` or `reason` names the ERP paused state (contains "paused").
* Soft issue `in_progress_paused_offshift` with the op's lineage at
  `$.production_orders[i].operations[j]`.
* The paused op's machine is NOT blocked: `Machine.state.available_from == 0`.
* Crew ON shift at `as_of`: unchanged (in_progress on its machine, no operator needs, no lock,
  no issue). No TF-002 change.
* Scaled fixture: `MO-0154:0040` (inspect, `inspect-5`) at Monday 05:00 with inspectors from
  07:00 is paused; the plan solves verified with the baseline and paused ops resume on their
  locked machines no earlier than the crew window start.
"""

from __future__ import annotations

from typing import Any

import pytest
from _factories import lineage
from fixtures.generate import scaled_shop

from twinflow.erp.build import BuildResult, build_problem_from_erp
from twinflow.erp.digest import snapshot_digest
from twinflow.erp.issues import ErpIssue
from twinflow.erp.profile import PlanningProfile, load_planning_profile
from twinflow.erp.schema import ErpSnapshot
from twinflow.scheduling import (
    BaselineSolver,
    LockOverride,
    Operation,
    OperationStatus,
    SolveRequest,
    SolveResult,
    solve,
    validate,
)

AS_OF_0500 = "2026-10-12T05:00:00-05:00"  # Monday 05:00 CDT
CREW_START = 7_200.0  # 07:00 local, two hours after as_of
UNIT_SECONDS = 600.0
QTY_REMAINING = 6.0  # 10 to produce, 4 complete: 6 x 600 s = 3,600 s left

Data = dict[str, Any]


# --- snapshot builders --------------------------------------------------------------------------


def _window(start: int, end: int) -> Data:
    return {"start_minute": start, "end_minute": end}


def _calendar(calendar_id: str, window: Data, weekdays: range = range(5)) -> Data:
    return {
        "id": calendar_id,
        "week": [{"weekday": d, "windows": [window]} for d in weekdays],
        "exceptions": [],
        "lineage": lineage("calendar", calendar_id),
    }


DAY_SHIFT = _calendar("day", _window(420, 930))  # 07:00-15:30
# 22:00-06:00 next day; Sunday's window carries the crew into Monday 05:00 (as_of).
NIGHT_SHIFT = _calendar("night", _window(1320, 360), weekdays=range(7))


def _inspect_op(order_id: str, op_id: str, status: str, machine: str | None) -> Data:
    complete = 4.0 if status == "in_process" else 0.0
    return {
        "op_id": op_id,
        "sequence": int(op_id),
        "work_center_id": "inspect",
        "options": [
            {
                "work_center_id": "inspect",
                "machine_id": None,
                "setup_seconds": 300.0,
                "run_seconds_per_unit": UNIT_SECONDS,
                "fixed_run_seconds": 0.0,
                "preference": 0,
                "lineage": lineage("option", order_id, op_id),
            }
        ],
        "setup_params": {},
        "queue_seconds": 0.0,
        "move_seconds": 0.0,
        "status": status,
        "qty_to_produce": 10.0,
        "qty_complete": complete,
        "qty_scrapped": 0.0,
        "qty_remaining": 10.0 - complete,
        "current_machine_id": machine,
        "crew": {"labor_pool_id": "inspectors", "size": 1},
        "lineage": lineage("order_operation", order_id, op_id),
    }


def _order(order_id: str, *ops: Data) -> Data:
    return {
        "id": order_id,
        "order_type": "MO",
        "item_id": "SPR",
        "quantity": 10.0,
        "status": "in_process",
        "rush": False,
        "hold": False,
        "due_date": "2026-10-20",
        "sales_links": [],
        "operations": list(ops),
        "materials": [],
        "lineage": lineage("production_order", order_id),
    }


def _snapshot(
    orders: list[Data], *, pool_calendar: str | None = "day", as_of: str = AS_OF_0500
) -> ErpSnapshot:
    data: Data = {
        "schema_version": "twinflow.erp/1",
        "as_of": as_of,
        "plant_timezone": "America/Chicago",
        "source": {
            "system": "hand",
            "instance": "paused",
            "extractor": "test",
            "extractor_version": "1",
            "generation": None,
        },
        "source_issues": [],
        "items": [{"id": "SPR", "uom": "ea", "attributes": {}, "lineage": lineage("item", "SPR")}],
        "boms": [],
        "routings": [],
        # The machines run around the clock; only the inspectors keep shifts.
        "work_centers": [{"id": "inspect", "attributes": {}, "lineage": lineage("wc", "inspect")}],
        "machines": [
            {
                "id": machine_id,
                "work_center_id": "inspect",
                "efficiency": 1.0,
                "active": True,
                "lineage": lineage("machine", machine_id),
            }
            for machine_id in ("inspect-1", "inspect-2")
        ],
        "calendars": [DAY_SHIFT, NIGHT_SHIFT],
        "labor_pools": [
            {
                "id": "inspectors",
                "size": 1,
                "calendar_id": pool_calendar,
                "lineage": lineage("labor_pool", "inspectors"),
            }
        ],
        "production_orders": orders,
        "sales_order_lines": [],
        "customers": [],
        "vendors": [],
        "purchase_lines": [],
        "inventory": [],
    }
    snapshot = ErpSnapshot.model_validate(data)
    return snapshot.model_copy(update={"digest": snapshot_digest(snapshot)})


def _profile() -> PlanningProfile:
    return load_planning_profile("[erp]\n")


def _ready(result: BuildResult) -> SolveRequest:
    hard = sorted({(i.code, i.path) for i in result.issues if i.severity == "hard"})
    assert result.status == "ready", hard
    assert result.request is not None
    return result.request


def _ops(request: SolveRequest) -> dict[str, Operation]:
    return {op.id: op for job in request.problem.jobs for op in job.operations}


def _issues(result: BuildResult, code: str) -> list[ErpIssue]:
    return [issue for issue in result.issues if issue.code == code]


def _locks(request: SolveRequest) -> dict[str, LockOverride]:
    return {o.operation_id: o for o in request.overrides if isinstance(o, LockOverride)}


def _solved(request: SolveRequest) -> SolveResult:
    result = solve(request, solver=BaselineSolver(), budget_seconds=60.0)
    assert result.schedule.status in {"FEASIBLE", "OPTIMAL"}, result.issues[:5]
    assert result.verified is True, result.issues[:5]
    return result


def _assert_paused(
    request: SolveRequest, op_id: str, machine_id: str, remaining_seconds: float
) -> None:
    op = _ops(request)[op_id]
    assert op.status == OperationStatus()
    assert [o.machine_id for o in op.options] == [machine_id]
    (option,) = op.options
    assert option.fixed_seconds == pytest.approx(remaining_seconds)
    assert option.unit_seconds == 0.0
    assert op.ready_time == 0.0
    assert [(n.pool_id, n.quantity) for n in op.operator_needs] == [("inspectors", 1)]
    lock = _locks(request)[op_id]
    assert (lock.hardness, lock.machine_id, lock.start) == ("hard", machine_id, None)
    assert "paused" in f"{lock.fact_ref} {lock.reason}".lower()
    machine = next(m for m in request.problem.machines if m.id == machine_id)
    assert machine.state.available_from == 0.0


PAUSED_ORDER = _order(
    "MO-1",
    _inspect_op("MO-1", "10", "in_process", "inspect-2"),
    _inspect_op("MO-1", "20", "pending", None),
)


# --- small case ---------------------------------------------------------------------------------


def test_build_in_process_op_with_crew_off_shift_is_paused_on_its_machine() -> None:
    request = _ready(build_problem_from_erp(_snapshot([PAUSED_ORDER]), _profile()))
    _assert_paused(request, "MO-1:10", "inspect-2", QTY_REMAINING * UNIT_SECONDS)


def test_build_paused_op_keeps_its_crew_phase_from_the_shift_crossing_policy() -> None:
    request = _ready(build_problem_from_erp(_snapshot([PAUSED_ORDER]), _profile()))
    (need,) = _ops(request)["MO-1:10"].operator_needs
    assert need.phase == "both"  # the plain-op default `pause` holds the crew for setup + run


def test_build_paused_op_gets_one_soft_issue_with_its_lineage_and_path() -> None:
    snapshot = _snapshot([PAUSED_ORDER])
    result = build_problem_from_erp(snapshot, _profile())
    (found,) = _issues(result, "in_progress_paused_offshift")
    assert found.severity == "soft"
    assert found.path == "$.production_orders[0].operations[0]"
    assert found.lineage == snapshot.production_orders[0].operations[0].lineage
    assert _issues(result, "in_progress_machine_unknown") == []


def test_build_paused_op_resumes_on_its_machine_at_the_crew_window_start() -> None:
    request = _ready(build_problem_from_erp(_snapshot([PAUSED_ORDER]), _profile()))
    assert validate(request) == []
    solved = _solved(request)
    placed = {a.operation_id: a for a in solved.schedule.assignments}
    paused = placed["MO-1:10"]
    assert (paused.machine_id, paused.start) == ("inspect-2", CREW_START)
    assert paused.end == pytest.approx(CREW_START + QTY_REMAINING * UNIT_SECONDS)
    assert placed["MO-1:20"].start >= paused.end


def test_build_paused_op_on_an_inferred_machine_is_locked_to_that_machine() -> None:
    order = _order("MO-1", _inspect_op("MO-1", "10", "in_process", None))
    result = build_problem_from_erp(_snapshot([order]), _profile())
    request = _ready(result)
    # Inference gives the first active machine of the center option by id: inspect-1.
    _assert_paused(request, "MO-1:10", "inspect-1", QTY_REMAINING * UNIT_SECONDS)
    assert len(_issues(result, "in_process_machine_inferred")) == 1
    assert len(_issues(result, "in_progress_paused_offshift")) == 1


def test_build_two_paused_ops_share_one_inspector_without_infeasibility() -> None:
    first = _order("MO-1", _inspect_op("MO-1", "10", "in_process", "inspect-1"))
    second = _order("MO-2", _inspect_op("MO-2", "10", "in_process", "inspect-2"))
    request = _ready(build_problem_from_erp(_snapshot([first, second]), _profile()))
    assert set(_locks(request)) == {"MO-1:10", "MO-2:10"}
    assert len({lock.id for lock in request.overrides}) == len(request.overrides)
    placed = {a.operation_id: a for a in _solved(request).schedule.assignments}
    assert {placed["MO-1:10"].machine_id, placed["MO-2:10"].machine_id} == {
        "inspect-1",
        "inspect-2",
    }
    starts = sorted(placed[op].start for op in ("MO-1:10", "MO-2:10"))
    assert starts[0] == CREW_START
    assert starts[1] >= CREW_START + QTY_REMAINING * UNIT_SECONDS  # one inspector


@pytest.mark.parametrize(
    ("pool_calendar", "as_of"),
    [
        ("night", AS_OF_0500),  # 22:00-06:00 covers 05:00
        (None, AS_OF_0500),  # no calendar: always on shift
        ("day", "2026-10-12T07:00:00-05:00"),  # on the window start: working
    ],
    ids=["night-shift", "no-calendar", "window-start"],
)
def test_build_in_process_op_with_crew_on_shift_is_unchanged(
    pool_calendar: str | None, as_of: str
) -> None:
    snapshot = _snapshot([PAUSED_ORDER], pool_calendar=pool_calendar, as_of=as_of)
    result = build_problem_from_erp(snapshot, _profile())
    request = _ready(result)
    op = _ops(request)["MO-1:10"]
    assert (op.status.state, op.status.machine_id) == ("in_progress", "inspect-2")
    assert op.status.remaining_seconds == pytest.approx(QTY_REMAINING * UNIT_SECONDS)
    assert op.operator_needs == ()
    assert _locks(request) == {}
    assert _issues(result, "in_progress_paused_offshift") == []


def test_build_crew_at_the_window_end_is_off_shift() -> None:
    # 15:30 is the end of [07:00, 15:30): nobody is working at that instant.
    snapshot = _snapshot([PAUSED_ORDER], as_of="2026-10-12T15:30:00-05:00")
    result = build_problem_from_erp(snapshot, _profile())
    _assert_paused(_ready(result), "MO-1:10", "inspect-2", QTY_REMAINING * UNIT_SECONDS)


def test_build_in_process_op_without_crew_is_never_paused() -> None:
    op = _inspect_op("MO-1", "10", "in_process", "inspect-2") | {"crew": None}
    result = build_problem_from_erp(_snapshot([_order("MO-1", op)]), _profile())
    built = _ops(_ready(result))["MO-1:10"]
    assert built.status.state == "in_progress"
    assert _issues(result, "in_progress_paused_offshift") == []


# --- the scaled fixture -------------------------------------------------------------------------

SCALED_PROFILE = """
[erp.class_weights]
"A-KEY" = 4
"B-CORE" = 2
"C-SPOT" = 1
"""
REVIEW_OP = "MO-0154:0040"


@pytest.fixture(scope="module")
def scaled() -> tuple[ErpSnapshot, BuildResult]:
    snapshot = scaled_shop(orders=600)
    return snapshot, build_problem_from_erp(snapshot, load_planning_profile(SCALED_PROFILE))


def _crew_ops_in_process(snapshot: ErpSnapshot) -> dict[str, tuple[int, int]]:
    return {
        f"{order.id}:{op.op_id}": (i, j)
        for i, order in enumerate(snapshot.production_orders)
        for j, op in enumerate(order.operations)
        if op.status == "in_process" and op.crew is not None
    }


def test_scaled_shop_review_op_is_paused_on_inspect_5(
    scaled: tuple[ErpSnapshot, BuildResult],
) -> None:
    snapshot, result = scaled
    request = _ready(result)
    (i, j) = _crew_ops_in_process(snapshot)[REVIEW_OP]
    erp_op = snapshot.production_orders[i].operations[j]
    assert erp_op.current_machine_id == "inspect-5"
    unit = erp_op.options[0].run_seconds_per_unit  # efficiency 1.0, no setup in the remainder
    _assert_paused(request, REVIEW_OP, "inspect-5", erp_op.qty_remaining * unit)
    found = [
        x for x in _issues(result, "in_progress_paused_offshift") if x.lineage == erp_op.lineage
    ]
    assert [x.path for x in found] == [f"$.production_orders[{i}].operations[{j}]"]


def test_scaled_shop_every_off_shift_crew_op_is_paused_and_locked(
    scaled: tuple[ErpSnapshot, BuildResult],
) -> None:
    snapshot, result = scaled
    request = _ready(result)
    crew_ops = _crew_ops_in_process(snapshot)
    assert len(crew_ops) >= 1
    assert set(_locks(request)) == set(crew_ops)
    flagged = {x.path for x in _issues(result, "in_progress_paused_offshift")}
    assert flagged == {f"$.production_orders[{i}].operations[{j}]" for i, j in crew_ops.values()}
    ops = _ops(request)
    assert all(ops[op_id].status.state == "pending" for op_id in crew_ops)


def test_scaled_shop_with_paused_ops_solves_verified_and_resumes_at_the_crew_window(
    scaled: tuple[ErpSnapshot, BuildResult],
) -> None:
    snapshot, result = scaled
    request = _ready(result)
    assert validate(request) == []
    placed = {a.operation_id: a for a in _solved(request).schedule.assignments}
    locks = _locks(request)
    assert locks
    for op_id, lock in locks.items():
        assert placed[op_id].machine_id == lock.machine_id, op_id
        assert placed[op_id].start >= CREW_START, op_id
    assert min(placed[op_id].start for op_id in locks) == CREW_START
