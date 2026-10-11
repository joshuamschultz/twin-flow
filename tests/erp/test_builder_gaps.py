"""Builder gaps found by Western Spring's 600-order snapshot (TF-005, orchestrator ruling).

Three gaps, one focused group each, on small hand-made snapshots, plus the scaled shop end
to end.

1. Cross-center alternates. An ERP option may name its own `work_center_id` (a substitute
   center). The builder emits that machine and sets `ProcessingOption.work_center_id` to the
   option's center ONLY when it differs from the op's center (else None); the op keeps its
   own center. The ERP validator checks a machine-level option's machine against the
   OPTION's center (`machine_outside_work_center` otherwise).
2. In-process ops without `current_machine_id`. Explicit machines claim first. Then the
   ops that need inference, in order (order id, op sequence), each take their most
   preferred active option machine that no other in-process op holds (soft
   `in_process_machine_inferred`). When none is free the op becomes pending from now
   (default `OperationStatus()`, quantity = qty_remaining, normal options) with soft
   `in_progress_machine_unknown` at the op path with the op's lineage, and no
   `in_process_machine_inferred`. Never two in-progress ops on one machine.
3. Shift crossing. `ErpPolicy.shift_crossing_default = "pause"` and
   `ErpPolicy.batch_shift_crossing = "finish_unattended"` (batch op = op with a
   `BatchSpec`, whether or not its lot fits the capacity), both settable in `[erp]`.

Pinned choices beyond the ruling text are marked PINNED.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

import pytest
from _factories import lineage
from fixtures.generate import scaled_shop

from twinflow.erp.build import (
    BuildResult,
    RushOrder,
    build_problem_from_erp,
    build_rush_job,
)
from twinflow.erp.digest import snapshot_digest
from twinflow.erp.issues import ErpIssue
from twinflow.erp.profile import ErpPolicy, PlanningProfile, ProfileError, load_planning_profile
from twinflow.erp.schema import ErpSnapshot
from twinflow.erp.validate import validate_snapshot
from twinflow.scheduling import (
    BaselineSolver,
    Job,
    Operation,
    OperationStatus,
    SolveRequest,
    solve,
    validate,
)

AS_OF = "2026-10-12T05:00:00-05:00"
DUE = "2026-10-30"

Data = dict[str, Any]


# --- snapshot builders --------------------------------------------------------------------------


def _option(center: str, machine: str | None, preference: int, **fields: Any) -> Data:
    return {
        "work_center_id": center,
        "machine_id": machine,
        "setup_seconds": 0.0,
        "run_seconds_per_unit": 1.0,
        "fixed_run_seconds": 0.0,
        "preference": preference,
        "lineage": lineage("option", center, machine or center, str(preference)),
    } | fields


def _op(
    order_id: str,
    op_id: str,
    center: str,
    options: list[Data],
    *,
    status: str = "pending",
    machine: str | None = None,
    **fields: Any,
) -> Data:
    complete = 4.0 if status == "in_process" else 0.0
    return {
        "op_id": op_id,
        "sequence": int(op_id),
        "work_center_id": center,
        "options": options,
        "setup_params": {},
        "queue_seconds": 0.0,
        "move_seconds": 0.0,
        "status": status,
        "qty_to_produce": 10.0,
        "qty_complete": complete,
        "qty_scrapped": 0.0,
        "qty_remaining": 10.0 - complete,
        "current_machine_id": machine,
        "lineage": lineage("order_operation", order_id, op_id),
    } | fields


def _order(order_id: str, *ops: Data, status: str = "released") -> Data:
    return {
        "id": order_id,
        "order_type": "MO",
        "item_id": "SPR",
        "quantity": 10.0,
        "status": status,
        "rush": False,
        "hold": False,
        "due_date": DUE,
        "sales_links": [],
        "operations": list(ops),
        "materials": [],
        "lineage": lineage("production_order", order_id),
    }


def _machine(machine_id: str, center: str, active: bool = True) -> Data:
    return {
        "id": machine_id,
        "work_center_id": center,
        "efficiency": 1.0,
        "active": active,
        "lineage": lineage("machine", machine_id),
    }


def _snapshot(orders: list[Data], machines: list[Data]) -> ErpSnapshot:
    centers = sorted({m["work_center_id"] for m in machines})
    data: Data = {
        "schema_version": "twinflow.erp/1",
        "as_of": AS_OF,
        "plant_timezone": "America/Chicago",
        "source": {
            "system": "hand",
            "instance": "gaps",
            "extractor": "test",
            "extractor_version": "1",
            "generation": None,
        },
        "source_issues": [],
        "items": [{"id": "SPR", "uom": "ea", "attributes": {}, "lineage": lineage("item", "SPR")}],
        "boms": [],
        "routings": [],
        "work_centers": [
            {"id": c, "attributes": {}, "lineage": lineage("work_center", c)} for c in centers
        ],
        "machines": machines,
        "calendars": [],
        "labor_pools": [],
        "production_orders": orders,
        "sales_order_lines": [],
        "customers": [],
        "vendors": [],
        "purchase_lines": [],
        "inventory": [],
    }
    snapshot = ErpSnapshot.model_validate(data)
    return snapshot.model_copy(update={"digest": snapshot_digest(snapshot)})


def _profile(erp_toml: str = "") -> PlanningProfile:
    return load_planning_profile(f"[erp]\n{erp_toml}")


def _ready(result: BuildResult) -> SolveRequest:
    hard = sorted({(i.code, i.path) for i in result.issues if i.severity == "hard"})
    assert result.status == "ready", hard
    assert result.request is not None
    return result.request


def _ops(request: SolveRequest) -> dict[str, Operation]:
    return {op.id: op for job in request.problem.jobs for op in job.operations}


def _issues(result: BuildResult, code: str) -> list[ErpIssue]:
    return [issue for issue in result.issues if issue.code == code]


def _running(request: SolveRequest) -> dict[str, str | None]:
    return {
        op.id: op.status.machine_id
        for op in _ops(request).values()
        if op.status.state == "in_progress"
    }


def _assert_one_in_progress_per_machine(request: SolveRequest) -> None:
    machines = [m for m in _running(request).values()]
    assert None not in machines
    assert {m: n for m, n in Counter(machines).items() if n > 1} == {}


# --- gap 1: cross-center alternates -------------------------------------------------------------

GRIND_MACHINES = [_machine("grind-1", "grind"), _machine("form-1", "form")]


def _substitute_order(**form_option: Any) -> Data:
    return _order(
        "MO-1",
        _op(
            "MO-1",
            "10",
            "grind",
            [_option("grind", "grind-1", 0), _option("form", "form-1", 1, **form_option)],
        ),
    )


def test_erp_validate_accepts_a_machine_inside_its_option_substitute_center() -> None:
    report = validate_snapshot(_snapshot([_substitute_order()], GRIND_MACHINES), _profile())
    assert [(i.code, i.path) for i in report.issues if i.severity == "hard"] == []


def test_erp_validate_rejects_a_machine_outside_its_option_center() -> None:
    # The option claims form, but the machine is a grind machine.
    order = _order(
        "MO-1",
        _op("MO-1", "10", "grind", [_option("grind", "grind-1", 0), _option("form", "grind-1", 1)]),
    )
    report = validate_snapshot(_snapshot([order], GRIND_MACHINES), _profile())
    path = "$.production_orders[0].operations[0].options[1].machine_id"
    assert [(i.code, i.path) for i in report.issues if i.severity == "hard"] == [
        ("machine_outside_work_center", path)
    ]


def test_build_substitute_center_option_sets_option_work_center() -> None:
    request = _ready(
        build_problem_from_erp(_snapshot([_substitute_order()], GRIND_MACHINES), _profile())
    )
    op = _ops(request)["MO-1:10"]
    assert op.work_center_id == "grind"
    by_machine = {option.machine_id: option for option in op.options}
    assert set(by_machine) == {"grind-1", "form-1"}
    assert by_machine["form-1"].work_center_id == "form"
    assert by_machine["grind-1"].work_center_id is None  # same center as the op: left None


def test_build_substitute_center_work_center_level_option_spans_that_center() -> None:
    # A center-level ERP option on the substitute center reaches every active form machine.
    order = _order(
        "MO-1",
        _op("MO-1", "10", "grind", [_option("grind", "grind-1", 0), _option("form", None, 1)]),
    )
    machines = [*GRIND_MACHINES, _machine("form-2", "form")]
    op = _ops(_ready(build_problem_from_erp(_snapshot([order], machines), _profile())))["MO-1:10"]
    centers = {option.machine_id: option.work_center_id for option in op.options}
    assert centers == {"grind-1": None, "form-1": "form", "form-2": "form"}


def test_build_substitute_center_request_passes_tf002_validate_and_solves_verified() -> None:
    request = _ready(
        build_problem_from_erp(_snapshot([_substitute_order()], GRIND_MACHINES), _profile())
    )
    assert validate(request) == []
    result = solve(request, solver=BaselineSolver())
    assert result.verified is True, result.issues


def test_build_substitute_center_machine_is_chosen_when_it_finishes_first() -> None:
    # grind-1 runs 10 s/unit, the substitute 1 s/unit: the baseline picks form-1.
    order = _order(
        "MO-1",
        _op(
            "MO-1",
            "10",
            "grind",
            [
                _option("grind", "grind-1", 0, run_seconds_per_unit=10.0),
                _option("form", "form-1", 1),
            ],
        ),
    )
    request = _ready(build_problem_from_erp(_snapshot([order], GRIND_MACHINES), _profile()))
    result = solve(request, solver=BaselineSolver())
    assert result.verified is True, result.issues
    (assignment,) = result.schedule.assignments
    assert assignment.machine_id == "form-1"


def test_build_in_process_op_on_a_substitute_machine_runs_there() -> None:
    options = [_option("grind", "grind-1", 0), _option("form", "form-1", 1)]
    order = _order(
        "MO-1",
        _op("MO-1", "10", "grind", options, status="in_process", machine="form-1"),
        status="in_process",
    )
    request = _ready(build_problem_from_erp(_snapshot([order], GRIND_MACHINES), _profile()))
    status = _ops(request)["MO-1:10"].status
    assert (status.state, status.machine_id) == ("in_progress", "form-1")
    assert validate(request) == []


# --- gap 2: in-process ops without a current machine --------------------------------------------

FORM_MACHINES = [_machine("form-1", "form"), _machine("form-2", "form"), _machine("form-3", "form")]
RANKED = [_option("form", "form-1", 0), _option("form", "form-2", 1), _option("form", "form-3", 2)]


def _running_op(
    order_id: str, machine: str | None = None, options: list[Data] | None = None
) -> Data:
    return _order(
        order_id,
        _op(order_id, "10", "form", options or RANKED, status="in_process", machine=machine),
        status="in_process",
    )


def test_build_three_inferred_ops_take_distinct_machines_in_preference_order() -> None:
    # Document order is shuffled; the assignment order is by order id (PINNED).
    orders = [_running_op("MO-2"), _running_op("MO-3"), _running_op("MO-1")]
    result = build_problem_from_erp(_snapshot(orders, FORM_MACHINES), _profile())
    request = _ready(result)
    assert _running(request) == {"MO-1:10": "form-1", "MO-2:10": "form-2", "MO-3:10": "form-3"}
    assert validate(request) == []
    inferred = _issues(result, "in_process_machine_inferred")
    assert sorted(i.lineage.key for i in inferred if i.lineage) == [
        ("MO-1", "10"),
        ("MO-2", "10"),
        ("MO-3", "10"),
    ]
    assert all(i.severity == "soft" for i in inferred)


def test_build_two_inferred_ops_on_one_work_center_option_spread_by_machine_id() -> None:
    center_option = [_option("form", None, 0)]
    orders = [
        _running_op("MO-1", options=center_option),
        _running_op("MO-2", options=center_option),
    ]
    request = _ready(build_problem_from_erp(_snapshot(orders, FORM_MACHINES), _profile()))
    assert _running(request) == {"MO-1:10": "form-1", "MO-2:10": "form-2"}


def test_build_explicit_machine_claims_first_even_from_a_later_order() -> None:
    # MO-3 names form-1 explicitly; MO-1 and MO-2 infer around it.
    orders = [_running_op("MO-1"), _running_op("MO-2"), _running_op("MO-3", machine="form-1")]
    result = build_problem_from_erp(_snapshot(orders, FORM_MACHINES), _profile())
    request = _ready(result)
    assert _running(request) == {"MO-1:10": "form-2", "MO-2:10": "form-3", "MO-3:10": "form-1"}
    inferred = {i.lineage.key for i in _issues(result, "in_process_machine_inferred") if i.lineage}
    assert inferred == {("MO-1", "10"), ("MO-2", "10")}


def test_build_inferred_ops_of_one_order_go_by_op_sequence() -> None:
    # Two in-process ops of one order (document order 20 before 10): op 10 picks first.
    order = _order(
        "MO-1",
        _op("MO-1", "20", "form", RANKED, status="in_process"),
        _op("MO-1", "10", "form", RANKED, status="in_process"),
        status="in_process",
    )
    request = _ready(build_problem_from_erp(_snapshot([order], FORM_MACHINES), _profile()))
    assert _running(request) == {"MO-1:10": "form-1", "MO-1:20": "form-2"}


def test_build_inference_skips_inactive_machines() -> None:
    machines = [_machine("form-1", "form", active=False), *FORM_MACHINES[1:]]
    orders = [_running_op("MO-1"), _running_op("MO-2")]
    request = _ready(build_problem_from_erp(_snapshot(orders, machines), _profile()))
    assert _running(request) == {"MO-1:10": "form-2", "MO-2:10": "form-3"}


def test_build_more_in_process_ops_than_machines_leaves_extras_pending_with_issue() -> None:
    machines = FORM_MACHINES[:2]
    two_options = RANKED[:2]
    orders = [_running_op(f"MO-{n}", options=two_options) for n in (1, 2, 3)]
    snapshot = _snapshot(orders, machines)
    result = build_problem_from_erp(snapshot, _profile())
    request = _ready(result)
    assert _running(request) == {"MO-1:10": "form-1", "MO-2:10": "form-2"}
    extra = _ops(request)["MO-3:10"]
    assert extra.status == OperationStatus()
    assert extra.quantity == 6.0  # qty_remaining
    assert {o.machine_id for o in extra.options} == {"form-1", "form-2"}
    (unknown,) = _issues(result, "in_progress_machine_unknown")
    assert unknown.severity == "soft"
    assert unknown.path == "$.production_orders[2].operations[0]"
    assert unknown.lineage == snapshot.production_orders[2].operations[0].lineage
    inferred = {i.lineage.key for i in _issues(result, "in_process_machine_inferred") if i.lineage}
    assert inferred == {("MO-1", "10"), ("MO-2", "10")}  # PINNED: not for the pending extra
    assert validate(request) == []
    assert solve(request, solver=BaselineSolver()).verified is True


def test_build_explicit_claims_can_leave_an_inferred_op_pending() -> None:
    machines = FORM_MACHINES[:2]
    two_options = RANKED[:2]
    orders = [
        _running_op("MO-1", options=two_options),
        _running_op("MO-2", machine="form-1", options=two_options),
        _running_op("MO-3", machine="form-2", options=two_options),
    ]
    result = build_problem_from_erp(_snapshot(orders, machines), _profile())
    request = _ready(result)
    assert _running(request) == {"MO-2:10": "form-1", "MO-3:10": "form-2"}
    assert _ops(request)["MO-1:10"].status.state == "pending"
    (unknown,) = _issues(result, "in_progress_machine_unknown")
    assert unknown.path == "$.production_orders[0].operations[0]"


def test_build_never_puts_two_in_progress_ops_on_one_machine_even_when_erp_does() -> None:
    # Beyond the ruling text: two ERP ops name the same machine. Whatever the builder does
    # with the second one, the request must stay valid for TF-002.
    orders = [_running_op("MO-1", machine="form-1"), _running_op("MO-2", machine="form-1")]
    request = _ready(build_problem_from_erp(_snapshot(orders, FORM_MACHINES), _profile()))
    _assert_one_in_progress_per_machine(request)
    assert validate(request) == []


# --- gap 3: shift crossing ----------------------------------------------------------------------


def _mixed_order(lot: float = 10.0) -> ErpSnapshot:
    """A form op and an oven (batch) op with the given batch capacity."""
    batch = {"capacity": lot, "unit": "ea"}
    order = _order(
        "MO-1",
        _op("MO-1", "10", "form", [_option("form", "form-1", 0)]),
        _op(
            "MO-1", "20", "oven", [_option("oven", None, 0, run_seconds_per_unit=0.0)], batch=batch
        ),
    )
    return _snapshot([order], [_machine("form-1", "form"), _machine("oven-1", "oven")])


def _crossings(snapshot: ErpSnapshot, profile: PlanningProfile) -> dict[str, str]:
    request = _ready(build_problem_from_erp(snapshot, profile))
    return {op.id: op.shift_crossing for op in _ops(request).values()}


def test_erp_policy_shift_crossing_defaults() -> None:
    policy = ErpPolicy()
    assert policy.shift_crossing_default == "pause"
    assert policy.batch_shift_crossing == "finish_unattended"


def test_build_default_pause_for_plain_ops_and_finish_unattended_for_batch_ops() -> None:
    assert _crossings(_mixed_order(), _profile()) == {
        "MO-1:10": "pause",
        "MO-1:20": "finish_unattended",
    }


def test_build_batch_op_over_capacity_is_still_finish_unattended() -> None:
    # The lot (10) exceeds the capacity (5): no batch_family, but it is still an oven op.
    snapshot = _mixed_order(lot=5.0)
    request = _ready(build_problem_from_erp(snapshot, _profile()))
    oven = _ops(request)["MO-1:20"]
    assert oven.batch_family is None
    assert oven.shift_crossing == "finish_unattended"


def test_build_profile_overrides_both_shift_crossing_policies() -> None:
    profile = _profile('shift_crossing_default = "forbid"\nbatch_shift_crossing = "pause"\n')
    assert _crossings(_mixed_order(), profile) == {"MO-1:10": "forbid", "MO-1:20": "pause"}


def test_build_profile_can_make_plain_ops_finish_unattended() -> None:
    profile = _profile('shift_crossing_default = "finish_unattended"\n')
    assert _crossings(_mixed_order(), profile)["MO-1:10"] == "finish_unattended"


def test_profile_rejects_an_unknown_shift_crossing_with_its_key_path() -> None:
    with pytest.raises(ProfileError, match=r"erp\.shift_crossing_default: literal_error"):
        _profile('shift_crossing_default = "sometimes"\n')
    with pytest.raises(ProfileError, match=r"erp\.batch_shift_crossing: literal_error"):
        _profile('batch_shift_crossing = "never"\n')


def test_rush_job_follows_the_shift_crossing_policy() -> None:
    routing_op = {
        "op_id": "10",
        "sequence": 10,
        "work_center_id": "form",
        "options": [_option("form", "form-1", 0)],
        "setup_params": {},
        "queue_seconds": 0.0,
        "move_seconds": 0.0,
        "lineage": lineage("routing_operation", "RT-SPR", "10"),
    }
    routing = {
        "id": "RT-SPR",
        "item_id": "SPR",
        "operations": [routing_op],
        "lineage": lineage("routing", "RT-SPR"),
    }
    base = _mixed_order()
    snapshot = ErpSnapshot.model_validate(
        {**base.model_dump(mode="json"), "routings": [routing], "digest": None}
    )
    rush = RushOrder(order_id="RUSH-1", item_id="SPR", quantity=5.0, due="2026-10-20T00:00:00Z")
    job = build_rush_job(snapshot, _profile(), rush)
    assert isinstance(job, Job), job
    assert [op.shift_crossing for op in job.operations] == ["pause"]


# --- scaled shop end to end ---------------------------------------------------------------------

# Validate + build + baseline solve measured about 1.5 s before the fixes (when the problem
# still solved), so this end-to-end test is not marked slow (limit: 10 s).
SCALED_PROFILE = """
[erp.class_weights]
"A-KEY" = 4
"B-CORE" = 2
"C-SPOT" = 1
"""


def test_scaled_shop_validates_builds_and_solves_verified_with_the_baseline() -> None:
    snapshot = scaled_shop()
    profile = load_planning_profile(SCALED_PROFILE)
    report = validate_snapshot(snapshot, profile)
    assert [(i.code, i.path) for i in report.issues if i.severity == "hard"] == []

    result = build_problem_from_erp(snapshot, profile)
    request = _ready(result)
    assert validate(request) == []
    _assert_one_in_progress_per_machine(request)
    assert any(op.shift_crossing == "finish_unattended" for op in _ops(request).values())
    substitutes = [
        option
        for op in _ops(request).values()
        for option in op.options
        if option.work_center_id is not None
    ]
    assert substitutes and all(o.work_center_id == "form" for o in substitutes)

    started = time.perf_counter()
    solved = solve(request, solver=BaselineSolver(), budget_seconds=60.0)
    elapsed = time.perf_counter() - started
    print(f"scaled shop baseline solve: {elapsed:.2f} s")
    assert solved.schedule.status in {"FEASIBLE", "OPTIMAL"}, solved.issues[:5]
    assert solved.verified is True, solved.issues[:5]


# --- ruling 4: running crews, unattended crews, releases past the horizon -----------------------

CREW = {"labor_pool_id": "crew", "size": 1}


def _with_crew_pool(snapshot: ErpSnapshot) -> ErpSnapshot:
    pool = {"id": "crew", "size": 2, "calendar_id": None, "lineage": lineage("labor_pool", "crew")}
    data = {**snapshot.model_dump(mode="json"), "labor_pools": [pool], "digest": None}
    return ErpSnapshot.model_validate(data)


def test_build_in_progress_op_carries_no_operator_needs() -> None:
    # The running crew is already committed; asking for its calendar at `now` is wrong.
    order = _order(
        "MO-1",
        _op("MO-1", "10", "form", RANKED, status="in_process", machine="form-1", crew=CREW),
        _op("MO-1", "20", "form", RANKED, crew=CREW),
        status="in_process",
    )
    snapshot = _with_crew_pool(_snapshot([order], FORM_MACHINES))
    ops = _ops(_ready(build_problem_from_erp(snapshot, _profile())))
    assert ops["MO-1:10"].status.state == "in_progress"
    assert ops["MO-1:10"].operator_needs == ()
    assert [(n.pool_id, n.quantity, n.phase) for n in ops["MO-1:20"].operator_needs] == [
        ("crew", 1, "both")
    ]


def test_build_finish_unattended_op_needs_its_crew_for_the_setup_only() -> None:
    batch = {"capacity": 10.0, "unit": "ea"}
    order = _order(
        "MO-1",
        _op("MO-1", "10", "oven", [_option("oven", "oven-1", 0)], batch=batch, crew=CREW),
    )
    snapshot = _with_crew_pool(_snapshot([order], [_machine("oven-1", "oven")]))
    oven = _ops(_ready(build_problem_from_erp(snapshot, _profile())))["MO-1:10"]
    assert oven.shift_crossing == "finish_unattended"
    assert [(n.pool_id, n.quantity, n.phase) for n in oven.operator_needs] == [("crew", 1, "setup")]


def test_build_release_at_or_after_the_horizon_end_holds_the_job_with_soft_issue() -> None:
    # as_of 2026-10-12 05:00 CDT, horizon 7 days: 2026-10-19 05:00 CDT. A release date of
    # 2026-10-20 starts at 00:00 local, after the horizon end; MO-2 releases inside it.
    late = _order("MO-1", _op("MO-1", "10", "form", RANKED)) | {"release_date": "2026-10-20"}
    early = _order("MO-2", _op("MO-2", "10", "form", RANKED)) | {"release_date": "2026-10-13"}
    snapshot = _snapshot([late, early], FORM_MACHINES)
    result = build_problem_from_erp(snapshot, _profile("horizon_days = 7\n"))
    request = _ready(result)
    jobs = {job.id: job for job in request.problem.jobs}
    assert (jobs["MO-1"].status, jobs["MO-2"].status) == ("held", "open")
    (found,) = _issues(result, "release_after_horizon")
    assert (found.severity, found.path) == ("soft", "$.production_orders[0]")
    assert found.lineage == snapshot.production_orders[0].lineage
    solved = solve(request, solver=BaselineSolver())
    assert solved.verified is True, solved.issues


def test_build_op_material_ready_after_the_horizon_end_holds_the_job() -> None:
    # Op 10's wire arrives on 2026-10-20, after the 7-day horizon: no working time is left
    # for it, so the job is held rather than failing the whole solve.
    wire = {"id": "WIRE", "uom": "ea", "attributes": {}, "lineage": lineage("item", "WIRE")}
    need = {
        "item_id": "WIRE",
        "op_id": "10",
        "qty_required": 5.0,
        "qty_issued": 0.0,
        "lineage": lineage("material", "MO-1", "WIRE"),
    }
    purchase = {
        "order_type": "PO",
        "order_nbr": "PO-1",
        "line_nbr": "1",
        "vendor_id": "V",
        "item_id": "WIRE",
        "qty_ordered": 5.0,
        "qty_received": 0.0,
        "status": "open",
        "promised_date": "2026-10-20",
        "receipts": [],
        "lineage": lineage("purchase_line", "PO-1", "1"),
    }
    order = _order("MO-1", _op("MO-1", "10", "form", RANKED)) | {"materials": [need]}
    base = _snapshot([order], FORM_MACHINES).model_dump(mode="json")
    items = [*base["items"], wire]
    data = {**base, "items": items, "purchase_lines": [purchase], "digest": None}
    snapshot = ErpSnapshot.model_validate(data)
    result = build_problem_from_erp(snapshot, _profile("horizon_days = 7\n"))
    request = _ready(result)
    (job,) = request.problem.jobs
    assert job.status == "held"
    (found,) = _issues(result, "release_after_horizon")
    assert found.path == "$.production_orders[0]"
