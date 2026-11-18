"""Calendars and shift crossing in execution (T-174, REQ-129, REQ-131, COMP-135, D-089).

Calendar `C` works [0, 100), [120, 200), [220, 300). Pool calendar `CP` works [0, 90) and
[130, 300), narrower than `C`; their intersection (the effective calendar) is
[0, 90), [130, 200), [220, 300). Each slip test plans the calendared op before the time its
slowed predecessor now ends, so the executor's own calendar decision is what is tested.
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
    plan,
    problem,
    run_plan,
    slow,
)

from twinflow.scheduling import calendar_math
from twinflow.scheduling.model import (
    Calendar,
    Job,
    Machine,
    OperatorPool,
    OvertimeInterval,
    Problem,
    SetupFunction,
)

C = Calendar(id="C", intervals=((0.0, 100.0), (120.0, 200.0), (220.0, 300.0)))
CP = Calendar(id="CP", intervals=((0.0, 90.0), (130.0, 300.0)))
EFFECTIVE = calendar_math.intersect(C.intervals, CP.intervals)
SETUP_10 = SetupFunction(id="S10", terms=(), default_seconds=10.0)

FREE = Machine(id="M1", work_center_id="A")
"""Always working: where the slowed predecessors run."""
SET_UP = Machine(id="M2", work_center_id="A", calendar_id="C", setup_function_id="S10")
"""Calendar C, 10 s setup before every op."""
PLAIN = Machine(id="M3", work_center_id="A", calendar_id="C")
"""Calendar C, no setup."""
MACHINES = (FREE, SET_UP, PLAIN)
POOL = OperatorPool(id="P", size=5, calendar_id="CP")


def _problem(*jobs: Job) -> Problem:
    return problem(jobs, MACHINES, calendars=(C, CP), pools=(POOL,), setup_functions=(SETUP_10,))


def test_effective_calendar_fixture_is_the_machine_and_pool_intersection() -> None:
    assert list(EFFECTIVE or ()) == [(0.0, 90.0), (130.0, 200.0), (220.0, 300.0)]


# --- forbid (D-089) ---------------------------------------------------------------------------


def test_forbid_waits_for_the_next_interval_where_setup_plus_planned_run_fits() -> None:
    # p ends at 66 instead of 44. At 66 the run alone (30 s) fits before 100, but setup 10
    # plus run 30 does not, so s waits for 120.
    compiled = compile_problem(
        _problem(
            job(
                "J",
                op("p", "M1", 44, quantity=SLOW),
                op("s", "M2", 30, predecessors=after("p")),
            )
        )
    )
    schedule = plan(at("p", "M1", 0, 0, 44), at("s", "M2", 44, 54, 84))
    assert_verified(compiled, schedule)
    assert_replays(run_plan(compiled, schedule), schedule)

    records = by_op(run_plan(compiled, schedule, slow(1.5)))

    assert_times(records["p"], 0, 0, 66)
    assert_times(records["s"], 120, 130, 160)
    assert records["s"].setup_seconds == pytest.approx(10.0)


def test_forbid_sampled_overrun_finishes_past_the_interval_end_and_is_recorded() -> None:
    compiled = compile_problem(_problem(job("J", op("o", "M3", 40, quantity=SLOW))))
    schedule = plan(at("o", "M3", 50, 50, 90))
    assert_verified(compiled, schedule)

    planned = by_op(run_plan(compiled, schedule))["o"]
    overrun = by_op(run_plan(compiled, schedule, slow(1.5)))["o"]

    assert planned.overrun_seconds == 0.0
    # Started on expected time (40 s fits [50, 90)); 60 s of work ends at 110, 10 s past 100.
    assert_times(overrun, 50, 50, 110)
    assert overrun.run_seconds == pytest.approx(60.0)
    assert overrun.overrun_seconds == pytest.approx(10.0)
    assert overrun.censored is False


# --- pause on the effective calendar ------------------------------------------------------------


def _pause_job(*, quantity: float = 1.0, predecessor: bool = False) -> Job:
    ops = [op("p", "M1", 60, quantity=SLOW)] if predecessor else []
    ops.append(
        op(
            "w",
            "M3",
            50,
            quantity=quantity,
            shift_crossing="pause",
            operator_needs=(need("P", "both"),),
            predecessors=after("p") if predecessor else (),
        )
    )
    return job("J", *ops)


def test_pause_end_equals_finish_after_work_on_machine_and_pool_calendar() -> None:
    compiled = compile_problem(_problem(_pause_job()))
    end = calendar_math.finish_after_work(EFFECTIVE, 70.0, 50.0)
    assert end == pytest.approx(160.0)  # 20 s to 90 (pool break), 30 s from 130
    schedule = plan(at("w", "M3", 70, 70, end))
    assert_verified(compiled, schedule)

    assert_replays(run_plan(compiled, schedule), schedule)


def test_pause_slowed_run_consumes_working_seconds_of_the_effective_calendar() -> None:
    compiled = compile_problem(_problem(_pause_job(quantity=SLOW)))
    schedule = plan(at("w", "M3", 70, 70, 160))
    assert_verified(compiled, schedule)

    record = by_op(run_plan(compiled, schedule, slow(1.5)))["w"]

    expected_end = calendar_math.finish_after_work(EFFECTIVE, 70.0, 75.0)
    assert expected_end == pytest.approx(185.0)
    assert_times(record, 70, 70, expected_end)


def test_pause_starts_on_a_working_instant_of_the_effective_calendar() -> None:
    # p ends at 90 instead of 60: the machine works at 90, the pool does not until 130.
    compiled = compile_problem(_problem(_pause_job(predecessor=True)))
    schedule = plan(at("p", "M1", 0, 0, 60), at("w", "M3", 70, 70, 160))
    assert_verified(compiled, schedule)
    assert calendar_math.next_working(C.intervals, 90.0) == 90.0

    record = by_op(run_plan(compiled, schedule, slow(1.5)))["w"]

    start = calendar_math.next_working(EFFECTIVE, 90.0)
    assert start == pytest.approx(130.0)
    assert_times(record, start, start, calendar_math.finish_after_work(EFFECTIVE, start, 50.0))


# --- finish_unattended -------------------------------------------------------------------------


def _unattended_job() -> Job:
    return job(
        "J",
        op("p", "M1", 40, quantity=SLOW),
        op("u", "M3", 50, shift_crossing="finish_unattended", predecessors=after("p")),
    )


def test_finish_unattended_runs_through_the_break_after_a_working_start() -> None:
    compiled = compile_problem(_problem(_unattended_job()))
    schedule = plan(at("p", "M1", 0, 0, 40), at("u", "M3", 80, 80, 130))
    assert_verified(compiled, schedule)

    assert_replays(run_plan(compiled, schedule), schedule)


def test_finish_unattended_waits_for_a_working_instant_then_runs_continuously() -> None:
    # p ends at 100 (an interval end, not a working instant): u starts at 120, runs 50 s.
    compiled = compile_problem(_problem(_unattended_job()))
    schedule = plan(at("p", "M1", 0, 0, 40), at("u", "M3", 80, 80, 130))

    record = by_op(run_plan(compiled, schedule, slow(2.5)))["u"]

    assert_times(record, 120, 120, 170)


# --- overtime is working time ------------------------------------------------------------------


def test_overtime_interval_counts_as_working_time_for_forbid() -> None:
    shift = Calendar(
        id="OT",
        intervals=((0.0, 100.0), (100.0, 140.0)),
        overtime=(OvertimeInterval(start=100.0, end=140.0, cost_multiplier=1.5),),
    )
    machine = Machine(id="M4", work_center_id="A", calendar_id="OT")
    compiled = compile_problem(
        problem([job("J", op("o", "M4", 120))], (machine,), calendars=(shift,))
    )
    schedule = plan(at("o", "M4", 10, 10, 130))
    assert_verified(compiled, schedule)

    assert_replays(run_plan(compiled, schedule), schedule)


def test_overtime_interval_counts_as_working_time_for_pause() -> None:
    shift = Calendar(
        id="OT2",
        intervals=((0.0, 100.0), (120.0, 160.0)),
        overtime=(OvertimeInterval(start=120.0, end=160.0, cost_multiplier=2.0),),
    )
    machine = Machine(id="M4", work_center_id="A", calendar_id="OT2")
    compiled = compile_problem(
        problem(
            [job("J", op("o", "M4", 60, shift_crossing="pause"))], (machine,), calendars=(shift,)
        )
    )
    schedule = plan(at("o", "M4", 80, 80, 160))  # 20 s before the break, 40 s of overtime
    assert_verified(compiled, schedule)

    assert_replays(run_plan(compiled, schedule), schedule)


# --- calendar end censors ------------------------------------------------------------------------


def test_calendar_that_ends_before_work_finishes_censors_the_op_and_its_job() -> None:
    # Every machine is calendared, so the run stops at the last working instant, 300.
    # p ends at 250 instead of 100; s pauses from 250 and has 50 of its 80 s by 300;
    # s2 (after s, same job) never starts. q, on another job, is done by 50.
    long_shift = Calendar(id="CL", intervals=((0.0, 300.0),))
    m1 = Machine(id="M1", work_center_id="A", calendar_id="CL")
    jobs = [
        job(
            "J",
            op("p", "M1", 100, quantity=SLOW),
            op("s", "M3", 80, shift_crossing="pause", predecessors=after("p")),
            op("s2", "M1", 10, predecessors=after("s")),
        ),
        job("K", op("q", "M3", 50, shift_crossing="pause")),
    ]
    compiled = compile_problem(problem(jobs, (m1, PLAIN), calendars=(C, long_shift)))
    schedule = plan(
        at("p", "M1", 0, 0, 100),
        at("s", "M3", 120, 120, 200),
        at("s2", "M1", 200, 200, 210),
        at("q", "M3", 0, 0, 50),
    )
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule, slow(2.5))
    records = by_op(run)

    assert run.termination == "calendar_end"
    assert records["s"].censored is True
    assert records["s"].start == pytest.approx(250.0)
    assert records["s2"].censored is True
    assert records["p"].censored is False
    assert_times(records["q"], 0, 0, 50)
    assert records["q"].censored is False


# --- former guard -------------------------------------------------------------------------------


def test_former_guard_labor_pool_calendar() -> None:
    """Former guard "no labor calendar": a pool calendar narrower than the machine's (T-174).

    f needs pool P (works to 90) on a machine working to 100. After p slips to 65, [65, 95)
    fits the machine but not the pool, so f waits for the effective calendar's 130.
    """
    jobs = [
        job(
            "J",
            op("p", "M1", 26, quantity=SLOW),
            op("f", "M3", 30, operator_needs=(need("P", "both"),), predecessors=after("p")),
        )
    ]
    compiled = compile_problem(_problem(*jobs))
    schedule = plan(at("p", "M1", 0, 0, 26), at("f", "M3", 26, 26, 56))
    assert_verified(compiled, schedule)
    assert calendar_math.fits_within(C.intervals, 65.0, 95.0)
    assert_replays(run_plan(compiled, schedule), schedule)

    records = by_op(run_plan(compiled, schedule, slow(2.5)))

    assert_times(records["p"], 0, 0, 65)
    assert_times(records["f"], 130, 130, 160)
