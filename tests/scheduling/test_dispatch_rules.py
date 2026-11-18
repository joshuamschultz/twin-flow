"""Elementary dispatch rules (T-125, COMP-009, REQ-090; Pinedo Table 14.1, p.372-373).

`twinflow.scheduling.algorithms.dispatch_rules` registers each rule as a solver on the
non-delay engine (family `dispatch`, `deterministic=True`, every `Feature`). Index
readings used by the hand calculations below (higher index first, ties by op id):

- `edd`: earliest job due first (no due = last).
- `wspt`: largest w_j / p_i(o).
- `spt` / `lpt`: shortest / longest run p_i(o) on the deciding machine.
- `ms`: smallest slack d_j - t - (p_i(o) + R_after(o)), i.e. the remaining processing of
  the job including the op being dispatched (Pinedo's MS for a job shop; R_after is the
  index's `remaining_shortest_run`, the later ops' shortest runs).
- `cr`: smallest (d_j - t) / (p_i(o) + R_after(o)); negative once the job is overdue, so
  overdue jobs come first, the most overdue per unit of remaining work before the others.
- `sst`: smallest setup s_i(last, o) from the machine's last placed op, or from the machine
  state when nothing is placed yet.
- `lfj`: fewest eligible machines, then EDD.
- `sqno`: shortest queue at the next op's work center, the queue being the ready (all
  predecessors placed) unplaced ops of that center at decision time.

Every crafted instance is checked through `solve(request, solver=get_solver(name))`, so
each result is also verified.
"""

from __future__ import annotations

import functools
from typing import Any

import pytest
from generators import random_request
from kernel_cases import load_request, machine, op, request_from

from twinflow.scheduling.model import SolveRequest, SolveResult
from twinflow.scheduling.registry import available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature
from twinflow.scheduling.solver import solve

RULES = ("edd", "wspt", "spt", "lpt", "ms", "cr", "sst", "lfj", "sqno")
ALL_DISPATCH = ("fifo", *RULES)
PROPERTY_SEEDS = range(200)
MIN_VERIFIED = 150
DETERMINISM_SEEDS = (3, 8, 17)


# --- helpers ---------------------------------------------------------------------------------


def _job(job_id: str, ops: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    return {"id": job_id, "operations": ops} | fields


def _problem(
    jobs: list[dict[str, Any]], machines: list[dict[str, Any]], **fields: Any
) -> dict[str, Any]:
    centers = sorted({m["work_center_id"] for m in machines})
    return {
        "id": "rule-case",
        "jobs": jobs,
        "machines": machines,
        "work_centers": [{"id": c} for c in centers],
    } | fields


def _run(name: str, request: SolveRequest) -> SolveResult:
    result = solve(request, solver=get_solver(name))
    assert result.verified, (name, result.issues)
    return result


def _order(result: SolveResult, machine_id: str) -> list[str]:
    on_m = [a for a in result.schedule.assignments if a.machine_id == machine_id]
    return [a.operation_id for a in sorted(on_m, key=lambda a: (a.start, a.operation_id))]


def _check(name: str, request: SolveRequest, machine_id: str, expected: list[str]) -> None:
    """`name` sequences `machine_id` as `expected`, and FIFO does not."""
    assert _order(_run(name, request), machine_id) == expected
    assert _order(_run("fifo", request), machine_id) != expected


# --- single-machine base instance: EDD, WSPT, SPT, LPT ---------------------------------------

# job: (run, due, weight); all released at 0 on M1, so FIFO falls back to op id order
# J1, J2, J3, J4.
BASE = {
    "J1": (4.0, 40.0, 1.0),
    "J2": (3.0, 10.0, 2.0),
    "J3": (8.0, 30.0, 4.0),
    "J4": (2.0, 20.0, 3.0),
}


def _base_request() -> SolveRequest:
    jobs = [
        _job(j, [op(f"{j}-1", {"M1": p})], due=d, weight_override=w)
        for j, (p, d, w) in BASE.items()
    ]
    return request_from(_problem(jobs, [machine("M1")]))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # due: J2 10, J4 20, J3 30, J1 40.
        ("edd", ["J2-1", "J4-1", "J3-1", "J1-1"]),
        # w/p: J1 1/4 = 0.25, J2 2/3 = 0.667, J3 4/8 = 0.5, J4 3/2 = 1.5.
        ("wspt", ["J4-1", "J2-1", "J3-1", "J1-1"]),
        # p: J4 2, J2 3, J1 4, J3 8.
        ("spt", ["J4-1", "J2-1", "J1-1", "J3-1"]),
        ("lpt", ["J3-1", "J1-1", "J2-1", "J4-1"]),
    ],
)
def test_rule_on_single_machine_base(name: str, expected: list[str]) -> None:
    _check(name, _base_request(), "M1", expected)


def test_fifo_on_single_machine_base_is_op_id_order() -> None:
    assert _order(_run("fifo", _base_request()), "M1") == ["J1-1", "J2-1", "J3-1", "J4-1"]


# --- MS --------------------------------------------------------------------------------------


def test_ms_uses_remaining_run_including_current_op() -> None:
    # First ops on M1 (center A), second ops on M2 (center B); all released at 0, so every
    # M1 decision compares the four first ops at the same t and t drops out of the order.
    # slack + t = d - (p + R_after):
    #   J1: 20 - (2 + 1)  = 17     J2: 18 - (3 + 10) = 5
    #   J3: 15 - (4 + 0)  = 11     J4: 16 - (1 + 2)  = 13
    # MS: J2, J3, J4, J1. (EDD would give J3, J4, J2, J1; slack without the current op,
    # d - R_after: 19, 8, 15, 14 -> J2, J4, J3, J1; FIFO J1, J2, J3, J4.)
    spec = {"J1": (2.0, 1.0, 20.0), "J2": (3.0, 10.0, 18.0), "J3": (4.0, None, 15.0),
            "J4": (1.0, 2.0, 16.0)}  # fmt: skip
    jobs = []
    for j, (p1, p2, due) in spec.items():
        ops = [op(f"{j}-1", {"M1": p1}, center="A")]
        if p2 is not None:
            ops.append(op(f"{j}-2", {"M2": p2}, center="B", after=[f"{j}-1"]))
        jobs.append(_job(j, ops, due=due))
    request = request_from(_problem(jobs, [machine("M1", center="A"), machine("M2", center="B")]))
    _check("ms", request, "M1", ["J2-1", "J3-1", "J4-1", "J1-1"])


# --- CR --------------------------------------------------------------------------------------


def test_cr_puts_overdue_first_with_negative_ratio() -> None:
    # J0-1 (run 10, no due) is the only op ready at 0; J1..J4 are released at 1. On M1:
    #   t = 10: CR = (d - 10) / p: J1 30/20 = 1.5, J2 -4/8 = -0.5, J3 -4/2 = -2,
    #           J4 16/5 = 3.2 -> J3 [10, 12]
    #   t = 12: J1 28/20 = 1.4, J2 -6/8 = -0.75, J4 14/5 = 2.8 -> J2 [12, 20]
    #   t = 20: J1 20/20 = 1.0, J4 6/5 = 1.2 -> J1 [20, 40], then J4.
    # CR: J0, J3, J2, J1, J4. (EDD: J0, J2, J3, J4, J1 (J2, J3 tie at 6, op id); CR clamped
    # at 0 when overdue would tie J2, J3 and take J2 first; FIFO J0, J1, J2, J3, J4.)
    spec = {"J1": (20.0, 40.0), "J2": (8.0, 6.0), "J3": (2.0, 6.0), "J4": (5.0, 26.0)}
    jobs = [_job("J0", [op("J0-1", {"M1": 10.0})])]
    jobs += [_job(j, [op(f"{j}-1", {"M1": p})], due=d, release=1.0) for j, (p, d) in spec.items()]
    request = request_from(_problem(jobs, [machine("M1")]))
    _check("cr", request, "M1", ["J0-1", "J3-1", "J2-1", "J1-1", "J4-1"])


# --- SST -------------------------------------------------------------------------------------


def test_sst_starts_from_machine_state_then_last_op() -> None:
    # Setup |w_prev - w_next| s; M1's state is w = 10; runs 3; w: J1 5, J2 9, J3 1, J4 6.
    #   from state 10: J1 5, J2 1, J3 9, J4 4 -> J2
    #   from J2 (9):   J1 4, J3 8, J4 3       -> J4
    #   from J4 (6):   J1 1, J3 5             -> J1, then J3.
    # SST: J2, J4, J1, J3. (Ignoring the state, i.e. from w = 0: J3, J1, J4, J2.)
    w = {"J1": 5.0, "J2": 9.0, "J3": 1.0, "J4": 6.0}
    jobs = [_job(j, [op(f"{j}-1", {"M1": 3.0}, setup_params={"w": x})]) for j, x in w.items()]
    m1 = machine("M1", setup_function_id="S", state={"last_setup_params": {"w": 10.0}})
    setup = {"id": "S", "terms": [{"kind": "abs_diff", "param": "w", "seconds_per_unit": 1.0}]}
    request = request_from(_problem(jobs, [m1], setup_functions=[setup]))
    _check("sst", request, "M1", ["J2-1", "J4-1", "J1-1", "J3-1"])


# --- LFJ -------------------------------------------------------------------------------------


def test_lfj_fewest_machines_then_edd() -> None:
    # M1, M2 in one center, pref 0 everywhere, runs 5, dues loose (all finish by 10).
    #   J1 {M1, M2} d 100, J2 {M1} d 300, J3 {M1, M2} d 50, J4 {M1} d 200.
    # t=0 M1 (lower id): J2, J4 have 1 machine, EDD -> J4 [0, 5].
    # t=0 M2: J1, J3 (2 machines each), EDD -> J3 [0, 5].
    # t=5 M1: J1 (2), J2 (1) -> J2 [5, 10].  t=5 M2: J1 [5, 10].
    # LFJ: M1 J4, J2; M2 J3, J1. (FIFO: M1 J1, J2, J4; M2 J3. EDD: M1 J3, J4, J2; M2 J1.)
    spec = {"J1": (("M1", "M2"), 100.0), "J2": (("M1",), 300.0),
            "J3": (("M1", "M2"), 50.0), "J4": (("M1",), 200.0)}  # fmt: skip
    jobs = [_job(j, [op(f"{j}-1", dict.fromkeys(ms, 5.0))], due=d) for j, (ms, d) in spec.items()]
    request = request_from(_problem(jobs, [machine("M1"), machine("M2")]))
    result = _run("lfj", request)
    assert _order(result, "M1") == ["J4-1", "J2-1"]
    assert _order(result, "M2") == ["J3-1", "J1-1"]
    assert _order(_run("fifo", request), "M1") != ["J4-1", "J2-1"]


# --- SQNO ------------------------------------------------------------------------------------


def test_sqno_shortest_queue_at_next_center() -> None:
    # Centers A (M1), B (M2), C (M3), D (M4). J1..J4: first op on M1 (run 2), second op
    # (run 1) at B, C, B, D. Fillers K1, K2 on M2 and L1 on M3, run 100, ready at 0.
    # t=0 M1 first (lowest id among free machines): queues B = {K1, K2} 2, C = {L1} 1,
    #   D = {} 0 -> J4-1 [0, 2].
    # M2, M3 take a K and L1 at 0; M4 takes J4-2 at 2.
    # t=2 M1: B = 1 (one K left), C = 0, D = 0 -> J2 (C) [2, 4].
    # t=4 M1: J1 (B 1), J3 (B 1) tie -> op id J1 [4, 6], then J3.
    # SQNO on M1: J4, J2, J1, J3. (FIFO and a queue at the current center: J1, J2, J3, J4.)
    nxt = {"J1": ("B", "M2"), "J2": ("C", "M3"), "J3": ("B", "M2"), "J4": ("D", "M4")}
    jobs = [
        _job(j, [
            op(f"{j}-1", {"M1": 2.0}, center="A"),
            op(f"{j}-2", {m: 1.0}, center=c, after=[f"{j}-1"]),
        ])
        for j, (c, m) in nxt.items()
    ]  # fmt: skip
    jobs += [_job(k, [op(f"{k}-1", {"M2": 100.0}, center="B")]) for k in ("K1", "K2")]
    jobs.append(_job("L1", [op("L1-1", {"M3": 100.0}, center="C")]))
    machines = [
        machine(m, center=c) for m, c in (("M1", "A"), ("M2", "B"), ("M3", "C"), ("M4", "D"))
    ]
    request = request_from(_problem(jobs, machines))
    _check("sqno", request, "M1", ["J4-1", "J2-1", "J1-1", "J3-1"])


# --- Pinedo 14.2.1 (Example 15.5.3 table) ----------------------------------------------------

# p 13, 9, 13, 10; d 12, 37, 21, 22; w 2, 4, 2, 5; family setups from state "start".
# EDD 1, 3, 4, 2: setups 1, 1, 3, 3 -> C 14, 28, 41, 53;
#   WT = 2*2 + 2*7 + 5*19 + 4*16 = 177.
# SPT 2, 4, 1, 3 (1 and 3 tie at 13, op id): setups 1, 0, 4, 1 -> C 10, 20, 37, 51;
#   WT = 0 + 0 + 2*25 + 2*30 = 110.


@pytest.mark.parametrize(
    ("name", "expected", "weighted_tardiness"),
    [("edd", ["1-1", "3-1", "4-1", "2-1"], 177.0), ("spt", ["2-1", "4-1", "1-1", "3-1"], 110.0)],
)
def test_rule_on_pinedo_14_2_1(name: str, expected: list[str], weighted_tardiness: float) -> None:
    result = _run(name, load_request("pinedo_14_2_1"))
    assert _order(result, "M1") == expected
    assert result.schedule.status == "FEASIBLE"
    assert result.schedule.primary_value == pytest.approx(weighted_tardiness)


# --- registration ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_DISPATCH)
def test_rule_registered_deterministic_dispatch(name: str) -> None:
    info = {i.name: i for i in available()}[name]
    assert info.family == "dispatch"
    assert info.deterministic is True
    assert info.capabilities == frozenset(Feature)
    assert info.module == "twinflow.scheduling.algorithms.dispatch_rules"
    assert get_solver(name).name == name


@pytest.mark.parametrize("name", ALL_DISPATCH)
def test_rule_same_output_for_any_seed(name: str) -> None:
    for seed in DETERMINISM_SEEDS:
        request = random_request(seed)
        a = solve(request, solver=get_solver(name), seed=0)
        b = solve(request, solver=get_solver(name), seed=1)
        assert a.schedule.assignments == b.schedule.assignments, f"{name} seed {seed}"


# --- property --------------------------------------------------------------------------------


@functools.cache
def _seed_result(name: str, seed: int) -> SolveResult:
    return solve(random_request(seed), solver=get_solver(name))


@pytest.mark.parametrize("name", RULES)
def test_rule_property_verifies_or_names_issue(name: str) -> None:
    verified = 0
    for seed in PROPERTY_SEEDS:
        result = _seed_result(name, seed)
        status = result.schedule.status
        assert status in ("FEASIBLE", "UNKNOWN"), f"{name} seed {seed}: {status}"
        if status == "FEASIBLE":
            assert result.verified, f"{name} seed {seed}: {result.issues}"
            assert result.schedule.primary_value is not None, f"{name} seed {seed}"
            verified += 1
            continue
        codes = [issue.code for issue in result.issues]
        assert codes, f"{name} seed {seed}: UNKNOWN without an issue"
        assert "solver_error" not in codes, f"{name} seed {seed}: raised"
        assert "unsupported_feature" not in codes, f"{name} seed {seed}: {codes}"
    assert verified >= MIN_VERIFIED, f"{name}: only {verified} schedules verified"
