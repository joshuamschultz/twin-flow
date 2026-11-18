"""Robustness measures of a plan (T-183, REQ-137, COMP-137, D-100, D-101; Pinedo Sec 18.1).

API under test, `twinflow.execution.robustness`:

- `planned_robustness(values) -> float`: R(S) = sum w_j (d_j - C_j) / sum w_j (d_j - now)
  read from the values of `objectives.evaluate` (the `_robustness` formula).
- `DeltaPoint{delta_seconds, machine_id, objective, increase, slope}`: one probe replay.
  `objective` is Z'(delta) of `policy.robustness_objective`, `increase` = Z'(delta) - Z
  (Z the planned value) and `slope` = increase / delta.
- `delta_curve(shop, compiled, plan, policy) -> tuple[DeltaPoint, ...]`: probe machines are
  the top `policy.robustness_probe_machines` by planned busy share (ties by id, never more
  than the shop has); for each probe m and each delta of `policy.robustness_deltas_seconds`
  the plan is replayed deterministically (`DeterministicSource`, no breakdowns, no profile
  outages, whatever the policy's source) with machine orders fixed and the one scripted
  outage (m, now, delta, 1.0). Points come in grid order, then probe order.
- `headline(points) -> tuple[DeltaPoint, ...]`: the worst probe per delta (largest
  `increase`, ties by machine id), one point per delta in grid order (Pinedo measure 1).
- `expected_objective(z, points, probabilities) -> float | None`:
  Z + sum p_delta (Z'(delta) - Z) over the headline points; `probabilities[i]` belongs to the
  i-th delta of the grid; None when `probabilities` is None (D-100).
- `load_check(shop, plan, targets) -> LoadCheck{weeks, violations_total, worst_excess,
  violations}`: weeks k = 0, 1, ... of 7 days from `now` up to the plan's last end; per
  machine and week, load = planned busy seconds in the week (clipped to it) / working
  seconds of the machine's calendar in the week (no calendar: the whole week); weeks without
  working time are skipped; a violation is load > `targets[min(k, len(targets) - 1)]`;
  `violations` holds the first 100 `LoadViolation{machine_id, week, load, target}` by excess
  (load - target) descending; `worst_excess` is 0.0 without violations (D-101).
- `robustness_summary(shop, compiled, plan, policy, targets) -> RobustnessSummary{planned_r,
  delta_curve, expected_objective, load_check}` (the COMP-137 output).

Example 18.1.1 (Pinedo p.483-485): one machine, p = 10, 10, 10, d = 10, 22, 34,
w = 1, 100, 100, now = 0. Denominator of R(S): 1 x 10 + 100 x 22 + 100 x 34 = 5610.
Sequence 1,2,3: C = 10, 20, 30, Z = 0, R = (0 + 200 + 400) / 5610 = 0.107; outage of 10 at 0:
C = 20, 30, 40, T = 10, 8, 6, Z' = 10 + 800 + 600 = 1410. Sequence 2,3,1: C2 = 10, C3 = 20,
C1 = 30, Z = 20, R = (1200 + 1400 - 20) / 5610 = 0.460; outage of 10: C2 = 20, C3 = 30, C1 = 40,
Z' = 30. With p(10) = 0.1: E = 0 + 0.1 x 1410 = 141 and 20 + 0.1 x 10 = 21.

The module is imported lazily so each test fails at the call after its fixture checks pass.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from _engine_cases import assert_verified, at, compile_problem, job, op, plan, problem

from twinflow.execution.policy import (
    BreakdownRule,
    Deterministic,
    FactorRule,
    LogNormal,
    ScriptedOutage,
    SimulationPolicy,
)
from twinflow.execution.shop import ShopModel, build_shop
from twinflow.scheduling.model import (
    Calendar,
    CompiledProblem,
    Job,
    Machine,
    ObjectiveKind,
    Schedule,
)
from twinflow.scheduling.objectives import evaluate

WEEK = 7 * 86_400.0
TOL = 1e-3
EXACT = 1e-6
WT = ObjectiveKind.WEIGHTED_TARDINESS


class _LazyModule:
    """The module under test, imported at its first attribute access (after arrange)."""

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module("twinflow.execution.robustness"), name)


@pytest.fixture
def rb() -> Any:
    """`twinflow.execution.robustness`, imported where a test first uses it."""
    return _LazyModule()


# --- Pinedo Example 18.1.1 ------------------------------------------------------------------


def _pinedo_compiled() -> CompiledProblem:
    jobs = [
        job("J1", op("O1", "M", 10.0)).model_copy(update={"due": 10.0, "weight_override": 1.0}),
        job("J2", op("O2", "M", 10.0)).model_copy(update={"due": 22.0, "weight_override": 100.0}),
        job("J3", op("O3", "M", 10.0)).model_copy(update={"due": 34.0, "weight_override": 100.0}),
    ]
    return compile_problem(problem(jobs, [Machine(id="M", work_center_id="A")]))


def _sequence(order: Sequence[str]) -> Schedule:
    """The jobs' operations back to back on M from 0, in `order`."""
    return plan(
        *(at(f"O{j}", "M", 10.0 * i, 10.0 * i, 10.0 * (i + 1)) for i, j in enumerate(order))
    )


PINEDO = _pinedo_compiled()
PLANS = {"1,2,3": _sequence("123"), "2,3,1": _sequence("231")}
PLANNED_Z = {"1,2,3": 0.0, "2,3,1": 20.0}
PLANNED_R = {"1,2,3": 600 / 5610, "2,3,1": 2580 / 5610}
Z_PRIME_10 = {"1,2,3": 1410.0, "2,3,1": 30.0}
EXPECTED_10 = {"1,2,3": 141.0, "2,3,1": 21.0}


def _policy(**fields: object) -> SimulationPolicy:
    defaults: dict[str, object] = {
        "robustness_deltas_seconds": (10.0,),
        "robustness_objective": WT,
    }
    return SimulationPolicy.model_validate(defaults | fields)


def test_pinedo_fixture_plans_verify_and_match_the_book() -> None:
    """Precondition: both sequences verify and evaluate to the book's Z and R(S)."""
    for name, schedule in PLANS.items():
        assert_verified(PINEDO, schedule)
        values = evaluate(PINEDO, schedule)
        assert values[WT] == pytest.approx(PLANNED_Z[name], abs=EXACT), name
        assert values[ObjectiveKind.ROBUSTNESS] == pytest.approx(PLANNED_R[name], abs=EXACT)


@pytest.mark.parametrize(("name", "r"), [("1,2,3", 0.107), ("2,3,1", 0.460)])
def test_planned_robustness_pinedo_18_1_1_matches_book(rb: Any, name: str, r: float) -> None:
    values = evaluate(PINEDO, PLANS[name])
    assert rb.planned_robustness(values) == pytest.approx(r, abs=TOL)
    assert rb.planned_robustness(values) == pytest.approx(PLANNED_R[name], abs=EXACT)


@pytest.mark.parametrize("name", ["1,2,3", "2,3,1"])
def test_delta_curve_pinedo_outage_10_gives_book_z_prime_and_slope(rb: Any, name: str) -> None:
    points = rb.delta_curve(build_shop(PINEDO), PINEDO, PLANS[name], _policy())
    assert len(points) == 1
    point = points[0]
    assert point.machine_id == "M"
    assert point.delta_seconds == pytest.approx(10.0)
    assert point.objective == pytest.approx(Z_PRIME_10[name], abs=EXACT)
    assert point.increase == pytest.approx(Z_PRIME_10[name] - PLANNED_Z[name], abs=EXACT)
    assert point.slope == pytest.approx({"1,2,3": 141.0, "2,3,1": 1.0}[name], abs=EXACT)


@pytest.mark.parametrize("name", ["1,2,3", "2,3,1"])
def test_expected_objective_pinedo_probability_0_1_matches_book(rb: Any, name: str) -> None:
    policy = _policy(robustness_delta_probabilities=(0.1,))
    points = rb.delta_curve(build_shop(PINEDO), PINEDO, PLANS[name], policy)
    expected = rb.expected_objective(PLANNED_Z[name], points, (0.1,))
    assert expected == pytest.approx(EXPECTED_10[name], abs=EXACT)


def test_expected_objective_pinedo_ranks_2_3_1_first_while_planned_z_ranks_1_2_3(
    rb: Any,
) -> None:
    policy = _policy(robustness_delta_probabilities=(0.1,))
    expected = {
        name: rb.expected_objective(
            PLANNED_Z[name], rb.delta_curve(build_shop(PINEDO), PINEDO, s, policy), (0.1,)
        )
        for name, s in PLANS.items()
    }
    assert min(PLANNED_Z, key=PLANNED_Z.__getitem__) == "1,2,3"
    assert min(expected, key=expected.__getitem__) == "2,3,1"


def test_delta_curve_pinedo_two_deltas_in_grid_order(rb: Any) -> None:
    # Outage 20 on 1,2,3: C = 30, 40, 50, T = 20, 18, 16 -> 20 + 1800 + 1600 = 3420.
    # Outage 20 on 2,3,1: C2 = 30, C3 = 40, C1 = 50 -> 800 + 600 + 40 = 1440.
    policy = _policy(robustness_deltas_seconds=(10.0, 20.0))
    for name, z20 in (("1,2,3", 3420.0), ("2,3,1", 1440.0)):
        points = rb.delta_curve(build_shop(PINEDO), PINEDO, PLANS[name], policy)
        assert [p.delta_seconds for p in points] == [10.0, 20.0]
        assert [p.objective for p in points] == pytest.approx([Z_PRIME_10[name], z20])
        assert points[1].slope == pytest.approx((z20 - PLANNED_Z[name]) / 20.0)


def test_expected_objective_two_deltas_sums_each_probability_times_increase(
    rb: Any,
) -> None:
    # 0 + 0.1 x 1410 + 0.2 x 3420 = 141 + 684 = 825.
    policy = _policy(robustness_deltas_seconds=(10.0, 20.0))
    points = rb.delta_curve(build_shop(PINEDO), PINEDO, PLANS["1,2,3"], policy)
    assert rb.expected_objective(0.0, points, (0.1, 0.2)) == pytest.approx(825.0, abs=EXACT)


def test_expected_objective_without_probabilities_is_none(rb: Any) -> None:
    points = rb.delta_curve(build_shop(PINEDO), PINEDO, PLANS["1,2,3"], _policy())
    assert rb.expected_objective(0.0, points, None) is None


def test_delta_curve_ignores_the_policy_source_breakdowns_and_scripted_outages(
    rb: Any,
) -> None:
    """The probe replay is deterministic whatever the calibration says (D-100)."""
    noisy = _policy(
        distribution_source="profile",
        run_factors=(FactorRule(distribution=LogNormal(mean=1.5, cv=0.5)),),
        setup_factors=(FactorRule(distribution=LogNormal(mean=2.0, cv=0.5)),),
        breakdowns=(BreakdownRule(mtbf_seconds=60.0, repair=Deterministic(value=1000.0)),),
        outages=(ScriptedOutage(machine_id="M", start=0.0, duration=500.0),),
    )
    for name, schedule in PLANS.items():
        points = rb.delta_curve(build_shop(PINEDO), PINEDO, schedule, noisy)
        assert [p.objective for p in points] == pytest.approx([Z_PRIME_10[name]], abs=EXACT)


def test_delta_curve_uses_the_policy_robustness_objective(rb: Any) -> None:
    # Makespan of either sequence: 30 planned, 40 after a 10-second outage at 0.
    policy = _policy(robustness_objective=ObjectiveKind.MAKESPAN)
    for schedule in PLANS.values():
        (point,) = rb.delta_curve(build_shop(PINEDO), PINEDO, schedule, policy)
        assert point.objective == pytest.approx(40.0, abs=EXACT)
        assert point.increase == pytest.approx(10.0, abs=EXACT)
        assert point.slope == pytest.approx(1.0, abs=EXACT)


def test_robustness_summary_pinedo_collects_r_curve_expected_and_load(rb: Any) -> None:
    policy = _policy(robustness_delta_probabilities=(0.1,))
    shop = build_shop(PINEDO)
    summary = rb.robustness_summary(shop, PINEDO, PLANS["2,3,1"], policy, (0.9, 0.8, 0.7))
    assert summary.planned_r == pytest.approx(0.460, abs=TOL)
    assert summary.delta_curve == rb.delta_curve(shop, PINEDO, PLANS["2,3,1"], policy)
    assert summary.expected_objective == pytest.approx(21.0, abs=EXACT)
    # M has no calendar (always working): 30 busy seconds in one week, far under 90 percent.
    assert summary.load_check.weeks == 1
    assert summary.load_check.violations_total == 0
    assert summary.load_check.worst_excess == 0.0
    assert summary.load_check.violations == ()


def test_robustness_summary_without_probabilities_has_no_expected_objective(
    rb: Any,
) -> None:
    summary = rb.robustness_summary(
        build_shop(PINEDO), PINEDO, PLANS["1,2,3"], _policy(), (0.9, 0.8, 0.7)
    )
    assert summary.expected_objective is None


# --- headline and expected objective on synthetic points -----------------------------------


def _point(rb: Any, delta: float, machine: str, z_prime: float, z: float) -> object:
    return rb.DeltaPoint(
        delta_seconds=delta,
        machine_id=machine,
        objective=z_prime,
        increase=z_prime - z,
        slope=(z_prime - z) / delta,
    )


def test_headline_takes_the_worst_probe_per_delta_in_grid_order(rb: Any) -> None:
    points = (
        _point(rb, 10.0, "M1", 50.0, 20.0),
        _point(rb, 10.0, "M2", 90.0, 20.0),
        _point(rb, 30.0, "M1", 200.0, 20.0),
        _point(rb, 30.0, "M2", 120.0, 20.0),
    )
    head = rb.headline(points)
    assert [(p.delta_seconds, p.machine_id) for p in head] == [(10.0, "M2"), (30.0, "M1")]


def test_headline_breaks_ties_by_machine_id(rb: Any) -> None:
    points = (_point(rb, 10.0, "M9", 70.0, 20.0), _point(rb, 10.0, "M3", 70.0, 20.0))
    assert [p.machine_id for p in rb.headline(points)] == ["M3"]


def test_expected_objective_uses_the_worst_probe_per_delta(rb: Any) -> None:
    # Worst increases: 70 at delta 10, 180 at delta 30 -> 20 + 0.1 x 70 + 0.5 x 180 = 117.
    points = (
        _point(rb, 10.0, "M1", 50.0, 20.0),
        _point(rb, 10.0, "M2", 90.0, 20.0),
        _point(rb, 30.0, "M1", 200.0, 20.0),
        _point(rb, 30.0, "M2", 120.0, 20.0),
    )
    assert rb.expected_objective(20.0, points, (0.1, 0.5)) == pytest.approx(117.0, abs=EXACT)


# --- weekly load plans ----------------------------------------------------------------------

CAPACITY = 100_000.0
"""Working seconds per week of calendar `C`: [k x WEEK, k x WEEK + CAPACITY)."""


def _weekly_calendar(weeks: int, calendar_id: str = "C") -> Calendar:
    return Calendar(
        id=calendar_id,
        intervals=tuple((k * WEEK, k * WEEK + CAPACITY) for k in range(weeks)),
    )


def _load_plan(
    busy: Mapping[str, Sequence[float]], weeks: int = 3
) -> tuple[CompiledProblem, Schedule]:
    """One job per (machine, week) with busy seconds > 0: one op at the week's start.

    `busy[m][k]` is machine m's planned busy share of calendar `C` in week k.
    """
    jobs: list[Job] = []
    assignments = []
    for machine_id, shares in busy.items():
        for k, share in enumerate(shares):
            if share <= 0:
                continue
            op_id = f"{machine_id}-w{k}"
            seconds = share * CAPACITY
            jobs.append(job(f"J-{op_id}", op(op_id, machine_id, seconds), release=k * WEEK))
            assignments.append(at(op_id, machine_id, k * WEEK, k * WEEK, k * WEEK + seconds))
    machines = [Machine(id=m, work_center_id="A", calendar_id="C") for m in busy]
    compiled = compile_problem(problem(jobs, machines, calendars=[_weekly_calendar(weeks)]))
    schedule = plan(*assignments)
    assert_verified(compiled, schedule)
    return compiled, schedule


THREE_WEEKS = {"M1": (0.95, 0.50, 0.75), "M2": (0.10, 0.10, 0.10)}


def _load_case(busy: Mapping[str, Sequence[float]]) -> tuple[ShopModel, CompiledProblem, Schedule]:
    compiled, schedule = _load_plan(busy)
    return build_shop(compiled), compiled, schedule


def test_load_fixture_three_week_plan_verifies() -> None:
    """Precondition: the generated three-week plan passes the verifier."""
    _load_case(THREE_WEEKS)


def test_load_check_95_percent_week_1_and_75_percent_week_3_give_two_violations(
    rb: Any,
) -> None:
    shop, _, schedule = _load_case(THREE_WEEKS)
    check = rb.load_check(shop, schedule, (0.9, 0.8, 0.7))
    assert check.weeks == 3
    assert check.violations_total == 2
    found = {(v.machine_id, v.week): (v.load, v.target) for v in check.violations}
    assert set(found) == {("M1", 0), ("M1", 2)}
    assert found["M1", 0] == pytest.approx((0.95, 0.9))
    assert found["M1", 2] == pytest.approx((0.75, 0.7))
    assert check.worst_excess == pytest.approx(0.05, abs=EXACT)
    excesses = [v.load - v.target for v in check.violations]
    assert excesses == sorted(excesses, reverse=True)


def test_load_check_last_target_applies_to_later_weeks(rb: Any) -> None:
    # One target for every week: only week 0 (0.95) exceeds 0.9.
    shop, _, schedule = _load_case(THREE_WEEKS)
    check = rb.load_check(shop, schedule, (0.9,))
    assert [(v.machine_id, v.week, v.target) for v in check.violations] == [("M1", 0, 0.9)]


def test_load_check_load_equal_to_target_is_not_a_violation(rb: Any) -> None:
    shop, _, schedule = _load_case({"M1": (0.9, 0.8, 0.7)})
    check = rb.load_check(shop, schedule, (0.9, 0.8, 0.7))
    assert check.violations_total == 0
    assert check.violations == ()
    assert check.worst_excess == 0.0


def test_load_check_violations_sorted_by_excess_descending(rb: Any) -> None:
    # Excesses: M2 week 1 0.99 - 0.8 = 0.19, M1 week 0 0.95 - 0.9 = 0.05, M1 week 2 0.03.
    shop, _, schedule = _load_case({"M1": (0.95, 0.5, 0.73), "M2": (0.1, 0.99, 0.1)})
    check = rb.load_check(shop, schedule, (0.9, 0.8, 0.7))
    assert [(v.machine_id, v.week) for v in check.violations] == [
        ("M2", 1),
        ("M1", 0),
        ("M1", 2),
    ]
    assert check.worst_excess == pytest.approx(0.19, abs=EXACT)


def test_load_check_lists_at_most_100_violations_and_counts_all(rb: Any) -> None:
    # 101 machines over target 0.5 in week 0; machine i has excess 0.3 + i / 1000.
    busy = {f"M{i:03d}": (0.8 + i / 1000,) for i in range(101)}
    compiled, schedule = _load_plan(busy, weeks=1)
    check = rb.load_check(build_shop(compiled), schedule, (0.5,))
    assert check.weeks == 1
    assert check.violations_total == 101
    assert len(check.violations) == 100
    assert "M000" not in {v.machine_id for v in check.violations}
    assert check.violations[0].machine_id == "M100"
    assert check.worst_excess == pytest.approx(0.4, abs=EXACT)


def test_load_check_clips_an_operation_to_each_week_it_spans(rb: Any) -> None:
    # Calendar [500000, 700000) straddles the week end 604800; the op runs [550000, 650000).
    # Week 0: 54800 busy / 104800 working = 0.523; week 1: 45200 / 95200 = 0.475.
    calendar = Calendar(id="C", intervals=((500_000.0, 700_000.0),))
    compiled = compile_problem(
        problem(
            [job("J", op("O", "M1", 100_000.0))],
            [Machine(id="M1", work_center_id="A", calendar_id="C")],
            calendars=[calendar],
        )
    )
    schedule = plan(at("O", "M1", 550_000.0, 550_000.0, 650_000.0))
    assert_verified(compiled, schedule)
    check = rb.load_check(build_shop(compiled), schedule, (0.5,))
    assert check.weeks == 2
    assert [(v.week, v.load) for v in check.violations] == [(0, pytest.approx(54_800 / 104_800))]
    # Week 1 sits under 0.5; at 0.4 both weeks are violations.
    loads = {
        v.week: v.load for v in rb.load_check(build_shop(compiled), schedule, (0.4,)).violations
    }
    assert loads == pytest.approx({0: 54_800 / 104_800, 1: 45_200 / 95_200})


def test_load_check_weeks_start_at_now_not_at_time_zero(rb: Any) -> None:
    # now = 3.5 days; both working intervals fall in the first week from now:
    # (95000 + 95000) / (100000 + 100000) = 0.95 in week 0, nothing in a week 1.
    now = 302_400.0
    calendar = Calendar(id="C", intervals=((now, now + 100_000.0), (700_000.0, 800_000.0)))
    jobs = [
        job("J1", op("O1", "M1", 95_000.0), release=now),
        job("J2", op("O2", "M1", 95_000.0), release=now),
    ]
    compiled = compile_problem(
        problem(
            jobs,
            [Machine(id="M1", work_center_id="A", calendar_id="C")],
            calendars=[calendar],
            now=now,
        )
    )
    schedule = plan(
        at("O1", "M1", now, now, now + 95_000.0),
        at("O2", "M1", 700_000.0, 700_000.0, 795_000.0),
    )
    assert_verified(compiled, schedule)
    check = rb.load_check(build_shop(compiled), schedule, (0.9, 0.8))
    assert check.weeks == 1
    assert [(v.week, v.load, v.target) for v in check.violations] == [(0, pytest.approx(0.95), 0.9)]


def test_load_check_skips_weeks_without_working_time(rb: Any) -> None:
    # M2's calendar works only in week 2; weeks 0 and 1 have no working time for it.
    calendars = [
        _weekly_calendar(3),
        Calendar(id="C2", intervals=((2 * WEEK, 2 * WEEK + CAPACITY),)),
    ]
    jobs = [
        job("J1", op("O1", "M1", 0.5 * CAPACITY)),
        job("J2", op("O2", "M2", 0.95 * CAPACITY), release=2 * WEEK),
    ]
    machines = [
        Machine(id="M1", work_center_id="A", calendar_id="C"),
        Machine(id="M2", work_center_id="A", calendar_id="C2"),
    ]
    compiled = compile_problem(problem(jobs, machines, calendars=calendars))
    schedule = plan(
        at("O1", "M1", 0.0, 0.0, 0.5 * CAPACITY),
        at("O2", "M2", 2 * WEEK, 2 * WEEK, 2 * WEEK + 0.95 * CAPACITY),
    )
    assert_verified(compiled, schedule)
    check = rb.load_check(build_shop(compiled), schedule, (0.9,))
    assert check.weeks == 3
    assert [(v.machine_id, v.week) for v in check.violations] == [("M2", 2)]


# --- probe machines -------------------------------------------------------------------------


def test_delta_curve_probes_the_top_k_machines_by_planned_busy_share(rb: Any) -> None:
    shop, compiled, schedule = _load_case(THREE_WEEKS)
    one = rb.delta_curve(shop, compiled, schedule, _policy(robustness_deltas_seconds=(60.0, 600.0)))
    assert [(p.delta_seconds, p.machine_id) for p in one] == [(60.0, "M1"), (600.0, "M1")]
    two = rb.delta_curve(
        shop,
        compiled,
        schedule,
        _policy(robustness_deltas_seconds=(60.0, 600.0), robustness_probe_machines=2),
    )
    assert [(p.delta_seconds, p.machine_id) for p in two] == [
        (60.0, "M1"),
        (60.0, "M2"),
        (600.0, "M1"),
        (600.0, "M2"),
    ]


def test_delta_curve_probe_ties_break_by_machine_id(rb: Any) -> None:
    shop, compiled, schedule = _load_case({"Mb": (0.6, 0.6, 0.6), "Ma": (0.6, 0.6, 0.6)})
    points = rb.delta_curve(shop, compiled, schedule, _policy())
    assert [p.machine_id for p in points] == ["Ma"]


def test_delta_curve_never_probes_more_machines_than_the_shop_has(rb: Any) -> None:
    shop, compiled, schedule = _load_case(THREE_WEEKS)
    points = rb.delta_curve(shop, compiled, schedule, _policy(robustness_probe_machines=5))
    assert sorted(p.machine_id for p in points) == ["M1", "M2"]


def test_delta_curve_outage_on_the_bottleneck_delays_its_first_op(rb: Any) -> None:
    # M1's week-0 op (95000 s) starts after a 600 s outage: makespan unchanged (week 2 op
    # ends last), but the first op's job is 600 s later: weighted completion grows by 600.
    shop, compiled, schedule = _load_case(THREE_WEEKS)
    policy = _policy(
        robustness_deltas_seconds=(600.0,), robustness_objective=ObjectiveKind.WEIGHTED_COMPLETION
    )
    (point,) = rb.delta_curve(shop, compiled, schedule, policy)
    assert point.machine_id == "M1"
    assert point.increase == pytest.approx(600.0, abs=EXACT)
    assert point.slope == pytest.approx(1.0, abs=EXACT)
