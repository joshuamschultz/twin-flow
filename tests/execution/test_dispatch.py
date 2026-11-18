"""T-182 (RED): dispatch control, the executor's baseline mode (REQ-138, COMP-135, D-102).

SDD COMP-135: "Dispatch control replaces `units(m)` by a pick: when machine `m` is idle
and up, it takes the best ready, unclaimed operation for which it is an option
(respecting a pin or lock machine) by the rule key, ties by operation id. Idle machines
are woken in machine-id order when the ready set changes. A picked batch-family operation
gathers ready same-family operations eligible on `m`, in rule order, up to
`m.batch.capacity`. Everything after the pick is the plan-control code."

Rule keys (smaller first): `fifo` gate time; `edd` job due (none = +inf); `spt` expected
setup + run on `m`; `critical_ratio` (due - now) / expected remaining work of the job.

API decisions fixed by these tests:

- `DispatchControl(rule)` is importable from `twinflow.execution.engine` and is a
  `Control`: `execute(shop, DispatchControl(rule), draws, outages, limits)` as plan
  control. `rule` is one of `twinflow.policy.DISPATCH_NAMES`.
- An unknown rule raises `ValueError` no later than `sequencer(shop, env)` (the `Control`
  protocol's "raises ValueError on bad input"); the test accepts construction or
  sequencer time.
- One control object may run many replications: each `sequencer` call is a fresh run.
- `OpRecord.planned_start` and `planned_end` are None under dispatch control.
- "Expected remaining work of the job" is tested only where it is unambiguous: single-
  option operations without setups, so it is the sum of `fixed + q x unit` over the job's
  unfinished operations, the candidate included.
- "Expected setup" is the setup function on the machine's actual predecessor (unfactored).
- Batch gathering is tested only where "up to capacity" reads the same whether the first
  member that does not fit stops the gather or is skipped (the over-capacity member is
  the last in rule order).
- The shared path is `Floor.start_unit` in `twinflow.execution._floor`: called exactly
  once per unit under both controls (call-count spy).

Hand-built problems: unit seconds 0 (run = fixed), no setups unless a test says so,
deterministic factors, so every expected time is computed by hand in the comments.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

import pytest
import simpy
from _engine_cases import peak_pool_usage
from _handplans import (
    EXACT,
    a,
    op,
    option,
    override,
    records,
    request,
    run_plan,
    verified_plan,
)
from _plans import generate_mixed

from twinflow.execution import _floor
from twinflow.execution.policy import FactorRule, LogNormal, ScriptedOutage, SimulationPolicy
from twinflow.execution.shop import build_shop
from twinflow.execution.sources import DeterministicSource, DistributionSource, ProfileSource
from twinflow.policy import DISPATCH_NAMES
from twinflow.scheduling import CompiledProblem, SolveRequest, apply_overrides

Data = dict[str, Any]


# --- helpers ---------------------------------------------------------------------------------


def _dispatch_control(rule: str) -> Any:
    """`DispatchControl(rule)`, imported lazily so each test reaches the executor first."""
    from twinflow.execution.engine import DispatchControl

    return DispatchControl(rule)


def run_dispatch(
    compiled: CompiledProblem,
    rule: str,
    *,
    control: Any = None,
    source: DistributionSource | None = None,
    seed: int = 1,
    replication: int = 0,
    outages: Sequence[ScriptedOutage] = (),
) -> Any:
    """Execute the compiled problem under dispatch control (the test_fidelity.py contract)."""
    from twinflow.execution.engine import Limits, RepDraws, execute

    shop = build_shop(compiled)
    return execute(
        shop,
        control if control is not None else _dispatch_control(rule),
        draws=RepDraws(seed=seed, replication=replication, source=source or DeterministicSource()),
        outages=tuple(outages),
        limits=Limits.for_shop(shop),
    )


def start_order(run: Any, machine_id: str | None = None) -> list[str]:
    """Operation ids by (start, id), optionally on one machine."""
    chosen = [r for r in run.records if machine_id is None or r.machine_id == machine_id]
    return [r.operation_id for r in sorted(chosen, key=lambda r: (r.start, r.operation_id))]


def job(job_id: str, *ops: Data, release: float = 0.0, due: float | None = None) -> Data:
    """A job as request data; predecessors are given per op."""
    data: Data = {"id": job_id, "release": release, "operations": list(ops)}
    if due is not None:
        data["due"] = due
    return data


def one_machine(jobs: list[Data], **extra: Any) -> CompiledProblem:
    """The jobs on machine M1 (work center W) and nothing else."""
    req = request(jobs, [{"id": "M1", "work_center_id": "W"}], [{"id": "W"}], **extra)
    return apply_overrides(req)


def blocker() -> Data:
    """Job JZ: op Z on M1, 100 s, released at 0 with no due. At t = 0 it is the only ready
    op, so every rule picks it first and the real choice happens at t = 100 with the whole
    queue ready."""
    return job("JZ", op("Z", "W", [option("M1", 100.0)]))


def assert_back_to_back(run: Any, order: Iterable[str], first_start: float) -> None:
    """The ops ran on one machine in `order`, each starting when the previous one ended."""
    found = records(run)
    t = first_start
    for op_id in order:
        assert abs(found[op_id].start - t) <= EXACT, (op_id, found[op_id].start, t)
        t = found[op_id].end


# --- rule keys on a hand-built queue ---------------------------------------------------------


def _queue() -> CompiledProblem:
    """After Z (0-100), four single-op jobs are ready on M1:

    ===  =======  ===  ===
    op   release  due  run
    ===  =======  ===  ===
    A    10       1000 40
    B    20       300  30
    C    30       900  10
    D    40       500  100
    ===  =======  ===  ===

    fifo (gate): A B C D. edd (due): B D C A. spt (run): C B A D.
    critical_ratio, (due - now) / remaining run:
      t=100: A 900/40=22.5, B 200/30=6.67, C 800/10=80, D 400/100=4    -> D, ends 200
      t=200: A 800/40=20,   B 100/30=3.33, C 700/10=70                 -> B, ends 230
      t=230: A 770/40=19.25,               C 670/10=67                 -> A, then C
    so D B A C. Each rule picks a different first op.
    """
    rows = {"A": (10.0, 1000.0, 40.0), "B": (20.0, 300.0, 30.0), "C": (30.0, 900.0, 10.0)}
    rows["D"] = (40.0, 500.0, 100.0)
    jobs = [blocker()]
    for name, (release, due, run) in rows.items():
        jobs.append(job(f"J{name}", op(name, "W", [option("M1", run)]), release=release, due=due))
    return one_machine(jobs)


QUEUE_ORDERS = {
    "fifo": ["A", "B", "C", "D"],
    "edd": ["B", "D", "C", "A"],
    "spt": ["C", "B", "A", "D"],
    "critical_ratio": ["D", "B", "A", "C"],
}


def test_dispatch_queue_orders_cover_every_rule_name() -> None:
    assert set(QUEUE_ORDERS) == set(DISPATCH_NAMES)
    assert len({tuple(order) for order in QUEUE_ORDERS.values()}) == len(QUEUE_ORDERS)


@pytest.mark.parametrize("rule", DISPATCH_NAMES)
def test_dispatch_rule_picks_by_its_key_on_a_hand_built_queue(rule: str) -> None:
    run = run_dispatch(_queue(), rule)

    assert run.termination == "completed"
    assert start_order(run) == ["Z", *QUEUE_ORDERS[rule]]
    assert_back_to_back(run, ["Z", *QUEUE_ORDERS[rule]], first_start=0.0)


@pytest.mark.parametrize("rule", DISPATCH_NAMES)
def test_dispatch_rule_breaks_ties_by_operation_id(rule: str) -> None:
    # "b" is listed first; both ops have equal gate, due, run and job work: "a" wins.
    twins = [
        job(f"J{name}", op(name, "W", [option("M1", 20.0)]), release=10.0, due=500.0)
        for name in ("b", "a")
    ]
    run = run_dispatch(one_machine([blocker(), *twins]), rule)

    assert start_order(run) == ["Z", "a", "b"]


def test_dispatch_edd_ranks_a_job_without_due_last_and_ties_them_by_id() -> None:
    jobs = [
        blocker(),
        job("JNB", op("n-b", "W", [option("M1", 10.0)]), release=5.0),
        job("JE", op("e", "W", [option("M1", 10.0)]), release=6.0, due=5000.0),
        job("JNA", op("n-a", "W", [option("M1", 10.0)]), release=7.0),
    ]
    run = run_dispatch(one_machine(jobs), "edd")

    assert start_order(run) == ["Z", "e", "n-a", "n-b"]


def test_dispatch_spt_counts_the_setup_from_the_actual_predecessor() -> None:
    # Family matrix on W: A->A 1, A->B 50, B->A 50, B->B 1, default 5 (machine state).
    # Z (family A) runs setup 5 + run 100: 0-105. At 105: S1 (B) = 50 + 10 = 60,
    # S2 (A) = 1 + 40 = 41 -> S2 105/106/146, then S1 146/196/206. Run alone would pick S1.
    matrix = [
        {"from_family": f, "to_family": t, "seconds": s}
        for (f, t), s in {("A", "A"): 1.0, ("A", "B"): 50.0, ("B", "A"): 50.0}.items()
    ] + [{"from_family": "B", "to_family": "B", "seconds": 1.0}]
    jobs = [
        job("JZ", op("Z", "W", [option("M1", 100.0)], setup_family="A")),
        job("J1", op("S1", "W", [option("M1", 10.0)], setup_family="B"), release=1.0),
        job("J2", op("S2", "W", [option("M1", 40.0)], setup_family="A"), release=1.0),
    ]
    req = request(
        jobs,
        [{"id": "M1", "work_center_id": "W"}],
        [{"id": "W", "setup_function_id": "S"}],
        setup_functions=[{"id": "S", "terms": [], "family_matrix": matrix, "default_seconds": 5.0}],
    )
    run = run_dispatch(apply_overrides(req), "spt")

    found = records(run)
    assert start_order(run) == ["Z", "S2", "S1"]
    for op_id, times in {"S2": (105.0, 106.0, 146.0), "S1": (146.0, 196.0, 206.0)}.items():
        got = (found[op_id].start, found[op_id].setup_end, found[op_id].end)
        assert all(abs(g - e) <= EXACT for g, e in zip(got, times, strict=True)), (op_id, got)


def test_dispatch_spt_reads_the_run_on_the_picking_machine() -> None:
    # M2 is busy 0-1000 with W (M2 only). At t = 100 M1 picks: on M1, Y = 20 < X = 30;
    # a key from X's best option anywhere (1 s on M2) would pick X first.
    jobs = [
        blocker(),
        job("JW", op("W2", "W", [option("M2", 1000.0)])),
        job("JX", op("X", "W", [option("M1", 30.0), option("M2", 1.0)]), release=1.0),
        job("JY", op("Y", "W", [option("M1", 20.0), option("M2", 100.0)]), release=1.0),
    ]
    machines = [{"id": "M1", "work_center_id": "W"}, {"id": "M2", "work_center_id": "W"}]
    run = run_dispatch(apply_overrides(request(jobs, machines, [{"id": "W"}])), "spt")

    assert start_order(run, "M1") == ["Z", "Y", "X"]


def test_dispatch_critical_ratio_uses_the_remaining_work_of_the_whole_job() -> None:
    # At t = 100: P's job has P1 (20 on M1) + P2 (180 on M2) left: (500-100)/200 = 2;
    # Q's job has Q1 (40): (400-100)/40 = 7.5 -> P1 first. Op-only work would give
    # P1 400/20 = 20 > 7.5 and pick Q1.
    jobs = [
        blocker(),
        job(
            "JP",
            op("P1", "W", [option("M1", 20.0)]),
            op("P2", "W", [option("M2", 180.0)], predecessors=[{"operation_id": "P1"}]),
            release=1.0,
            due=500.0,
        ),
        job("JQ", op("Q1", "W", [option("M1", 40.0)]), release=1.0, due=400.0),
    ]
    machines = [{"id": "M1", "work_center_id": "W"}, {"id": "M2", "work_center_id": "W"}]
    run = run_dispatch(apply_overrides(request(jobs, machines, [{"id": "W"}])), "critical_ratio")

    assert start_order(run, "M1") == ["Z", "P1", "Q1"]


def test_dispatch_critical_ratio_measures_slack_from_the_pick_time() -> None:
    # Blocker Z2 runs 0-150. At t = 150: X (due 200, work 10) = 50/10 = 5,
    # Y (due 1000, work 100) = 850/100 = 8.5 -> X first. Slack from t = 0 would rank
    # Y (1000/100 = 10) before X (200/10 = 20).
    jobs = [
        job("JZ", op("Z2", "W", [option("M1", 150.0)])),
        job("JX", op("X", "W", [option("M1", 10.0)]), release=1.0, due=200.0),
        job("JY", op("Y", "W", [option("M1", 100.0)]), release=1.0, due=1000.0),
    ]
    run = run_dispatch(one_machine(jobs), "critical_ratio")

    assert start_order(run) == ["Z2", "X", "Y"]


def test_dispatch_critical_ratio_picks_an_overdue_job_first() -> None:
    # At t = 100: L is overdue, (50 - 100)/10 = -5; E (due 300, work 10) = 20 -> L first.
    jobs = [
        blocker(),
        job("JE", op("E", "W", [option("M1", 10.0)]), release=1.0, due=300.0),
        job("JL", op("L", "W", [option("M1", 10.0)]), release=2.0, due=50.0),
    ]
    run = run_dispatch(one_machine(jobs), "critical_ratio")

    assert start_order(run) == ["Z", "L", "E"]


# --- alternates and wake-up order ------------------------------------------------------------

TWO_MACHINES_REVERSED = [{"id": "M2", "work_center_id": "W"}, {"id": "M1", "work_center_id": "W"}]
"""M2 is listed first: machine-id order, not document order, must decide."""


def _both(run_seconds: float) -> list[Data]:
    """Options on M2 and M1 (M2 listed first), each `run_seconds`."""
    return [option("M2", run_seconds), option("M1", run_seconds)]


def test_dispatch_alternate_goes_to_the_first_idle_eligible_machine_by_id() -> None:
    # Both idle at 0: X goes to M1. At 50 Y is released; M1 is busy (0-100), M2 idle -> M2.
    jobs = [
        job("JX", op("X", "W", _both(100.0))),
        job("JY", op("Y", "W", _both(10.0)), release=50.0),
    ]
    run = run_dispatch(apply_overrides(request(jobs, TWO_MACHINES_REVERSED, [{"id": "W"}])), "edd")

    found = records(run)
    assert (found["X"].machine_id, found["X"].start) == ("M1", 0.0)
    assert found["Y"].machine_id == "M2"
    assert abs(found["Y"].start - 50.0) <= EXACT


def test_dispatch_skips_an_idle_machine_that_is_not_an_option() -> None:
    jobs = [job("JX", op("X", "W", [option("M2", 10.0)]))]
    run = run_dispatch(apply_overrides(request(jobs, TWO_MACHINES_REVERSED, [{"id": "W"}])), "edd")

    assert (records(run)["X"].machine_id, records(run)["X"].start) == ("M2", 0.0)


def test_dispatch_wakes_idle_machines_in_id_order_when_the_ready_set_changes() -> None:
    # Nothing is ready before 50; then U (due 100, 30 s), V (due 200, 60 s) and
    # W (due 300, 5 s) appear together. M1 wakes first and takes U (50-80), M2 takes V
    # (50-110); W waits for the first machine free again: M1 at 80.
    jobs = [
        job("JU", op("U", "W", _both(30.0)), release=50.0, due=100.0),
        job("JV", op("V", "W", _both(60.0)), release=50.0, due=200.0),
        job("JW", op("W1", "W", _both(5.0)), release=50.0, due=300.0),
    ]
    run = run_dispatch(apply_overrides(request(jobs, TWO_MACHINES_REVERSED, [{"id": "W"}])), "edd")

    found = records(run)
    assert [(found[x].machine_id, found[x].start) for x in ("U", "V", "W1")] == [
        ("M1", 50.0),
        ("M2", 50.0),
        ("M1", 80.0),
    ]


def test_dispatch_starts_a_successor_on_an_idle_machine_when_its_gate_opens() -> None:
    # P on M1 0-30; S (M2 only) waits for P and starts at 30 on the idle M2.
    # T (M2 only) has material at 70 and M2 is idle again from 40: it starts at 70.
    jobs = [
        job(
            "JP",
            op("P", "W", [option("M1", 30.0)]),
            op("S", "W", [option("M2", 10.0)], predecessors=[{"operation_id": "P"}]),
        ),
        job("JT", op("T", "W", [option("M2", 10.0)], ready_time=70.0)),
    ]
    run = run_dispatch(apply_overrides(request(jobs, TWO_MACHINES_REVERSED, [{"id": "W"}])), "fifo")

    found = records(run)
    assert (found["S"].machine_id, found["T"].machine_id) == ("M2", "M2")
    assert abs(found["S"].start - 30.0) <= EXACT
    assert abs(found["T"].start - 70.0) <= EXACT


# --- pins and hard locks ---------------------------------------------------------------------


def _restricted(how: str) -> SolveRequest:
    """M2 runs W2 0-100 (M2 only). P (M1 or M2, released 5) is pinned or hard-locked to
    M2; Q (M1 or M2, released 5) is free. M1 is idle from 0."""
    p_extra: Data = {"pin": {"machine_id": "M2"}} if how == "pin" else {}
    jobs = [
        job("JW", op("W2", "W", [option("M2", 100.0)])),
        job("JP", op("P", "W", _both(10.0), **p_extra), release=5.0),
        job("JQ", op("Q", "W", _both(10.0)), release=5.0),
    ]
    lock = override("lock-p", "lock", operation_id="P", machine_id="M2")
    overrides = [lock] if how == "lock" else []
    return request(jobs, TWO_MACHINES_REVERSED, [{"id": "W"}], overrides=overrides)


@pytest.mark.parametrize("how", ["pin", "lock"])
def test_dispatch_pin_or_hard_lock_restricts_the_machine(how: str) -> None:
    compiled = apply_overrides(_restricted(how))
    assert not compiled.override_issues, compiled.override_issues
    pinned = next(o for j in compiled.problem.jobs for o in j.operations if o.id == "P")
    assert pinned.pin is not None and pinned.pin.machine_id == "M2"  # precondition

    run = run_dispatch(compiled, "fifo")

    found = records(run)
    assert found["P"].machine_id == "M2"
    assert abs(found["P"].start - 100.0) <= EXACT  # waited for M2, not the idle M1
    assert (found["Q"].machine_id, found["Q"].start) == ("M1", 5.0)


# --- batches ---------------------------------------------------------------------------------


def _batch_request() -> SolveRequest:
    """B1 (capacity 4, run 40) and B2 (capacity 3, run 50), no setups, rule edd.

    ===  ======  ===  =======  ======
    op   family  qty  due      on
    ===  ======  ===  =======  ======
    F1   F       1    100      B1
    F2   F       2    200      B1
    F6   F       1    250      B2
    F3   F       1    300      B1
    F4   F       1    400      B1
    G1   G       1    150      B1
    F5   F       1    50 (released 50)  B1
    ===  ======  ===  =======  ======

    t=0, B1: F1 gathers F2 (load 3), F3 (4); F4 would make 5 -> {F1, F2, F3} 0-40.
         F6 is not eligible on B1, G1 is another family, F5 is not ready.
    t=0, B2: F6 alone 0-50.
    t=40, B1: F4 and G1 ready (F5 not before 50) -> G1 40-80.
    t=80, B1: F5 (due 50) gathers F4 (load 2) -> {F5, F4} 80-120.
    """
    rows = {
        "F1": ("F", 1.0, 100.0, "B1", 0.0),
        "F2": ("F", 2.0, 200.0, "B1", 0.0),
        "F6": ("F", 1.0, 250.0, "B2", 0.0),
        "F3": ("F", 1.0, 300.0, "B1", 0.0),
        "F4": ("F", 1.0, 400.0, "B1", 0.0),
        "G1": ("G", 1.0, 150.0, "B1", 0.0),
        "F5": ("F", 1.0, 50.0, "B1", 50.0),
    }
    run_on = {"B1": 40.0, "B2": 50.0}
    jobs = [
        job(
            f"J{name}",
            op(name, "WB", [option(m, run_on[m])], qty, batch_family=family),
            release=release,
            due=due,
        )
        for name, (family, qty, due, m, release) in rows.items()
    ]
    machines = [
        {"id": "B1", "work_center_id": "WB", "batch": {"capacity": 4.0, "unit": "pc"}},
        {"id": "B2", "work_center_id": "WB", "batch": {"capacity": 3.0, "unit": "pc"}},
    ]
    return request(jobs, machines, [{"id": "WB"}])


def test_dispatch_batch_gathers_ready_same_family_ops_eligible_on_the_machine_up_to_capacity() -> (
    None
):
    run = run_dispatch(apply_overrides(_batch_request()), "edd")

    found = records(run)
    assert run.termination == "completed"
    expected = {
        "F1": ("B1", 0.0, 40.0),
        "F2": ("B1", 0.0, 40.0),
        "F3": ("B1", 0.0, 40.0),
        "F6": ("B2", 0.0, 50.0),
        "G1": ("B1", 40.0, 80.0),
        "F5": ("B1", 80.0, 120.0),
        "F4": ("B1", 80.0, 120.0),
    }
    for op_id, (machine_id, start, end) in expected.items():
        r = found[op_id]
        assert r.machine_id == machine_id, op_id
        assert abs(r.start - start) <= EXACT and abs(r.end - end) <= EXACT, (op_id, r.start, r.end)
    first, second = found["F1"].batch_id, found["F5"].batch_id
    assert first is not None and second is not None and first != second
    assert found["F2"].batch_id == found["F3"].batch_id == first
    assert found["F4"].batch_id == second
    assert found["F6"].batch_id != first


def test_dispatch_batch_load_never_exceeds_capacity() -> None:
    run = run_dispatch(apply_overrides(_batch_request()), "fifo")

    quantities = {"F2": 2.0}
    loads: dict[tuple[str, str], float] = {}
    for r in run.records:
        if r.batch_id is not None:
            key = (r.machine_id, r.batch_id)
            loads[key] = loads.get(key, 0.0) + quantities.get(r.operation_id, 1.0)
    capacity = {"B1": 4.0, "B2": 3.0}
    assert loads, "no batch was formed"
    assert all(load <= capacity[machine] + EXACT for (machine, _), load in loads.items()), loads


# --- in-progress work ------------------------------------------------------------------------


def test_dispatch_keeps_an_in_progress_op_on_its_status_machine_at_now() -> None:
    # now = 10. X (M1 or M2) is in progress on M2 with 30 s left: M2 10-40, no setup.
    # Q (M1 or M2, due 100) takes the idle M1 at 10. R (M2 only, due 50, the best key)
    # must wait for M2 until 40.
    in_progress = {"state": "in_progress", "machine_id": "M2", "remaining_seconds": 30.0}
    jobs = [
        job("JX", op("X", "W", _both(50.0), status=in_progress)),
        job("JQ", op("Q", "W", _both(20.0)), release=10.0, due=100.0),
        job("JR", op("R", "W", [option("M2", 5.0)]), release=10.0, due=50.0),
    ]
    req = request(jobs, TWO_MACHINES_REVERSED, [{"id": "W"}], now=10.0)
    run = run_dispatch(apply_overrides(req), "edd")

    found = records(run)
    x = found["X"]
    assert (x.machine_id, x.setup_seconds) == ("M2", 0.0)
    assert abs(x.start - 10.0) <= EXACT and abs(x.end - 40.0) <= EXACT
    assert (found["Q"].machine_id, found["Q"].start) == ("M1", 10.0)
    assert found["R"].machine_id == "M2"
    assert abs(found["R"].start - 40.0) <= EXACT


# --- a mixed generated problem ---------------------------------------------------------------


@pytest.fixture(scope="module")
def mixed() -> Any:
    """The mixed group (every TF-002 feature) with calendars and operator pools."""
    plan = generate_mixed(1)
    problem = plan.compiled.problem
    assert problem.calendars and problem.operator_pools  # precondition
    return plan


def _outage(compiled: CompiledProblem) -> ScriptedOutage:
    """Machine with the lowest id down 60 s from now + 20."""
    machine_id = min(m.id for m in compiled.problem.machines)
    return ScriptedOutage(machine_id=machine_id, start=compiled.problem.now + 20.0, duration=60.0)


def _assert_unary_machines(run_records: Iterable[Any]) -> None:
    """No machine runs two units at once (batch members share one interval)."""
    units: dict[str, set[tuple[float, float, str]]] = {}
    for r in run_records:
        key = r.batch_id or r.operation_id
        units.setdefault(r.machine_id, set()).add((r.start, r.end, key))
    for machine_id, intervals in units.items():
        ordered = sorted(intervals)
        for (_, end, _), (start, _, _) in zip(ordered, ordered[1:], strict=False):
            assert start >= end - EXACT, (machine_id, ordered)


@pytest.mark.parametrize("rule", DISPATCH_NAMES)
def test_dispatch_completes_every_op_of_a_mixed_problem_with_calendars_operators_and_outage(
    mixed: Any, rule: str
) -> None:
    compiled = mixed.compiled
    shop = build_shop(compiled)

    run = run_dispatch(compiled, rule, outages=(_outage(compiled),))

    assert run.termination == "completed"
    found = records(run)
    assert set(found) == set(shop.operations)
    for op_id, r in found.items():
        shop_op = shop.operations[op_id]
        assert not r.censored, op_id
        assert r.planned_start is None and r.planned_end is None, op_id
        assert r.machine_id in {o.machine_id for o in shop_op.options}, op_id
        if shop_op.pin is not None and shop_op.pin.machine_id is not None:
            assert r.machine_id == shop_op.pin.machine_id, op_id
        if shop_op.status.state == "in_progress":
            assert r.machine_id == shop_op.status.machine_id, op_id
            assert abs(r.start - shop.now) <= EXACT, op_id
    _assert_unary_machines(run.records)
    operations = {o.id: o for j in compiled.problem.jobs for o in j.operations}
    for pool in compiled.problem.operator_pools:
        assert peak_pool_usage(run.records, operations, pool.id) <= pool.size, pool.id


def _lognormal_source() -> ProfileSource:
    policy = SimulationPolicy(
        distribution_source="profile",
        run_factors=(FactorRule(distribution=LogNormal(mean=1.0, cv=0.3)),),
    )
    return ProfileSource(policy)


@pytest.mark.parametrize("rule", DISPATCH_NAMES)
def test_dispatch_run_is_reproducible_with_a_reused_or_fresh_control(mixed: Any, rule: str) -> None:
    compiled = mixed.compiled
    control = _dispatch_control(rule)
    kwargs: Data = {"source": _lognormal_source(), "seed": 7, "replication": 2}

    first = run_dispatch(compiled, rule, control=control, **kwargs)
    again = run_dispatch(compiled, rule, control=control, **kwargs)
    fresh = run_dispatch(compiled, rule, **kwargs)

    assert first.records == again.records == fresh.records
    assert (first.termination, first.events, first.end_time) == (
        fresh.termination,
        fresh.events,
        fresh.end_time,
    )


# --- rule names ------------------------------------------------------------------------------


@pytest.mark.parametrize("rule", ["lifo", "", "EDD", "random"])
def test_dispatch_unknown_rule_raises_value_error(rule: str) -> None:
    shop = build_shop(_queue())
    env = simpy.Environment(initial_time=shop.now)

    with pytest.raises(ValueError, match="rule|dispatch"):
        _dispatch_control(rule).sequencer(shop, env)


def test_every_shared_dispatch_name_has_a_key() -> None:
    from twinflow.execution import _dispatch

    assert set(_dispatch.RULES) == set(DISPATCH_NAMES)


def test_a_rule_added_to_dispatch_names_without_a_key_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A new shared rule name must not silently run as another rule's key.
    from twinflow.execution import _dispatch

    monkeypatch.setattr(_dispatch, "DISPATCH_NAMES", (*DISPATCH_NAMES, "slack"))
    shop = build_shop(_queue())
    env = simpy.Environment(initial_time=shop.now)

    with pytest.raises(ValueError, match="rule|dispatch"):
        _dispatch_control("slack").sequencer(shop, env)


@pytest.mark.parametrize("rule", DISPATCH_NAMES)
def test_dispatch_known_rule_builds_a_sequencer_over_every_machine(rule: str) -> None:
    shop = build_shop(apply_overrides(request([], TWO_MACHINES_REVERSED, [{"id": "W"}])))
    env = simpy.Environment(initial_time=shop.now)

    sequencer = _dispatch_control(rule).sequencer(shop, env)

    assert sequencer.machine_ids() == ("M1", "M2")


# --- one shared path after the pick ----------------------------------------------------------


@pytest.fixture
def start_unit_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[str, ...]]]:
    """Wrap `Floor.start_unit`: record (machine id, member ids) per call, then delegate."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    original: Callable[..., Any] = _floor.Floor.start_unit

    def counting(self: Any, machine: Any, unit: Any) -> Any:
        calls.append((machine.id, tuple(sorted(o.id for o in unit.members))))
        return (yield from original(self, machine, unit))

    monkeypatch.setattr(_floor.Floor, "start_unit", counting)
    return calls


BATCH_UNITS = sorted(
    [("B1", ("F1", "F2", "F3")), ("B2", ("F6",)), ("B1", ("G1",)), ("B1", ("F4", "F5"))]
)


def test_plan_and_dispatch_control_share_start_unit_once_per_unit(
    start_unit_calls: list[tuple[str, tuple[str, ...]]],
) -> None:
    # The verified plan is the dispatch outcome of `_batch_request` under edd, so both
    # controls run the same four units through the one shared function.
    compiled, plan = verified_plan(
        _batch_request(),
        [
            a("F1", "B1", 0.0, 40.0, batch_id="k1"),
            a("F2", "B1", 0.0, 40.0, batch_id="k1"),
            a("F3", "B1", 0.0, 40.0, batch_id="k1"),
            a("F6", "B2", 0.0, 50.0),
            a("G1", "B1", 40.0, 80.0),
            a("F5", "B1", 80.0, 120.0, batch_id="k2"),
            a("F4", "B1", 80.0, 120.0, batch_id="k2"),
        ],
    )
    run_plan(compiled, plan)
    assert sorted(start_unit_calls) == BATCH_UNITS  # plan control: once per unit

    start_unit_calls.clear()
    run = run_dispatch(compiled, "edd")

    assert run.termination == "completed"
    assert sorted(start_unit_calls) == BATCH_UNITS  # dispatch control: the same path


@pytest.mark.parametrize("rule", DISPATCH_NAMES)
def test_dispatch_routes_every_unit_through_start_unit_on_the_mixed_problem(
    mixed: Any, rule: str, start_unit_calls: list[tuple[str, tuple[str, ...]]]
) -> None:
    run = run_dispatch(mixed.compiled, rule)

    started = [op_id for _, members in start_unit_calls for op_id in members]
    assert sorted(started) == sorted(r.operation_id for r in run.records)
    assert len(started) == len(set(started))  # no op started twice
