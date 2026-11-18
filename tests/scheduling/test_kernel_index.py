"""Problem index and instance statistics (T-120, COMP-003, REQ-093).

`ProblemIndex.build(problem, phi=2.0)` turns a compiled problem into plain values keyed by
op id: only scheduled ops (not done, of open jobs), in problem order; per op its job,
material release max(job.release, op.ready_time), eligible options (the pinned machine
only when a pin names one, the status machine only for an in-progress op; machine id
order; run = fixed + quantity * unit), scheduled predecessor links, work center,
`remaining_shortest_run` (sum over the job's later ops of their shortest eligible run) and
`local_due` = due - phi * remaining_shortest_run (None without a due). Per job: effective
weight (`weight_override`, else the default class weight 1/2/4) and due.

`InstanceStats.for_center(index, center_id, now)` follows the SDD ATCS pseudo-code. The
14.2.1 numbers come from the PLAN (recomputed by brute force); sbar = 2.0 only when the
machine-state pairs (l = state) are in the mean (23/12 = 1.917 without them), so the
state pairs are part of the contract.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from generators import random_request, scale_request
from kernel_cases import compile_request, load_request, machine, op, request_from
from pydantic import BaseModel

from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.stats import InstanceStats
from twinflow.scheduling.model import Job, Operation, Problem

DEFAULT_CLASS_WEIGHTS = {1: 1.0, 2: 2.0, 4: 4.0}
TOL = 1e-3


def _scheduled(problem: Problem) -> list[tuple[Operation, Job]]:
    return [
        (o, job)
        for job in problem.jobs
        if job.status == "open"
        for o in job.operations
        if o.status.state != "done"
    ]


def _eligible(operation: Operation) -> dict[str, tuple[float, float]]:
    """Expected options: machine -> (run, preference_cost), pin and status applied."""
    only: str | None = None
    if operation.status.state == "in_progress":
        only = operation.status.machine_id
    elif operation.pin is not None and operation.pin.machine_id is not None:
        only = operation.pin.machine_id
    return {
        o.machine_id: (o.fixed_seconds + operation.quantity * o.unit_seconds, o.preference_cost)
        for o in operation.options
        if only is None or o.machine_id == only
    }


def _expected_remaining(job: Job, position: int, scheduled_ids: set[str]) -> float:
    later = [o for o in job.operations[position + 1 :] if o.id in scheduled_ids]
    return sum(min(run for run, _ in _eligible(o).values()) for o in later)


def _assert_index_matches(problem: Problem, phi: float = 2.0) -> None:
    index = ProblemIndex.build(problem, phi=phi)
    scheduled = _scheduled(problem)
    ids = {o.id for o, _ in scheduled}
    assert tuple(index.op_ids) == tuple(o.id for o, _ in scheduled)
    for o, job in scheduled:
        assert index.job_of(o.id) == job.id
        assert index.release(o.id) == max(job.release, o.ready_time)
        assert index.work_center(o.id) == o.work_center_id
        options = index.options(o.id)
        assert [opt.machine_id for opt in options] == sorted(_eligible(o))
        assert {opt.machine_id: (opt.run, opt.preference_cost) for opt in options} == _eligible(o)
        links = {
            (link.operation_id, link.min_lag, link.max_lag, link.overlap_quantity)
            for link in o.predecessors
            if link.operation_id in ids
        }
        got = {(lk.pred_id, lk.min_lag, lk.max_lag, lk.overlap) for lk in index.predecessors(o.id)}
        assert got == links, o.id
        remaining = _expected_remaining(job, job.operations.index(o), ids)
        assert index.remaining_shortest_run(o.id) == pytest.approx(remaining)
        due = job.due
        expected_local = None if due is None else due - phi * remaining
        assert index.local_due(o.id) == pytest.approx(expected_local)
    for job in {job.id: job for _, job in scheduled}.values():
        weight = job.weight_override or DEFAULT_CLASS_WEIGHTS[job.weight_class]
        assert index.weight(job.id) == weight
        assert index.due(job.id) == job.due


@pytest.mark.parametrize("name", ["pinedo_7_1_1", "pinedo_14_2_1"])
def test_index_matches_pinedo_fixture(name: str) -> None:
    _assert_index_matches(compile_request(load_request(name)).problem)


@pytest.mark.parametrize("seed", range(10))
def test_index_matches_generated_problem(seed: int) -> None:
    _assert_index_matches(compile_request(random_request(seed)).problem)


def test_index_phi_scales_local_due() -> None:
    _assert_index_matches(compile_request(random_request(4)).problem, phi=0.5)


def test_index_on_7_1_1_has_known_remaining_runs_and_machines_of_centers() -> None:
    index = ProblemIndex.build(compile_request(load_request("pinedo_7_1_1")).problem)

    assert index.remaining_shortest_run("2-1") == 3.0 + 5.0 + 6.0
    assert index.remaining_shortest_run("2-4") == 0.0
    assert index.local_due("2-1") is None
    assert index.machines_of("WC-M1") == ("M1",)
    assert index.predecessors("1-1") == ()
    assert [lk.pred_id for lk in index.predecessors("1-2")] == ["1-1"]


def _status_problem() -> Problem:
    jobs: list[dict[str, Any]] = [
        {
            "id": "open",
            "due": 100.0,
            "release": 5.0,
            "weight_class": 4,
            "operations": [
                op("o1", {"M1": 3.0}, status={"state": "done"}),
                op("o2", {"M1": 4.0, "M2": 2.0}, after=["o1"], ready_time=9.0),
                op("o3", {"M1": 6.0, "M2": 7.0}, after=["o2"], pin={"machine_id": "M2"}),
            ],
        },
        {"id": "held", "status": "held", "operations": [op("h1", {"M1": 1.0})]},
        {"id": "done", "status": "done", "operations": [op("d1", {"M1": 1.0})]},
    ]
    problem = {
        "id": "status",
        "jobs": jobs,
        "machines": [machine("M1"), machine("M2")],
        "work_centers": [{"id": "WC"}],
    }
    return request_from(problem).problem


def test_index_skips_done_ops_and_jobs_that_are_not_open() -> None:
    index = ProblemIndex.build(_status_problem())

    assert tuple(index.op_ids) == ("o2", "o3")
    assert index.predecessors("o2") == ()
    assert index.release("o2") == 9.0
    assert index.weight("open") == 4.0
    assert index.machines_of("WC") == ("M1", "M2")


def test_index_pin_machine_restricts_options_and_shortest_run() -> None:
    index = ProblemIndex.build(_status_problem())

    assert [o.machine_id for o in index.options("o3")] == ["M2"]
    assert index.remaining_shortest_run("o2") == 7.0
    assert index.local_due("o2") == pytest.approx(100.0 - 2.0 * 7.0)


def test_index_values_are_plain_not_pydantic() -> None:
    index = ProblemIndex.build(compile_request(random_request(5)).problem)

    first = index.op_ids[0]
    assert not isinstance(index.options(first)[0], BaseModel)
    assert all(not isinstance(lk, BaseModel) for o in index.op_ids for lk in index.predecessors(o))


# --- instance statistics -------------------------------------------------------------------------


def test_stats_on_14_2_1_match_pinedo() -> None:
    index = ProblemIndex.build(compile_request(load_request("pinedo_14_2_1")).problem)

    stats = InstanceStats.for_center(index, "WC-M1", 0.0)

    assert stats.m == 1
    assert stats.pbar == pytest.approx(11.25, abs=TOL)
    assert stats.sbar == pytest.approx(2.0, abs=TOL)
    assert stats.cmax_hat == pytest.approx(53.0, abs=TOL)
    assert stats.tau == pytest.approx(0.566, abs=TOL)
    assert stats.R == pytest.approx(0.472, abs=TOL)
    assert stats.eta == pytest.approx(0.178, abs=TOL)
    assert stats.sampled_pairs == 16


def test_stats_cmax_hat_is_divided_by_machine_count() -> None:
    jobs = [
        {"id": f"J{k}", "due": 50.0, "operations": [op(f"o{k}", {"M1": 10.0, "M2": 10.0})]}
        for k in range(4)
    ]
    problem = request_from(
        {
            "id": "two",
            "jobs": jobs,
            "machines": [machine("M1"), machine("M2")],
            "work_centers": [{"id": "WC"}],
        }
    ).problem

    stats = InstanceStats.for_center(ProblemIndex.build(problem), "WC", 0.0)

    assert stats.m == 2
    assert stats.sbar == 0.0
    assert stats.eta == 0.0
    assert stats.cmax_hat == pytest.approx(40.0 / 2)


def _big_center(order: int, uniform: bool) -> Problem:
    """150 single-op jobs on 2 machines: (150 + 1) * 150 pairs per machine > 20,000."""
    jobs = [
        {
            "id": f"J{k:03d}",
            "due": 1000.0 + k,
            "operations": [
                op(f"o{k:03d}", {"M1": 5.0, "M2": 6.0}, setup_params={"w": float(k % 7)})
            ],
        }
        for k in range(150)
    ][::order]
    terms = [] if uniform else [{"kind": "abs_diff", "param": "w", "seconds_per_unit": 1.0}]
    function: dict[str, Any] = {"id": "S", "terms": terms}
    if uniform:
        function["default_seconds"] = 5.0
    state = {"last_setup_params": {"w": 3.0}}
    problem = {
        "id": "big",
        "jobs": jobs,
        "machines": [machine("M1", state=state), machine("M2", state=state)],
        "work_centers": [{"id": "WC", "setup_function_id": "S"}],
        "setup_functions": [function],
    }
    return request_from(problem).problem


def test_stats_sbar_sampling_is_capped_and_deterministic() -> None:
    first = InstanceStats.for_center(ProblemIndex.build(_big_center(1, False)), "WC", 0.0)
    again = InstanceStats.for_center(ProblemIndex.build(_big_center(1, False)), "WC", 0.0)
    reversed_jobs = InstanceStats.for_center(ProblemIndex.build(_big_center(-1, False)), "WC", 0.0)

    assert 0 < first.sampled_pairs <= 20_000
    assert first == again
    assert reversed_jobs.sbar == first.sbar  # the pair list is sorted, so input order is moot
    assert 0.0 < first.sbar < 6.0


def test_stats_sbar_with_uniform_setups_is_exact_under_sampling() -> None:
    stats = InstanceStats.for_center(ProblemIndex.build(_big_center(1, True)), "WC", 0.0)

    assert stats.sampled_pairs <= 20_000
    assert stats.sbar == pytest.approx(5.0)
    assert stats.pbar == pytest.approx(5.5)


@pytest.mark.slow
def test_index_build_of_600_by_8_is_under_one_second() -> None:
    problem = compile_request(scale_request(0)).problem

    started = time.perf_counter()
    index = ProblemIndex.build(problem)
    elapsed = time.perf_counter() - started

    assert len(index.op_ids) == 600 * 8
    assert elapsed < 1.0, f"index build took {elapsed:.2f} s"
