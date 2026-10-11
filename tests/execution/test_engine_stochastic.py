"""T-180 (RED): stochastic factors, breakdowns, scripted outages and paired-seed invariance.

REQ-132, REQ-133, REQ-134, COMP-130, COMP-132, COMP-135, D-094, D-095. Executor contract:
test_fidelity.py. Worker-count invariance belongs to the evaluator (T-184), not here.

API beyond the test_fidelity.py contract, fixed by these tests:

- `twinflow.execution.engine.failure_times(machine_id, rule, draws, now, until)
  -> tuple[tuple[float, float], ...]`: the machine's (start, duration) failures with
  start < until, in order. D-094 recurrence with k = 0, 1, 2, ... (0-based):
  `t_k = e_k + E_k` where `e_0 = now` and `e_k = t_{k-1} + r_{k-1}`;
  `E_k = -rule.mtbf_seconds * ln(1 - keyed_uniform(seed, rep, SOURCE_FAILURE, f"{m}:{k}"))`
  (the exponential inverse CDF); `r_k = quantile(rule.repair,
  keyed_uniform(seed, rep, SOURCE_REPAIR, f"{m}:{k}"))`. It reads only `draws.seed` and
  `draws.replication`, never a plan, so failures pair across plans by construction.
- `ExecutionRun.downtime_by_machine: Mapping[str, float]`: seconds each machine was down
  (breakdowns and scripted outages) within [now, end_time]; a machine never down may be
  absent (read with `.get(m, 0.0)`).
- `OpRecord.run_seconds` / `setup_seconds` are the drawn work (planned x factor) and
  exclude downtime; `OpRecord.interrupted_seconds` is the downtime while the op was in
  progress, so `end - start == setup_seconds + run_seconds + interrupted_seconds` for an
  op that starts on time on a machine without a calendar.
- A source's scripted outage at index i of `source.outages()` fires when
  `keyed_uniform(seed, rep, SOURCE_OUTAGE, str(i)) <= probability` (boundary inclusive).

The 2,000-failure mean check uses seed 7: with exponential gaps the standard error of the
mean is 1/sqrt(2000) = 2.2 percent, so the 3 percent band of the acceptance holds for some
seeds only (seeds 1 and 2 miss it). The exact recurrence test is what pins the generator;
the mean test guards the distribution family and parameters.
"""

from __future__ import annotations

import math

import pytest
from _handplans import (
    EXACT,
    a,
    op,
    option,
    planned_run,
    records,
    request,
    run_plan,
    verified_plan,
)

from twinflow.engine.rng import (
    SOURCE_FAILURE,
    SOURCE_OUTAGE,
    SOURCE_REPAIR,
    SOURCE_RUN_FACTOR,
    SOURCE_SETUP_FACTOR,
    keyed_uniform,
)
from twinflow.execution.distributions import quantile
from twinflow.execution.policy import (
    BreakdownRule,
    Deterministic,
    Distribution,
    FactorRule,
    LogNormal,
    ScriptedOutage,
    SimulationPolicy,
    Triangular,
)
from twinflow.execution.sources import DeterministicSource, OpContext, ProfileSource

SETUP = 10.0
RUN_DIST = LogNormal(mean=1.0, cv=0.3)
SETUP_DIST = Triangular(low=0.5, mode=1.0, high=2.0)
ANY_CTX = OpContext(work_center_id="W", machine_id="M1", family=None, quantity=1.0)


def _source(
    breakdowns: tuple[BreakdownRule, ...] = (),
    outages: tuple[ScriptedOutage, ...] = (),
    factors: bool = True,
) -> ProfileSource:
    policy = SimulationPolicy(
        distribution_source="profile",
        run_factors=(FactorRule(distribution=RUN_DIST),) if factors else (),
        setup_factors=(FactorRule(distribution=SETUP_DIST),) if factors else (),
        breakdowns=breakdowns,
        outages=outages,
    )
    return ProfileSource(policy)


def _draws(seed: int, replication: int) -> object:
    from twinflow.execution.engine import RepDraws

    return RepDraws(seed=seed, replication=replication, source=DeterministicSource())


def _failure_times(
    machine: str, rule: BreakdownRule, seed: int, rep: int, now: float, until: float
):
    from twinflow.execution.engine import failure_times

    return failure_times(machine, rule, _draws(seed, rep), now, until)


def _reference_failures(
    machine: str, mtbf: float, repair: Distribution, seed: int, rep: int, now: float, count: int
) -> list[tuple[float, float]]:
    """The D-094 recurrence written out independently (see the module docstring)."""
    failures, earliest = [], now
    for k in range(count):
        u_fail = keyed_uniform(seed, rep, SOURCE_FAILURE, f"{machine}:{k}")
        start = earliest - mtbf * math.log(1.0 - u_fail)
        duration = quantile(repair, keyed_uniform(seed, rep, SOURCE_REPAIR, f"{machine}:{k}"))
        failures.append((start, duration))
        earliest = start + duration
    return failures


def _clipped_downtime(failures, end: float) -> float:
    return sum(min(start + duration, end) - start for start, duration in failures if start < end)


# --- factors from the source via keyed quantiles (D-095) -----------------------------------


def _factor_request() -> object:
    jobs = [
        {
            "id": "J1",
            "operations": [
                op("A", "W", [option("M1", 20.0, 5.0)], 2.0),
                op("B", "W", [option("M2", 15.0)], predecessors=[{"operation_id": "A"}]),
            ],
        },
        {"id": "J2", "operations": [op("C", "W", [option("M1", 25.0)])]},
    ]
    return request(
        jobs,
        [{"id": "M1", "work_center_id": "W"}, {"id": "M2", "work_center_id": "W"}],
        [{"id": "W", "setup_function_id": "S"}],
        setup_functions=[{"id": "S", "terms": [], "default_seconds": SETUP}],
    )


# Two different verified plans of one problem: M1 runs A then C, or C then A.
PLAN_AC = [
    a("A", "M1", 0, 40, 10),
    a("C", "M1", 40, 75, 50),
    a("B", "M2", 40, 65, 50),
]
PLAN_CA = [
    a("C", "M1", 0, 35, 10),
    a("A", "M1", 35, 75, 45),
    a("B", "M2", 75, 100, 85),
]


def _run_factor(seed: int, rep: int, op_id: str) -> float:
    return quantile(RUN_DIST, keyed_uniform(seed, rep, SOURCE_RUN_FACTOR, op_id))


def _setup_factor(seed: int, rep: int, op_id: str) -> float:
    return quantile(SETUP_DIST, keyed_uniform(seed, rep, SOURCE_SETUP_FACTOR, op_id))


def test_run_and_setup_seconds_are_planned_times_keyed_quantiles_of_the_source() -> None:
    compiled, plan = verified_plan(_factor_request(), PLAN_AC)
    source, seed, rep = _source(), 13, 4
    assert source.run_factor(ANY_CTX) == RUN_DIST  # precondition: one default rule

    run = run_plan(compiled, plan, source=source, seed=seed, replication=rep)

    assert run.termination == "completed"
    for r in run.records:
        expected_run = planned_run(compiled, r.operation_id, r.machine_id) * _run_factor(
            seed, rep, r.operation_id
        )
        expected_setup = SETUP * _setup_factor(seed, rep, r.operation_id)
        assert r.run_seconds == pytest.approx(expected_run, rel=1e-12), r.operation_id
        assert r.setup_seconds == pytest.approx(expected_setup, rel=1e-12), r.operation_id


def test_draws_are_bit_identical_across_two_plans_of_one_problem() -> None:
    compiled, ac = verified_plan(_factor_request(), PLAN_AC)
    _, ca = verified_plan(_factor_request(), PLAN_CA)
    source = _source()

    first = records(run_plan(compiled, ac, source=source, seed=21, replication=2))
    second = records(run_plan(compiled, ca, source=source, seed=21, replication=2))

    assert first["A"].start != second["A"].start  # precondition: the plans really differ
    for op_id in ("A", "B", "C"):
        assert first[op_id].run_seconds == second[op_id].run_seconds, op_id
        assert first[op_id].setup_seconds == second[op_id].setup_seconds, op_id


@pytest.mark.parametrize(("seed", "replication"), [(22, 2), (21, 3)])
def test_changing_the_seed_or_replication_changes_every_draw(seed: int, replication: int) -> None:
    compiled, plan = verified_plan(_factor_request(), PLAN_AC)
    source = _source()

    base = records(run_plan(compiled, plan, source=source, seed=21, replication=2))
    other = records(run_plan(compiled, plan, source=source, seed=seed, replication=replication))

    for op_id in ("A", "B", "C"):
        assert base[op_id].run_seconds != other[op_id].run_seconds, op_id
        assert base[op_id].setup_seconds != other[op_id].setup_seconds, op_id


# --- breakdowns: clock-time failures with preempt-resume (D-094) -------------------------


def _one_op_request(fixed: float) -> object:
    return request(
        [{"id": "J", "operations": [op("A", "W", [option("M1", fixed)])]}],
        [{"id": "M1", "work_center_id": "W"}],
        [{"id": "W"}],
    )


def test_breakdown_interrupts_the_running_op_and_its_remaining_work_resumes_after_repair() -> None:
    rule = BreakdownRule(machine_id="M1", mtbf_seconds=600.0, repair=Deterministic(value=50.0))
    seed = 4
    (t0, d0), (t1, _) = _reference_failures("M1", 600.0, rule.repair, seed, 0, 0.0, 2)
    fixed = t0 + 100.0  # A is mid-run at the first failure
    assert t1 > fixed + d0  # precondition: no second failure before A ends
    compiled, plan = verified_plan(_one_op_request(fixed), [a("A", "M1", 0, fixed)])

    run = run_plan(compiled, plan, source=_source(breakdowns=(rule,), factors=False), seed=seed)

    r = records(run)["A"]
    assert run.termination == "completed"
    assert r.interrupted_seconds == pytest.approx(50.0, abs=EXACT)
    assert r.run_seconds == pytest.approx(fixed, abs=EXACT)
    assert r.end == pytest.approx(fixed + 50.0, abs=EXACT)
    assert run.downtime_by_machine["M1"] == pytest.approx(50.0, abs=EXACT)


def test_outage_on_an_idle_machine_delays_its_next_op() -> None:
    jobs = [
        {
            "id": "J",
            "operations": [
                op("A", "W", [option("M1", 30.0)]),
                op("B", "W", [option("M2", 10.0)], predecessors=[{"operation_id": "A"}]),
            ],
        }
    ]
    req = request(
        jobs,
        [{"id": "M1", "work_center_id": "W"}, {"id": "M2", "work_center_id": "W"}],
        [{"id": "W"}],
    )
    compiled, plan = verified_plan(req, [a("A", "M1", 0, 30), a("B", "M2", 30, 40)])
    outage = ScriptedOutage(machine_id="M2", start=10.0, duration=30.0)  # M2 idle until 30

    run = run_plan(compiled, plan, outages=[outage])

    found = records(run)
    assert found["B"].start == pytest.approx(40.0, abs=EXACT)
    assert found["B"].end == pytest.approx(50.0, abs=EXACT)
    assert found["B"].interrupted_seconds == 0.0
    assert run.downtime_by_machine["M2"] == pytest.approx(30.0, abs=EXACT)
    assert run.downtime_by_machine.get("M1", 0.0) == 0.0


def test_failure_times_follow_the_d094_recurrence_from_now() -> None:
    repair = LogNormal(mean=120.0, cv=0.5)
    rule = BreakdownRule(machine_id="M1", mtbf_seconds=600.0, repair=repair)
    now, until = 500.0, 50_000.0
    reference = _reference_failures("M1", 600.0, repair, 3, 2, now, 200)
    expected = [f for f in reference if f[0] < until]
    assert len(expected) < len(reference)  # precondition: `until` cuts the reference

    got = _failure_times("M1", rule, 3, 2, now, until)

    assert len(got) == len(expected)
    for (start, duration), (want_start, want_duration) in zip(got, expected, strict=True):
        assert start == pytest.approx(want_start, rel=1e-9)
        assert duration == pytest.approx(want_duration, rel=1e-9)


def test_failure_gaps_and_repairs_over_2000_failures_match_the_rule_within_3_percent() -> None:
    repair = LogNormal(mean=120.0, cv=0.5)
    rule = BreakdownRule(machine_id="M1", mtbf_seconds=600.0, repair=repair)
    until = _reference_failures("M1", 600.0, repair, 7, 0, 0.0, 2001)[-1][0]

    got = _failure_times("M1", rule, 7, 0, 0.0, until)

    assert len(got) == 2000
    ends = [0.0] + [start + duration for start, duration in got[:-1]]
    gaps = [start - end for (start, _), end in zip(got, ends, strict=True)]
    assert sum(gaps) / len(gaps) == pytest.approx(600.0, rel=0.03)
    assert sum(d for _, d in got) / len(got) == pytest.approx(120.0, rel=0.03)


def test_failure_times_change_with_the_seed() -> None:
    rule = BreakdownRule(machine_id="M1", mtbf_seconds=600.0, repair=Deterministic(value=50.0))

    base = _failure_times("M1", rule, 3, 0, 0.0, 20_000.0)
    other = _failure_times("M1", rule, 4, 0, 0.0, 20_000.0)

    assert base and other
    assert base[0][0] != other[0][0]


# --- scripted outages fire on their keyed draw ---------------------------------------------


def _outage_plan() -> tuple:
    jobs = [
        {"id": "J1", "operations": [op("A", "W", [option("M1", 30.0)])]},
        {"id": "J2", "operations": [op("B", "W", [option("M2", 30.0)])]},
    ]
    req = request(
        jobs,
        [{"id": "M1", "work_center_id": "W"}, {"id": "M2", "work_center_id": "W"}],
        [{"id": "W"}],
    )
    return verified_plan(req, [a("A", "M1", 0, 30), a("B", "M2", 0, 30)])


@pytest.mark.parametrize("fires", [True, False])
def test_scripted_outage_fires_when_its_keyed_draw_is_at_most_the_probability(fires: bool) -> None:
    seed, rep = 5, 1
    draws = [keyed_uniform(seed, rep, SOURCE_OUTAGE, str(i)) for i in (0, 1)]
    probabilities = draws if fires else [math.nextafter(u, 0.0) for u in draws]
    outages = tuple(
        ScriptedOutage(machine_id=m, start=10.0, duration=5.0, probability=p)
        for m, p in zip(("M1", "M2"), probabilities, strict=True)
    )
    compiled, plan = _outage_plan()

    run = run_plan(
        compiled, plan, source=_source(outages=outages, factors=False), seed=seed, replication=rep
    )

    found = records(run)
    expected_down, expected_end = (5.0, 35.0) if fires else (0.0, 30.0)
    for op_id, machine in (("A", "M1"), ("B", "M2")):
        assert run.downtime_by_machine.get(machine, 0.0) == pytest.approx(expected_down, abs=EXACT)
        assert found[op_id].end == pytest.approx(expected_end, abs=EXACT)


# --- paired seeds with breakdowns (REQ-134) and the former guard "no breakdowns" ----------


def test_two_plans_pair_on_factors_and_failure_times_with_breakdowns() -> None:
    compiled, ac = verified_plan(_factor_request(), PLAN_AC)
    _, ca = verified_plan(_factor_request(), PLAN_CA)
    rule = BreakdownRule(machine_id="M1", mtbf_seconds=60.0, repair=Deterministic(value=5.0))
    source = _source(breakdowns=(rule,))

    runs = [run_plan(compiled, p, source=source, seed=31, replication=1) for p in (ac, ca)]

    first, second = (records(run) for run in runs)
    for op_id in ("A", "B", "C"):
        assert first[op_id].run_seconds == second[op_id].run_seconds, op_id
        assert first[op_id].setup_seconds == second[op_id].setup_seconds, op_id
    for run in runs:
        failures = _failure_times("M1", rule, 31, 1, 0.0, run.end_time)
        assert failures  # precondition: M1 fails inside the run
        down = run.downtime_by_machine.get("M1", 0.0)
        assert down == pytest.approx(_clipped_downtime(failures, run.end_time), abs=EXACT)
        assert run.downtime_by_machine.get("M2", 0.0) == 0.0


def test_former_guard_no_breakdowns_plan_on_a_failing_machine_completes_with_downtime() -> None:
    rule = BreakdownRule(machine_id="M1", mtbf_seconds=60.0, repair=Deterministic(value=10.0))
    compiled, plan = verified_plan(_one_op_request(1000.0), [a("A", "M1", 0, 1000)])

    run = run_plan(compiled, plan, source=_source(breakdowns=(rule,), factors=False), seed=9)

    r = records(run)["A"]
    down = run.downtime_by_machine.get("M1", 0.0)
    assert run.termination == "completed"
    assert not r.censored
    assert down > 0.0
    assert r.interrupted_seconds == pytest.approx(down, abs=EXACT)
    assert r.end - r.start == pytest.approx(1000.0 + r.interrupted_seconds, abs=EXACT)
    failures = _failure_times("M1", rule, 9, 0, 0.0, run.end_time)
    assert down == pytest.approx(_clipped_downtime(failures, run.end_time), abs=EXACT)
