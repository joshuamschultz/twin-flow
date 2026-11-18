"""Operator pools by phase (T-176, REQ-130, REQ-131, COMP-135, D-088).

Machines M1 to M4 are always working with a 10 s setup before every op (work-center
function). Pool P has one operator unless a test says otherwise. A need holds the pool over
the verifier's `_need_span`: setup `[start, setup_end)`, run `[setup_end, end)`, both
`[start, end)`. Requests are granted by `(planned start, operation id)` (D-088).
"""

from __future__ import annotations

import pytest
from _engine_cases import (
    SLOW,
    after,
    assert_replays,
    assert_times,
    assert_verified,
    at,
    by_op,
    compile_problem,
    job,
    need,
    op,
    peak_pool_usage,
    plan,
    problem,
    run_plan,
    slow,
)

from twinflow.scheduling.model import (
    CompiledProblem,
    Job,
    Machine,
    Operation,
    OperatorPool,
    SetupFunction,
)

SETUP_10 = SetupFunction(id="S10", terms=(), default_seconds=10.0)
MACHINES = tuple(Machine(id=f"M{i}", work_center_id="A") for i in range(1, 5))
FEEDER = Machine(id="M9", work_center_id="B")
"""No setup (its center has no function): where a slowed common predecessor runs."""


def _compiled(jobs: list[Job], pool_size: int = 1) -> CompiledProblem:
    return compile_problem(
        problem(
            jobs,
            (*MACHINES, FEEDER),
            pools=(OperatorPool(id="P", size=pool_size),),
            setup_functions=(SETUP_10,),
            center_setups={"A": "S10"},
        )
    )


def _operations(compiled: CompiledProblem) -> dict[str, Operation]:
    return {o.id: o for j in compiled.problem.jobs for o in j.operations}


# --- phases hold exactly the verifier's span ----------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "b_times"),
    [
        # B's setup may start when A's setup (and so A's hold) ends at 10.
        ("setup", (10, 20, 40)),
        # B's setup [20, 30) overlaps A's run but needs nobody; B's run starts at A's end.
        ("run", (20, 30, 50)),
        # B needs the operator from its start, so it waits for A's end at 30.
        ("both", (30, 40, 60)),
    ],
)
def test_need_phase_holds_the_pool_over_exactly_the_verifier_need_span(
    phase: str, b_times: tuple[float, float, float]
) -> None:
    jobs = [
        job("JA", op("A", "M1", 20, operator_needs=(need("P", phase),))),
        job("JB", op("B", "M2", 20, operator_needs=(need("P", phase),))),
    ]
    compiled = _compiled(jobs)
    schedule = plan(at("A", "M1", 0, 10, 30), at("B", "M2", *b_times))
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule)

    # Holding longer than the span would delay B; the plan is tight against it.
    assert_replays(run, schedule)
    assert peak_pool_usage(run.records, _operations(compiled), "P") == 1


# --- grant order -------------------------------------------------------------------------------


def test_operator_goes_first_to_the_earlier_planned_start_when_both_become_ready() -> None:
    # z (planned 10) and a (planned 40) both wait for p; p ends at 50 instead of 10, so both
    # are ready at 50. z is granted first though "a" < "z" by id.
    jobs = [
        job(
            "J",
            op("p", "M9", 10, work_center_id="B", quantity=SLOW),
            op("z", "M1", 20, operator_needs=(need("P", "both"),), predecessors=after("p")),
            op("a", "M2", 20, operator_needs=(need("P", "both"),), predecessors=after("p")),
        )
    ]
    compiled = _compiled(jobs)
    schedule = plan(at("p", "M9", 0, 0, 10), at("z", "M1", 10, 20, 40), at("a", "M2", 40, 50, 70))
    assert_verified(compiled, schedule)

    records = by_op(run_plan(compiled, schedule, slow(5.0)))

    assert_times(records["p"], 0, 0, 50)
    assert_times(records["z"], 50, 60, 80)
    assert_times(records["a"], 80, 90, 110)


def test_operator_tie_on_planned_start_goes_to_the_lower_operation_id() -> None:
    # Unverified on purpose: both planned at 0 with one operator. "a" (on M2) wins over
    # "b" (on M1), so neither machine-id order nor document order decides.
    jobs = [
        job("Jb", op("b", "M1", 20, operator_needs=(need("P", "both"),))),
        job("Ja", op("a", "M2", 20, operator_needs=(need("P", "both"),))),
    ]
    compiled = _compiled(jobs)
    schedule = plan(at("b", "M1", 0, 10, 30), at("a", "M2", 0, 10, 30))

    records = by_op(run_plan(compiled, schedule))

    assert_times(records["a"], 0, 10, 30)
    assert_times(records["b"], 30, 40, 60)


def test_delayed_op_releases_its_operator_only_at_its_actual_end() -> None:
    # A runs 30 s instead of 20 and ends at 40; B, planned at A's planned end 30, waits.
    jobs = [
        job("JA", op("A", "M1", 20, quantity=SLOW, operator_needs=(need("P", "both"),))),
        job("JB", op("B", "M2", 20, operator_needs=(need("P", "both"),))),
    ]
    compiled = _compiled(jobs)
    schedule = plan(at("A", "M1", 0, 10, 30), at("B", "M2", 30, 40, 60))
    assert_verified(compiled, schedule)

    records = by_op(run_plan(compiled, schedule, slow(1.5)))

    assert_times(records["A"], 0, 10, 40)
    assert_times(records["B"], 40, 50, 70)


# --- capacity ----------------------------------------------------------------------------------


def test_pool_capacity_is_never_exceeded_when_slips_create_contention() -> None:
    # Pool of 2. a1 and a2 (one operator each) run 40 s instead of 20 and end at 50.
    # q needs both operators from its planned start 30: it waits for 50. r (one operator,
    # setup phase, planned 50) is ready at 50 too but q's earlier planned start wins.
    jobs = [
        job("J1", op("a1", "M1", 20, quantity=SLOW, operator_needs=(need("P", "both"),))),
        job("J2", op("a2", "M2", 20, quantity=SLOW, operator_needs=(need("P", "both"),))),
        job("J3", op("q", "M3", 10, operator_needs=(need("P", "both", 2),))),
        job("J4", op("r", "M4", 10, operator_needs=(need("P", "setup"),))),
    ]
    compiled = _compiled(jobs, pool_size=2)
    schedule = plan(
        at("a1", "M1", 0, 10, 30),
        at("a2", "M2", 0, 10, 30),
        at("q", "M3", 30, 40, 50),
        at("r", "M4", 50, 60, 70),
    )
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule, slow(2.0))
    records = by_op(run)

    assert run.termination == "completed"
    assert peak_pool_usage(run.records, _operations(compiled), "P") <= 2
    assert_times(records["q"], 50, 60, 70)
    assert_times(records["r"], 70, 80, 90)


def test_former_guard_binding_operator_headcount() -> None:
    """Former guard "binding headcount": one operator serializes three free machines (T-176)."""
    jobs = [
        job(f"J{o}", op(o, m, 20, operator_needs=(need("P", "both"),)))
        for o, m in (("A", "M1"), ("B", "M2"), ("C", "M3"))
    ]
    compiled = _compiled(jobs)
    schedule = plan(at("A", "M1", 0, 10, 30), at("B", "M2", 30, 40, 60), at("C", "M3", 60, 70, 90))
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule)

    assert_replays(run, schedule)
    assert peak_pool_usage(run.records, _operations(compiled), "P") == 1
