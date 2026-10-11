"""Replay fidelity: a verified plan executes exactly as planned (T-172, REQ-131, COMP-142).

With the deterministic distribution source and no outages, executing a verified plan under
plan control must reproduce, for every operation, the planned start, setup end and end
within 1e-6 s. The suite proves it on 20 generated plans per feature group
(`_plans.py`), each group checked first to really exercise its features
(`_plan_evidence.REQUIRED`), so a green fidelity test is never vacuous.

Executor contract this suite pins (COMP-134, COMP-135, COMP-136; T-173 implements it)::

    from twinflow.execution.shop import build_shop
    from twinflow.execution.engine import Limits, PlanControl, RepDraws, execute
    from twinflow.execution.sources import DeterministicSource

    shop = build_shop(compiled)                       # CompiledProblem -> ShopModel
    run = execute(
        shop,
        PlanControl(schedule),                        # a verified TF-002 Schedule
        draws=RepDraws(seed=1, replication=0, source=DeterministicSource()),
        outages=(),                                   # scripted outages (COMP-137 probes)
        limits=Limits.for_shop(shop),                 # event cap 50 x ops + 10,000; stop at T_c
    )

* `execute` returns `ExecutionRun{records: tuple[OpRecord, ...], downtime_by_machine,
  termination, events, end_time}`. `termination` is `"completed"` when every operation
  finished, else `"event_limit"` or `"calendar_end"` (censored ops).
* `OpRecord` carries at least `operation_id, job_id, machine_id, batch_id, planned_start,
  planned_end, start, setup_end, end, setup_seconds, run_seconds, censored` (full field
  list: COMP-136). Times are problem seconds (the SimPy clock starts at `problem.now`).
* There is exactly one record per assignment of the schedule: no record for an operation
  of a held or done job or for an operation in state `done`; an in-progress operation's
  record is `[now, now + remaining)` with no setup.

Groups whose executor code is not built yet are listed in `PENDING_GROUPS` and marked
`xfail(strict=True)`: their fidelity test must fail until the task that builds the group
removes it (T-173 core, T-174 calendars, T-175 setups and restart, T-176 operators, T-177
precedence, T-178 batches, T-179 state and mixed). The execution modules are imported
inside the tests, so the generator tests in this file run before `twinflow.execution`
exists.
"""

from __future__ import annotations

import functools
from collections import Counter
from typing import Any

import pytest
from _plan_evidence import REQUIRED, evidence
from _plans import (
    BASELINE_GROUPS,
    GENERATORS,
    GeneratedPlan,
    Tf003Unavailable,
    reverified,
    tf003_plans,
)

from twinflow.application.portfolio_port import load_portfolio_api
from twinflow.scheduling import Feature, problem_features

SEEDS = range(20)
FIDELITY_TOLERANCE = 1e-6
PENDING_GROUPS: frozenset[str] = frozenset()
"""Each green task removes its group; T-179 leaves this empty."""

TF003_SKIP = "TF-003 portfolio api not installed yet"

FEATURES: dict[str, frozenset[Feature]] = {
    "core": frozenset({Feature.ALTERNATIVES, Feature.MACHINE_STATE}),
    "calendars": frozenset({Feature.CALENDARS, Feature.PAUSE, Feature.UNATTENDED}),
    "setups": frozenset({Feature.SETUPS, Feature.DETACHED_SETUP, Feature.MACHINE_STATE}),
    "operators": frozenset({Feature.OPERATORS, Feature.CALENDARS, Feature.SETUPS}),
    "batches": frozenset({Feature.BATCHING, Feature.SETUPS}),
    "precedence": frozenset({Feature.LAGS, Feature.MAX_LAGS, Feature.OVERLAP, Feature.PINS}),
    "state": frozenset({Feature.IN_PROGRESS, Feature.PINS, Feature.OPERATORS}),
    "restart": frozenset({Feature.RESTART}),
}
FEATURES["mixed"] = frozenset().union(*(FEATURES[g] for g in BASELINE_GROUPS if g != "mixed"))
"""Features `problem_features` must report across a group's 20 compiled problems."""


@functools.cache
def _plan(group: str, seed: int) -> GeneratedPlan:
    try:
        return GENERATORS[group](seed)
    except Tf003Unavailable:
        pytest.skip(TF003_SKIP)


def _pending(groups: tuple[str, ...]) -> list[Any]:
    """One param per group; groups not built yet are strict xfails."""
    mark = pytest.mark.xfail(strict=True, reason="executor for this group not built yet")
    return [pytest.param(g, marks=mark) if g in PENDING_GROUPS else g for g in groups]


# --- the generators (scheduling package only; pass now) --------------------------------------


@pytest.mark.parametrize("group", BASELINE_GROUPS)
def test_generator_produces_verified_baseline_plans_for_every_seed(group: str) -> None:
    for seed in SEEDS:
        plan = _plan(group, seed)
        assert plan.method == "baseline", (group, seed)
        assert plan.schedule.status in {"FEASIBLE", "OPTIMAL"}, (group, seed)
        assert plan.schedule.assignments, (group, seed)
        assert reverified(plan.request, plan.compiled, plan.schedule), (group, seed)


@pytest.mark.parametrize("group", BASELINE_GROUPS)
def test_generator_is_deterministic(group: str) -> None:
    first, second = GENERATORS[group](3), GENERATORS[group](3)
    assert first.request == second.request
    assert first.schedule.assignments == second.schedule.assignments


@pytest.mark.parametrize("group", BASELINE_GROUPS)
def test_generator_problems_use_their_group_features(group: str) -> None:
    used: set[Feature] = set()
    for seed in SEEDS:
        used |= problem_features(_plan(group, seed).compiled.problem)
    assert FEATURES[group] <= used, (group, sorted(FEATURES[group] - used))


@pytest.mark.parametrize("group", BASELINE_GROUPS)
def test_generator_plans_exercise_every_group_feature_across_seeds(group: str) -> None:
    seen: Counter[str] = Counter()
    for seed in SEEDS:
        seen.update(evidence(_plan(group, seed)))
    missing = sorted(REQUIRED[group] - set(seen))
    assert not missing, f"{group}: no seed shows {missing}"


def test_core_group_has_no_calendars_setups_or_operators() -> None:
    excluded = {Feature.CALENDARS, Feature.SETUPS, Feature.OPERATORS, Feature.BATCHING}
    for seed in SEEDS:
        assert not excluded & problem_features(_plan("core", seed).compiled.problem), seed


def test_restart_generator_produces_verified_tf003_plans() -> None:
    if load_portfolio_api() is None:
        pytest.skip(TF003_SKIP)
    seen: Counter[str] = Counter()
    for seed in SEEDS:
        plan = _plan("restart", seed)
        assert reverified(plan.request, plan.compiled, plan.schedule), seed
        assert FEATURES["restart"] <= problem_features(plan.compiled.problem), seed
        seen.update(evidence(plan))
    assert REQUIRED["restart"] <= set(seen)


# --- fidelity (REQ-131) --------------------------------------------------------------------------


def _execute(plan: GeneratedPlan) -> Any:
    """Run `plan` deterministically under plan control (the contract in the docstring)."""
    from twinflow.execution.engine import Limits, PlanControl, RepDraws, execute
    from twinflow.execution.shop import build_shop
    from twinflow.execution.sources import DeterministicSource

    shop = build_shop(plan.compiled)
    return execute(
        shop,
        PlanControl(plan.schedule),
        draws=RepDraws(seed=1, replication=0, source=DeterministicSource()),
        outages=(),
        limits=Limits.for_shop(shop),
    )


def _assert_replays_exactly(plan: GeneratedPlan) -> None:
    label = f"{plan.group} seed {plan.seed} attempt {plan.attempt} ({plan.method})"
    run = _execute(plan)
    assert run.termination == "completed", label
    records = {r.operation_id: r for r in run.records}
    assert len(records) == len(run.records), f"{label}: an operation recorded twice"
    planned = {a.operation_id: a for a in plan.schedule.assignments}
    assert set(records) == set(planned), f"{label}: records differ from the plan's operations"
    for op_id, a in planned.items():
        r = records[op_id]
        assert r.machine_id == a.machine_id, f"{label} {op_id}: machine"
        for name in ("start", "setup_end", "end"):
            simulated, expected = getattr(r, name), getattr(a, name)
            assert abs(simulated - expected) <= FIDELITY_TOLERANCE, (
                f"{label} {op_id}: {name} simulated {simulated!r}, planned {expected!r}"
            )


@pytest.mark.parametrize("group", _pending(BASELINE_GROUPS))
def test_deterministic_execution_reproduces_every_baseline_plan(group: str) -> None:
    for seed in SEEDS:
        _assert_replays_exactly(_plan(group, seed))


@pytest.mark.parametrize("group", _pending(BASELINE_GROUPS))
def test_deterministic_execution_reproduces_every_tf003_plan(group: str) -> None:
    if load_portfolio_api() is None:
        pytest.skip(TF003_SKIP)
    for seed in SEEDS:
        for plan in tf003_plans(_plan(group, seed)):
            _assert_replays_exactly(plan)


@pytest.mark.parametrize("group", _pending(("restart",)))
def test_deterministic_execution_reproduces_every_restart_plan(group: str) -> None:
    if load_portfolio_api() is None:
        pytest.skip(TF003_SKIP)
    for seed in SEEDS:
        _assert_replays_exactly(_plan(group, seed))
