"""T-182: dispatch-control gaps closed by orchestrator decisions (COMP-135, D-102).

The RED suite (test_dispatch.py) left these readings open; each test pins one decision:

- batch gathering is first fit in rule order (an op that would exceed the remaining
  capacity is skipped, a smaller later one still joins);
- critical ratio work = sum over the job's unfinished ops of the minimum planned run over
  their options, setups excluded; no due goes last, ties by op id;
- a pin's earliest start is an extra gate; `hold until` acts through the job release;
- operators are requested by (pick time, op id);
- a machine not yet available (`available_from`) picks when it frees, not at now.

Hand-built problems: unit seconds 0 (run = fixed), no setups unless a test says so,
deterministic factors.
"""

from __future__ import annotations

from typing import Any

from _handplans import EXACT, op, option, override, records, request

from twinflow.execution.engine import DispatchControl, Limits, RepDraws, execute
from twinflow.execution.shop import build_shop
from twinflow.execution.sources import DeterministicSource
from twinflow.scheduling import CompiledProblem, SolveRequest, apply_overrides

Data = dict[str, Any]
ONE_MACHINE = [{"id": "M1", "work_center_id": "W"}]
TWO_MACHINES = [{"id": "M1", "work_center_id": "W"}, {"id": "M2", "work_center_id": "W"}]


def _job(job_id: str, *ops: Data, release: float = 0.0, due: float | None = None) -> Data:
    data: Data = {"id": job_id, "release": release, "operations": list(ops)}
    if due is not None:
        data["due"] = due
    return data


def _run(req: SolveRequest | CompiledProblem, rule: str) -> Any:
    compiled = req if isinstance(req, CompiledProblem) else apply_overrides(req)
    shop = build_shop(compiled)
    return execute(
        shop,
        DispatchControl(rule),
        draws=RepDraws(seed=1, replication=0, source=DeterministicSource()),
        outages=(),
        limits=Limits.for_shop(shop),
    )


def _starts(run: Any, machine_id: str | None = None) -> list[str]:
    chosen = [r for r in run.records if machine_id is None or r.machine_id == machine_id]
    return [r.operation_id for r in sorted(chosen, key=lambda r: (r.start, r.operation_id))]


def _blocker(seconds: float = 100.0) -> Data:
    """Job JZ: op Z on M1 from 0, so the real pick happens when Z ends."""
    return _job("JZ", op("Z", "W", [option("M1", seconds)]))


def test_batch_gather_is_first_fit_skipping_an_op_that_does_not_fit() -> None:
    # Capacity 4, edd. At 0: A (qty 2, due 10) picks; B (qty 3, due 20) would make 5 and is
    # skipped; C (qty 2, due 30) still fits -> {A, C} 0-40, then B 40-80.
    rows = {"A": (2.0, 10.0), "B": (3.0, 20.0), "C": (2.0, 30.0)}
    jobs = [
        _job(f"J{name}", op(name, "W", [option("M1", 40.0)], qty, batch_family="F"), due=due)
        for name, (qty, due) in rows.items()
    ]
    machines = [{"id": "M1", "work_center_id": "W", "batch": {"capacity": 4.0, "unit": "pc"}}]
    run = _run(request(jobs, machines, [{"id": "W"}]), "edd")

    found = records(run)
    assert found["A"].batch_id is not None
    assert found["C"].batch_id == found["A"].batch_id
    assert (found["A"].start, found["C"].start, found["A"].end) == (0.0, 0.0, 40.0)
    assert abs(found["B"].start - 40.0) <= EXACT and found["B"].batch_id != found["A"].batch_id


def test_critical_ratio_work_is_the_minimum_planned_run_over_options() -> None:
    # At t = 100: Y (100 on M1, due 1000) = 900 / 100 = 9. X (pinned to M1: 400 there,
    # 100 on M2, due 1100) has work min(400, 100) = 100: 1000 / 100 = 10 -> Y first.
    # Work from the picking machine's run (400) would give X 2.5 and pick X first.
    jobs = [
        _blocker(),
        _job("JY", op("Y", "W", [option("M1", 100.0)]), release=1.0, due=1000.0),
        _job(
            "JX",
            op("X", "W", [option("M1", 400.0), option("M2", 100.0)], pin={"machine_id": "M1"}),
            release=1.0,
            due=1100.0,
        ),
    ]
    run = _run(request(jobs, TWO_MACHINES, [{"id": "W"}]), "critical_ratio")

    assert _starts(run, "M1") == ["Z", "Y", "X"]


def test_critical_ratio_work_excludes_setups() -> None:
    # 500 s setup before every op. Z ends at 600. A (run 100, due 1500) = 900 / 100 = 9;
    # B (run 1000, due 9100) = 8500 / 1000 = 8.5 -> B first. With setups in the work A
    # would be 900 / 600 = 1.5 and B 8500 / 1500 = 5.7, picking A first.
    jobs = [
        _blocker(),
        _job("JA", op("A", "W", [option("M1", 100.0)]), release=1.0, due=1500.0),
        _job("JB", op("B", "W", [option("M1", 1000.0)]), release=1.0, due=9100.0),
    ]
    setup = [{"id": "S", "terms": [], "default_seconds": 500.0}]
    centers = [{"id": "W", "setup_function_id": "S"}]
    run = _run(request(jobs, ONE_MACHINE, centers, setup_functions=setup), "critical_ratio")

    assert _starts(run) == ["Z", "B", "A"]


def test_critical_ratio_ranks_a_job_without_due_last_and_ties_by_id() -> None:
    jobs = [
        _blocker(),
        _job("JNB", op("n-b", "W", [option("M1", 10.0)]), release=1.0),
        _job("JNA", op("n-a", "W", [option("M1", 10.0)]), release=1.0),
        _job("JE", op("e", "W", [option("M1", 10.0)]), release=1.0, due=10_000.0),
    ]
    run = _run(request(jobs, ONE_MACHINE, [{"id": "W"}]), "critical_ratio")

    assert _starts(run) == ["Z", "e", "n-a", "n-b"]


def test_pin_earliest_start_gates_the_pick() -> None:
    # P (due 10, best edd key) is pinned no earlier than 50; Q (due 100) is ready at 0.
    # M1 takes Q at 0 (0-30); P waits for 50, not for a free M1 at 30.
    jobs = [
        _job("JP", op("P", "W", [option("M1", 10.0)], pin={"earliest_start": 50.0}), due=10.0),
        _job("JQ", op("Q", "W", [option("M1", 30.0)]), due=100.0),
    ]
    run = _run(request(jobs, ONE_MACHINE, [{"id": "W"}]), "edd")

    found = records(run)
    assert found["Q"].start == 0.0
    assert abs(found["P"].start - 50.0) <= EXACT


def test_hold_until_delays_the_pick_through_the_job_release() -> None:
    # H (due 10) is held until 60; R (due 100, 30 s) runs 0-30; H starts at 60.
    jobs = [
        _job("JH", op("H", "W", [option("M1", 10.0)]), due=10.0),
        _job("JR", op("R", "W", [option("M1", 30.0)]), due=100.0),
    ]
    hold = override("hold-h", "hold", job_id="JH", until=60.0)
    run = _run(request(jobs, ONE_MACHINE, [{"id": "W"}], overrides=[hold]), "edd")

    found = records(run)
    assert found["R"].start == 0.0
    assert abs(found["H"].start - 60.0) <= EXACT


def test_operators_go_by_pick_time_then_op_id() -> None:
    # Rule edd. Pool P has one operator, held by c on M3 over 0-10. M2 runs d (due 1, no
    # operator) 0-5.
    # b (M1, released 2) is picked at 2; a (M2, released 0) is picked at 5 when M2 frees.
    # At 10 the operator goes to b, picked first: (2, "b") < (5, "a"); a gate-time or
    # id priority would serve a first.
    need = [{"pool_id": "P", "quantity": 1, "phase": "both"}]
    jobs = [
        _job("JC", op("c", "W", [option("M3", 10.0)], operator_needs=need)),
        _job("JD", op("d", "W", [option("M2", 5.0)]), due=1.0),
        _job("JB", op("b", "W", [option("M1", 20.0)], operator_needs=need), release=2.0, due=9.0),
        _job("JA", op("a", "W", [option("M2", 20.0)], operator_needs=need), due=8.0),
    ]
    machines = [*TWO_MACHINES, {"id": "M3", "work_center_id": "W"}]
    pools = [{"id": "P", "size": 1}]
    run = _run(request(jobs, machines, [{"id": "W"}], pools=pools), "edd")

    found = records(run)
    assert abs(found["b"].start - 10.0) <= EXACT
    assert abs(found["a"].start - 30.0) <= EXACT


def test_a_machine_not_yet_available_picks_when_it_frees() -> None:
    # M1 is available from 100. X (due 500) is ready at 0, Y (due 100) at 50: at 100 the
    # edd pick sees both and takes Y; a pick at 0 would have committed to X.
    machines = [{"id": "M1", "work_center_id": "W", "state": {"available_from": 100.0}}]
    jobs = [
        _job("JX", op("X", "W", [option("M1", 10.0)]), due=500.0),
        _job("JY", op("Y", "W", [option("M1", 10.0)]), release=50.0, due=100.0),
    ]
    run = _run(request(jobs, machines, [{"id": "W"}]), "edd")

    assert _starts(run) == ["Y", "X"]
    assert abs(records(run)["Y"].start - 100.0) <= EXACT
