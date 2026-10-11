"""T-178 (RED): batches execute as one unit (REQ-130, REQ-131, COMP-135).

Members of one `(machine, batch_id)` start together after every member's gate, share one
setup and one run draw, end together, stay within the machine's capacity, and the next
unit's setup is measured from the batch the way the verifier measures it
(`verifier._setup_predecessors`: the last member in `(start, end, operation_id)` order).
Executor contract: test_fidelity.py.

API beyond the test_fidelity.py contract, fixed by these tests:

- `OpRecord.batch_id` is the plan's `batch_id`; `OpRecord.setup_seconds` and
  `OpRecord.run_seconds` are the drawn seconds (planned x factor), excluding downtime.
- A batch's run factor is `quantile(source.run_factor(ctx), keyed_uniform(seed, rep,
  SOURCE_RUN_FACTOR, lowest member id))` and its setup factor the same with
  `SOURCE_SETUP_FACTOR` and the same key ("lowest" = smallest id string, the verifier's
  id order). The task names the run draw; the setup key is read the same way.

Shop: M1 (work center WA, no setup) and batch machine B1 (WB, capacity 4) with a family
matrix A->A 1, A->B 9, B->A 4, B->B 2, default 6 and no state family, so the batch's setup
from the machine state is the default 6 for every member.
"""

from __future__ import annotations

import pytest
from _handplans import (
    EXACT,
    StubSource,
    a,
    assert_replays,
    op,
    option,
    records,
    request,
    run_plan,
    verified_plan,
)

from twinflow.engine.rng import SOURCE_RUN_FACTOR, SOURCE_SETUP_FACTOR, keyed_uniform
from twinflow.execution.distributions import quantile
from twinflow.execution.policy import FactorRule, LogNormal, SimulationPolicy, Triangular
from twinflow.execution.sources import OpContext, ProfileSource

CAPACITY = 4.0
MATRIX = [
    {"from_family": f, "to_family": t, "seconds": s}
    for (f, t), s in {("A", "A"): 1.0, ("A", "B"): 9.0, ("B", "A"): 4.0, ("B", "B"): 2.0}.items()
]


def _request() -> object:
    # Document order puts "b-op" before "a-op": the lowest id is not the first listed.
    x1 = op("X1", "WA", [option("M1", 10.0)])
    b_op = op(
        "b-op",
        "WB",
        [option("B1", 40.0)],
        1.0,
        batch_family="F",
        setup_family="B",
        predecessors=[{"operation_id": "X1"}],
    )
    a_op = op("a-op", "WB", [option("B1", 40.0)], 2.0, batch_family="F", setup_family="A")
    q = op("Q", "WB", [option("B1", 30.0)], 1.0, batch_family="F", setup_family="A")
    return request(
        [
            {"id": "J1", "operations": [x1, b_op]},
            {"id": "J2", "operations": [a_op]},
            {"id": "J3", "operations": [q]},
        ],
        [
            {"id": "M1", "work_center_id": "WA"},
            {"id": "B1", "work_center_id": "WB", "batch": {"capacity": CAPACITY, "unit": "pc"}},
        ],
        [{"id": "WA"}, {"id": "WB", "setup_function_id": "SB"}],
        setup_functions=[
            {"id": "SB", "terms": [], "family_matrix": MATRIX, "default_seconds": 6.0}
        ],
    )


# Batch b1 = {a-op, b-op} after X1: setup 6 (state), run 40. Q follows the batch: its setup
# predecessor is b-op (last by id), family B -> A = 4 (a-op would give A -> A = 1).
PLAN = [
    a("X1", "M1", 0, 10),
    a("a-op", "B1", 10, 56, setup_end=16, batch_id="b1"),
    a("b-op", "B1", 10, 56, setup_end=16, batch_id="b1"),
    a("Q", "B1", 56, 90, setup_end=60),
]
MEMBERS = ("a-op", "b-op")


def _plan() -> tuple:
    return verified_plan(_request(), PLAN)


def test_former_guard_batch_pull_rule_batched_plan_replays_exactly() -> None:
    compiled, plan = _plan()

    run = run_plan(compiled, plan)

    assert_replays(run, plan)
    found = records(run)
    assert {found[m].batch_id for m in MEMBERS} == {"b1"}


def test_batch_members_start_together_after_every_members_gate() -> None:
    compiled, plan = _plan()

    # X1 slips to 20; only b-op waits on it, but a-op starts with its batch.
    found = records(run_plan(compiled, plan, source=StubSource(run={"M1": 2.0})))

    for member in MEMBERS:
        assert found[member].start == pytest.approx(20.0, abs=EXACT), member
        assert found[member].setup_end == pytest.approx(26.0, abs=EXACT), member
        assert found[member].end == pytest.approx(66.0, abs=EXACT), member


def test_batch_has_one_setup_and_the_next_unit_measures_setup_from_the_batch() -> None:
    compiled, plan = _plan()

    found = records(run_plan(compiled, plan))

    assert [found[m].setup_seconds for m in MEMBERS] == [6.0, 6.0]
    assert found["Q"].setup_seconds == pytest.approx(4.0, abs=EXACT)
    assert found["Q"].setup_end == pytest.approx(found["Q"].start + 4.0, abs=EXACT)


def _profile_source() -> ProfileSource:
    policy = SimulationPolicy(
        distribution_source="profile",
        run_factors=(FactorRule(distribution=LogNormal(mean=1.0, cv=0.4)),),
        setup_factors=(FactorRule(distribution=Triangular(low=0.5, mode=1.0, high=2.0)),),
    )
    return ProfileSource(policy)


def test_batch_shares_one_run_and_setup_draw_keyed_by_the_lowest_member_id() -> None:
    compiled, plan = _plan()
    source, seed, rep = _profile_source(), 11, 3
    ctx = OpContext(work_center_id="WB", machine_id="B1", family="A", quantity=2.0)
    run_dist, setup_dist = source.run_factor(ctx), source.setup_factor(ctx)
    lowest_run = quantile(run_dist, keyed_uniform(seed, rep, SOURCE_RUN_FACTOR, "a-op"))
    other_run = quantile(run_dist, keyed_uniform(seed, rep, SOURCE_RUN_FACTOR, "b-op"))
    lowest_setup = quantile(setup_dist, keyed_uniform(seed, rep, SOURCE_SETUP_FACTOR, "a-op"))
    assert lowest_run != other_run  # precondition: the key is observable

    run = run_plan(compiled, plan, source=source, seed=seed, replication=rep)

    found = records(run)
    for member in MEMBERS:
        assert found[member].run_seconds == pytest.approx(40.0 * lowest_run, rel=1e-12), member
        assert found[member].setup_seconds == pytest.approx(6.0 * lowest_setup, rel=1e-12), member
    first, second = (found[m] for m in MEMBERS)
    assert (first.start, first.setup_end, first.end) == (second.start, second.setup_end, second.end)


def test_batch_load_stays_within_machine_capacity() -> None:
    compiled, plan = _plan()
    quantity = {o.id: o.quantity for j in compiled.problem.jobs for o in j.operations}

    run = run_plan(compiled, plan)

    loads: dict[tuple[str, str], float] = {}
    for r in run.records:
        if r.batch_id is not None:
            key = (r.machine_id, r.batch_id)
            loads[key] = loads.get(key, 0.0) + quantity[r.operation_id]
    assert loads == {("B1", "b1"): 3.0}
    assert all(load <= CAPACITY for load in loads.values())
