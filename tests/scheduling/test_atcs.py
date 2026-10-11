"""ATC and ATCS (T-129..T-132, COMP-010, REQ-092..REQ-095, REQ-121, D-073).

`twinflow.scheduling.algorithms.atcs` registers `atc` and `atcs` (family `composite`,
`deterministic=True`, every `Feature`) on the dispatch engine. Index (SDD ATCS pseudo-code),
for op o of job j on machine i at decision time t after op l:

    (w_j / p_i(o)) * exp(-max(dloc(o) - p_i(o) - t, 0) / (K1 * pbar))
                   * exp(-s_i(l, o) / (K2 * sbar)) * flex(o) * suit(o, i)

with `dloc = d_j - phi * R_after(o)` (phi 2, param `phi`) and pbar, sbar, tau, R, eta, m per
work center (`kernel.stats.InstanceStats`). ATC is ATCS with the setup factor fixed at 1.

API these tests pin (the implementer builds to it):

- `atcs_k(tau, R, eta) -> (K1, K2)`: K1 = 4.5 + R (R <= 0.5) else 6 - 2R, K2 = tau / (2
  sqrt(eta)), clamped to [0.5, 10] and [0.05, 10]; finite for eta 0 (the setup factor is
  then 1, so K2 is only reported).
- `flexibility_factor(eligible, m) -> 1 + 0.5 * (1 - eligible / m)` (the value when on).
- `suitability_factor(preference_cost, prefbar) -> exp(-preference_cost / prefbar)` (K3 = 1),
  1 when prefbar is 0.
- Params through `profile.method_params[name]`: `K1`, `K2` (ATC takes its single K as `K1`
  too), `phi`, `flexibility`, `suitability`, `macro_jobs`, `merge_window`.
- `k_lookup` constructor argument: `registry.get("atcs", k_lookup=obj)` where `obj` is a
  picklable callable `obj(tau, R, eta, m) -> (K1, K2)` (the SDD pseudo-code's
  `k_lookup(tau, R, eta, m)`). Explicit params beat the lookup, the lookup beats the formula.
- `Schedule.atcs_runs`: one `AtcsRun` per work center with ops, `source` in
  formula / table / explicit, `objective_value` = the returned schedule's `primary_value`.

Every instance runs through `solve(request, solver=get(name))`, so each plan is also verified.
"""

from __future__ import annotations

import importlib
import math
import pickle
from types import ModuleType
from typing import Any

import pytest
from generators import random_request
from kernel_cases import load_request, machine, op, request_from

from twinflow.scheduling.model import ObjectiveKind, SolveRequest, SolveResult
from twinflow.scheduling.registry import available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature
from twinflow.scheduling.solver import solve

WT = ObjectiveKind.WEIGHTED_TARDINESS
WT_OBJECTIVE = {"mode": "lexicographic", "levels": [{"kind": "weighted_tardiness"}]}
K_TOL = 0.005


# --- helpers ---------------------------------------------------------------------------------


def _atcs_module() -> ModuleType:
    """The leaf, imported per test so its absence fails each test, not collection."""
    return importlib.import_module("twinflow.scheduling.algorithms.atcs")


def _with_params(request: SolveRequest, name: str, params: dict[str, Any]) -> SolveRequest:
    data = request.model_dump(mode="json")
    data["profile"]["method_params"] = {name: params}
    return SolveRequest.model_validate(data)


def _run(name: str, request: SolveRequest, **solver_kwargs: Any) -> SolveResult:
    result = solve(request, solver=get_solver(name, **solver_kwargs))
    assert result.schedule.status == "FEASIBLE", (name, result.schedule.issues)
    assert result.verified, (name, result.issues)
    return result


def _order(result: SolveResult, machine_id: str) -> list[str]:
    on_m = [a for a in result.schedule.assignments if a.machine_id == machine_id]
    return [a.operation_id for a in sorted(on_m, key=lambda a: (a.start, a.operation_id))]


def _start(result: SolveResult, op_id: str) -> float:
    return next(a.start for a in result.schedule.assignments if a.operation_id == op_id)


def _runs_by_center(result: SolveResult) -> dict[str, Any]:
    runs = result.schedule.atcs_runs
    by_center = {r.work_center_id: r for r in runs}
    assert len(by_center) == len(runs), "one AtcsRun per work center"
    return by_center


def _problem(
    jobs: list[dict[str, Any]], machines: list[dict[str, Any]], **fields: Any
) -> dict[str, Any]:
    centers = sorted({m["work_center_id"] for m in machines})
    return {
        "id": "atcs-case",
        "jobs": jobs,
        "machines": machines,
        "work_centers": [{"id": c} for c in centers],
    } | fields


def _job(job_id: str, ops: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    return {"id": job_id, "operations": ops} | fields


def _wt_request(problem: dict[str, Any], name: str, params: dict[str, Any]) -> SolveRequest:
    profile = {"objective": WT_OBJECTIVE, "method_params": {name: params}}
    return request_from(problem, profile=profile)


def _p1421(params: dict[str, Any]) -> SolveRequest:
    return _with_params(load_request("pinedo_14_2_1"), "atcs", params)


SEQ_1421 = ["2-1", "4-1", "3-1", "1-1"]
SEQ_1434 = ["4-1", "1-1", "2-1", "3-1"]


class FixedK:
    """A picklable `k_lookup` returning fixed K and recording what it was asked."""

    def __init__(self, k1: float, k2: float) -> None:
        self.k = (k1, k2)
        self.calls: list[tuple[float, float, float, int]] = []

    def __call__(self, tau: float, R: float, eta: float, m: int) -> tuple[float, float]:  # noqa: N803
        self.calls.append((tau, R, eta, m))
        return self.k


# --- registration (T-129) --------------------------------------------------------------------


@pytest.mark.parametrize("name", ["atc", "atcs"])
def test_atc_and_atcs_are_registered_composite_deterministic_with_every_feature(
    name: str,
) -> None:
    info = {i.name: i for i in available()}
    assert name in info, sorted(info)
    entry = info[name]
    assert entry.family == "composite"
    assert entry.deterministic is True
    assert entry.capabilities == frozenset(Feature)
    assert entry.module == "twinflow.scheduling.algorithms.atcs"
    solver = get_solver(name)
    assert solver.name == name
    assert solver.capabilities == frozenset(Feature)


# --- K function and clamps (T-129, REQ-093) --------------------------------------------------


def test_atcs_k_formula_reproduces_example_14_2_1() -> None:
    k1, k2 = _atcs_module().atcs_k(0.566038, 0.471698, 0.177778)
    assert k1 == pytest.approx(4.97, abs=K_TOL)
    assert k2 == pytest.approx(0.671, abs=K_TOL)


@pytest.mark.parametrize(
    ("R", "k1"),
    [(0.0, 4.5), (0.5, 5.0), (0.6, 4.8), (2.0, 2.0), (2.75, 0.5), (3.0, 0.5), (10.0, 0.5)],
)
def test_atcs_k1_branches_at_r_half_and_clamps_low_at_half(R: float, k1: float) -> None:  # noqa: N803
    assert _atcs_module().atcs_k(0.5, R, 0.25)[0] == pytest.approx(k1)


@pytest.mark.parametrize(
    ("tau", "eta", "k2"),
    [
        (0.5, 0.25, 0.5),  # 0.5 / (2 * 0.5)
        (0.9, 1e-4, 10.0),  # 45 -> clamped high
        (0.01, 1.0, 0.05),  # 0.005 -> clamped low
        (-0.5, 0.25, 0.05),  # negative (very loose dues) -> clamped low
    ],
)
def test_atcs_k2_formula_and_clamps(tau: float, eta: float, k2: float) -> None:
    assert _atcs_module().atcs_k(tau, 0.2, eta)[1] == pytest.approx(k2)


@pytest.mark.parametrize("tau", [-1.0, 0.0, 0.5])
def test_atcs_k_with_eta_zero_is_finite_and_clamped(tau: float) -> None:
    k1, k2 = _atcs_module().atcs_k(tau, 0.2, 0.0)
    assert k1 == pytest.approx(4.7)
    assert math.isfinite(k2)
    assert 0.05 <= k2 <= 10.0


def test_atcs_large_spread_instance_reports_k1_clamped_to_half() -> None:
    # One machine, run 5 each, no setups: Cmax_hat = 10; dues 0 and 30 give R = 3 (6 - 2R = 0).
    jobs = [
        _job("A", [op("A-1", {"M1": 5.0})], due=0.0),
        _job("B", [op("B-1", {"M1": 5.0})], due=30.0),
    ]
    result = _run("atcs", _wt_request(_problem(jobs, [machine("M1")]), "atcs", {}))
    run = _runs_by_center(result)["WC"]
    assert run.R == pytest.approx(3.0)
    assert run.K1 == pytest.approx(0.5)
    assert run.source == "formula"


# --- Example 14.2.1 and 14.3.4 (T-129, REQ-092, REQ-093) -------------------------------------


def test_atcs_explicit_k_on_14_2_1_gives_2_4_3_1_and_98() -> None:
    result = _run("atcs", _p1421({"K1": 5.0, "K2": 0.7}))
    assert _order(result, "M1") == SEQ_1421
    assert result.objective_values[WT] == pytest.approx(98.0)
    assert result.schedule.primary_value == pytest.approx(98.0)


def test_atcs_automatic_k_on_14_2_1_reports_book_factors_and_gives_98() -> None:
    result = _run("atcs", _p1421({}))
    assert _order(result, "M1") == SEQ_1421
    assert result.objective_values[WT] == pytest.approx(98.0)
    run = _runs_by_center(result)["WC-M1"]
    assert run.tau == pytest.approx(0.566, abs=K_TOL)
    assert run.R == pytest.approx(0.472, abs=K_TOL)
    assert run.eta == pytest.approx(0.178, abs=K_TOL)
    assert run.m == 1
    assert run.K1 == pytest.approx(4.97, abs=K_TOL)
    assert run.K2 == pytest.approx(0.671, abs=K_TOL)
    assert run.source == "formula"
    assert run.objective_value == pytest.approx(98.0)


def test_atc_with_k_5_on_14_3_4_gives_4_1_2_3_and_440() -> None:
    request = _with_params(load_request("pinedo_14_3_4"), "atc", {"K1": 5.0})
    result = _run("atc", request)
    assert _order(result, "M1") == SEQ_1434
    assert result.objective_values[WT] == pytest.approx(440.0)


def test_atc_ignores_setups_so_it_starts_14_2_1_with_job_4() -> None:
    # Setup factor fixed at 1: at t = 0 the ATC index (K = 5, pbar 11.25) is
    # 1: 0.154, 2: 0.270, 3: 0.133, 4: 0.404, while ATCS (setups 1, 1, 3, 4) starts with 2.
    request = _with_params(load_request("pinedo_14_2_1"), "atc", {"K1": 5.0})
    assert _order(_run("atc", request), "M1")[0] == "4-1"


def test_atcs_with_zero_setups_has_setup_factor_1_and_matches_atc() -> None:
    # 14.3.4 has no setups: sbar = 0, eta = 0, so 0 / (K2 * 0) must not poison the index.
    base = load_request("pinedo_14_3_4")
    atcs = _run("atcs", _with_params(base, "atcs", {"K1": 5.0}))
    atc = _run("atc", _with_params(base, "atc", {"K1": 5.0}))
    assert _order(atcs, "M1") == _order(atc, "M1") == SEQ_1434
    assert atcs.objective_values[WT] == pytest.approx(440.0)
    run = _runs_by_center(atcs)["WC-M1"]
    assert run.eta == 0.0
    assert math.isfinite(run.K2)


def test_atcs_is_deterministic_on_14_2_1() -> None:
    first = _run("atcs", _p1421({}))
    second = _run("atcs", _p1421({}))
    assert first.schedule.assignments == second.schedule.assignments
    assert first.schedule.atcs_runs == second.schedule.atcs_runs


# --- job shop: local due dates, per-center K, run records (T-130, REQ-093, REQ-121) ---------
#
# Center A (MA): X-1 and Y-1, run 5, weight 1. X has X-2 on center B (MB, run 20) and due 40;
# Y is single-op with due 30. dloc(X-1) = 40 - 2 * 20 = 0 < dloc(Y-1) = 30, so ATCS runs X-1
# first although X's due is later (EDD would run Y-1 first).
# Center A stats: pbar 5, sbar 0, Cmax_hat 10, tau = 1 - 15/10 = -0.5, R = 3 -> K1 0.5.
# Center B stats: P = {X-2}, Cmax_hat 20, dloc 40, tau = 1 - 40/20 = -1, R = 0 -> K1 4.5.


def _two_center_problem() -> dict[str, Any]:
    jobs = [
        _job(
            "X",
            [
                op("X-1", {"MA": 5.0}, center="A"),
                op("X-2", {"MB": 20.0}, center="B", after=["X-1"]),
            ],
            due=40.0,
        ),
        _job("Y", [op("Y-1", {"MA": 5.0}, center="A")], due=30.0),
    ]
    return _problem(jobs, [machine("MA", center="A"), machine("MB", center="B")])


def test_atcs_job_shop_orders_by_local_due_date() -> None:
    result = _run("atcs", _wt_request(_two_center_problem(), "atcs", {}))
    assert _order(result, "MA") == ["X-1", "Y-1"]


def test_atcs_phi_param_sets_the_local_due_factor() -> None:
    # phi = 0: dloc = d, so Y-1 (30) beats X-1 (40) with equal w / p.
    result = _run("atcs", _wt_request(_two_center_problem(), "atcs", {"phi": 0.0}))
    assert _order(result, "MA") == ["Y-1", "X-1"]


def test_atcs_records_one_run_per_center_with_its_own_statistics_and_k() -> None:
    result = _run("atcs", _wt_request(_two_center_problem(), "atcs", {}))
    runs = _runs_by_center(result)
    assert set(runs) == {"A", "B"}
    a, b = runs["A"], runs["B"]
    assert (a.tau, a.R, a.eta, a.m) == pytest.approx((-0.5, 3.0, 0.0, 1))
    assert (b.tau, b.R, b.eta, b.m) == pytest.approx((-1.0, 0.0, 0.0, 1))
    assert a.K1 == pytest.approx(0.5)
    assert b.K1 == pytest.approx(4.5)
    assert (a.K1, a.K2) != (b.K1, b.K2)
    assert {r.source for r in runs.values()} == {"formula"}
    primary = result.schedule.primary_value
    assert primary is not None
    assert all(r.objective_value == pytest.approx(primary) for r in runs.values())


def test_atcs_k_lookup_supplies_k_per_center_with_source_table() -> None:
    lookup = FixedK(3.0, 0.5)
    request = _wt_request(_two_center_problem(), "atcs", {})
    result = _run("atcs", request, k_lookup=lookup)
    runs = _runs_by_center(result)
    assert set(runs) == {"A", "B"}
    for run in runs.values():
        assert (run.K1, run.K2, run.source) == (3.0, 0.5, "table")
    # Asked once per center with that center's (tau, R, eta, m).
    asked = sorted(lookup.calls)
    expected = sorted([(-0.5, 3.0, 0.0, 1), (-1.0, 0.0, 0.0, 1)])
    assert len(asked) == len(expected)
    for got, want in zip(asked, expected, strict=True):
        assert got == pytest.approx(want)


def test_atcs_explicit_k_beats_the_lookup_with_source_explicit() -> None:
    lookup = FixedK(3.0, 0.5)
    request = _wt_request(_two_center_problem(), "atcs", {"K1": 2.0, "K2": 0.4})
    result = _run("atcs", request, k_lookup=lookup)
    for run in _runs_by_center(result).values():
        assert (run.K1, run.K2, run.source) == (2.0, 0.4, "explicit")


def test_atcs_solver_with_k_lookup_survives_pickling() -> None:
    # The portfolio runs stages in a spawn process pool (COMP-024).
    # Round-trips bytes this test just produced (not untrusted input).
    solver = pickle.loads(pickle.dumps(get_solver("atcs", k_lookup=FixedK(3.0, 0.5))))  # noqa: S301
    result = solve(_wt_request(_two_center_problem(), "atcs", {}), solver=solver)
    assert result.verified
    assert {r.source for r in result.schedule.atcs_runs} == {"table"}


def test_atcs_handles_jobs_without_due_dates() -> None:
    jobs = [
        _job("A", [op("A-1", {"M1": 4.0})], due=10.0),
        _job("B", [op("B-1", {"M1": 3.0})]),
        _job("C", [op("C-1", {"M1": 5.0}), op("C-2", {"M1": 2.0}, after=["C-1"])]),
    ]
    result = _run("atcs", _wt_request(_problem(jobs, [machine("M1")]), "atcs", {}))
    assert len(result.schedule.assignments) == 4
    run = _runs_by_center(result)["WC"]
    assert all(math.isfinite(v) for v in (run.tau, run.R, run.eta, run.K1, run.K2))


def test_atcs_with_no_due_dates_at_all_still_verifies() -> None:
    jobs = [_job(j, [op(f"{j}-1", {"M1": p})]) for j, p in (("A", 4.0), ("B", 3.0))]
    result = _run("atcs", _wt_request(_problem(jobs, [machine("M1")]), "atcs", {}))
    run = _runs_by_center(result)["WC"]
    assert (run.tau, run.R) == (0.0, 0.0)


# --- flexibility and suitability (T-131, REQ-094) -------------------------------------------


@pytest.mark.parametrize(
    ("eligible", "m", "factor"), [(1, 3, 4.0 / 3.0), (3, 3, 1.0), (2, 4, 1.25), (1, 1, 1.0)]
)
def test_flexibility_factor_raises_ops_with_fewer_eligible_machines(
    eligible: int, m: int, factor: float
) -> None:
    assert _atcs_module().flexibility_factor(eligible, m) == pytest.approx(factor)


@pytest.mark.parametrize(
    ("cost", "prefbar", "factor"),
    [(0.0, 1.0, 1.0), (2.0, 1.0, math.exp(-2.0)), (1.0, 2.0, math.exp(-0.5)), (2.0, 0.0, 1.0)],
)
def test_suitability_factor_lowers_options_with_higher_preference_cost(
    cost: float, prefbar: float, factor: float
) -> None:
    assert _atcs_module().suitability_factor(cost, prefbar) == pytest.approx(factor)


def _flex_problem() -> dict[str, Any]:
    # J1-1 may run on M1, M2 or M3; J2-1 only on M1. Equal run 10, weight 1, due 10. The op-id
    # tie-break favors J1-1 on M1; flexibility (4/3 vs 1) must favor J2-1 there.
    jobs = [
        _job("J1", [op("J1-1", {"M1": 10.0, "M2": 10.0, "M3": 10.0})], due=10.0),
        _job("J2", [op("J2-1", {"M1": 10.0})], due=10.0),
    ]
    return _problem(jobs, [machine("M1"), machine("M2"), machine("M3")])


def test_atcs_flexibility_on_puts_the_less_flexible_op_first() -> None:
    result = _run("atcs", _wt_request(_flex_problem(), "atcs", {"flexibility": True}))
    assert _start(result, "J2-1") == 0.0
    assert _order(result, "M1")[0] == "J2-1"
    assert result.objective_values[WT] == pytest.approx(0.0)


def test_atcs_flexibility_off_keeps_the_op_id_tie_break() -> None:
    result = _run("atcs", _wt_request(_flex_problem(), "atcs", {"flexibility": False}))
    assert _order(result, "M1") == ["J1-1", "J2-1"]
    assert _start(result, "J2-1") == pytest.approx(10.0)


def _suit_problem() -> dict[str, Any]:
    # One machine; J1-1's option costs 2, J2-1's costs 0; otherwise equal. prefbar = 1.
    jobs = [
        _job("J1", [op("J1-1", {"M1": 10.0})], due=10.0),
        _job("J2", [op("J2-1", {"M1": 10.0})], due=10.0),
    ]
    jobs[0]["operations"][0]["options"][0]["preference_cost"] = 2.0
    return _problem(jobs, [machine("M1")])


def test_atcs_suitability_on_puts_the_preferred_option_first() -> None:
    result = _run("atcs", _wt_request(_suit_problem(), "atcs", {"suitability": True}))
    assert _order(result, "M1") == ["J2-1", "J1-1"]


def test_atcs_suitability_off_keeps_the_op_id_tie_break() -> None:
    result = _run("atcs", _wt_request(_suit_problem(), "atcs", {"suitability": False}))
    assert _order(result, "M1") == ["J1-1", "J2-1"]


@pytest.mark.parametrize(
    "params",
    [
        {"K1": 5.0, "K2": 0.7, "flexibility": False, "suitability": False},
        # Factors are 1 here anyway (one machine, all costs 0), so on must not change a thing.
        {"K1": 5.0, "K2": 0.7, "flexibility": True, "suitability": True},
    ],
    ids=["both-off", "both-on-neutral"],
)
def test_atcs_factors_leave_14_2_1_unchanged(params: dict[str, Any]) -> None:
    result = _run("atcs", _p1421(params))
    assert _order(result, "M1") == SEQ_1421
    assert result.objective_values[WT] == pytest.approx(98.0)


def test_atcs_factors_off_keep_the_automatic_k_of_14_2_1() -> None:
    result = _run("atcs", _p1421({"flexibility": False, "suitability": False}))
    run = _runs_by_center(result)["WC-M1"]
    assert (run.K1, run.K2) == pytest.approx((4.97, 0.671), abs=K_TOL)
    assert _order(result, "M1") == SEQ_1421


# --- macro jobs (T-132, REQ-095; Pinedo p.93, p.448) -----------------------------------------
#
# One machine M1, run 10 each (pbar 10, so merge_window defaults to 10), no setup function
# (every setup 0), explicit K1 = 10 (divisor K1 * pbar = 100). U (w 1) and V (w 5) are the
# merge candidates; Z (w 2.5 or 4.5) has a due more than one pbar from both, so it never
# merges. Individual indexes at t = 0: U 0.1, V 0.5; the macro U+V is (1 + 5) / (10 + 10) =
# 0.3 (slack 0 under any reading of the group's dloc, since both dues are <= its run).
# Without macros, V goes first and Z beats U at t = 10, so U and V are never adjacent.


def _macro_problem(
    z_weight: float,
    v_due: float,
    z_due: float,
    v_gate: float | None = None,
    *,
    u_weight: float = 1.0,
    v_weight: float = 5.0,
) -> dict[str, Any]:
    v_ops = [op("V-1", {"M1": 10.0})]
    machines = [machine("M1")]
    if v_gate is not None:  # a predecessor on another center holds V-1's gate until v_gate
        v_ops = [
            op("V-0", {"M2": v_gate}, center="B"),
            op("V-1", {"M1": 10.0}, after=["V-0"]),
        ]
        machines.append(machine("M2", center="B"))
    jobs = [
        _job("U", [op("U-1", {"M1": 10.0})], due=0.0, weight_override=u_weight),
        _job("V", v_ops, due=v_due, weight_override=v_weight),
        _job("Z", [op("Z-1", {"M1": 10.0})], due=z_due, weight_override=z_weight),
    ]
    return _problem(jobs, machines)


def _macro_run(problem: dict[str, Any], **params: Any) -> SolveResult:
    return _run("atcs", _wt_request(problem, "atcs", {"K1": 10.0} | params))


def _adjacent(sequence: list[str], a: str, b: str) -> bool:
    return abs(sequence.index(a) - sequence.index(b)) == 1


def _assert_one_assignment_per_op(result: SolveResult, op_ids: set[str]) -> None:
    placed = [a.operation_id for a in result.schedule.assignments]
    assert sorted(placed) == sorted(op_ids)


def test_macro_off_does_not_group_u_and_v() -> None:
    # Z (2.5): t=0 U 0.1, V 0.5, Z 0.25*e^-0.15 = 0.215 -> V; t=10 Z 0.238 > U 0.1.
    result = _macro_run(_macro_problem(2.5, v_due=10.0, z_due=25.0))
    assert _order(result, "M1") == ["V-1", "Z-1", "U-1"]


def test_macro_runs_members_one_pbar_apart_back_to_back() -> None:
    # Dues 0 and 10 are exactly one pbar apart (inclusive). Macro 0.3 > Z 0.215 -> U+V first.
    result = _macro_run(_macro_problem(2.5, v_due=10.0, z_due=25.0), macro_jobs=True)
    sequence = _order(result, "M1")
    assert set(sequence[:2]) == {"U-1", "V-1"}
    assert sequence[2] == "Z-1"
    _assert_one_assignment_per_op(result, {"U-1", "V-1", "Z-1"})


def test_macro_index_uses_summed_weight_and_run_not_the_best_member() -> None:
    # Z (4.5): 0.45 * e^-0.15 = 0.387 sits between the macro (0.3) and V alone (0.5). Summed
    # w and p put Z first; an index from V alone would put the macro first.
    result = _macro_run(_macro_problem(4.5, v_due=10.0, z_due=25.0), macro_jobs=True)
    sequence = _order(result, "M1")
    assert sequence[0] == "Z-1"
    assert set(sequence[1:]) == {"U-1", "V-1"}
    _assert_one_assignment_per_op(result, {"U-1", "V-1", "Z-1"})


def test_macro_does_not_merge_dues_three_pbar_apart() -> None:
    # Dues 0 and 30; Z (3.0, due 15) is 15 from both. t=0: U 0.1, V 0.5*e^-0.2 = 0.409,
    # Z 0.3*e^-0.05 = 0.285 -> V; t=10: Z 0.3 > U 0.1. Same as macros off.
    problem = _macro_problem(3.0, v_due=30.0, z_due=15.0)
    on = _macro_run(problem, macro_jobs=True)
    off = _macro_run(problem)
    assert _order(on, "M1") == _order(off, "M1") == ["V-1", "Z-1", "U-1"]


def test_macro_merge_window_param_widens_the_merge() -> None:
    # The same dues 30 apart merge with merge_window = 30.
    result = _macro_run(_macro_problem(3.0, v_due=30.0, z_due=15.0), macro_jobs=True,
                        merge_window=30.0)  # fmt: skip
    assert _adjacent(_order(result, "M1"), "U-1", "V-1")


def test_macro_member_whose_gate_is_not_met_returns_to_the_pool() -> None:
    # V-1 waits for V-0 (M2, run 15). The macro (0.3) beats Z (0.215) at t=0, so U-1 runs
    # 0..10; V-1's gate (15) is not met at 10, so V-1 is released and Z-1 runs at 10 instead
    # of M1 idling for V-1.
    result = _macro_run(_macro_problem(2.5, v_due=10.0, z_due=25.0, v_gate=15.0),
                        macro_jobs=True)  # fmt: skip
    assert _order(result, "M1") == ["U-1", "Z-1", "V-1"]
    assert _start(result, "Z-1") == pytest.approx(10.0)
    assert _start(result, "V-1") >= 15.0
    _assert_one_assignment_per_op(result, {"U-1", "V-0", "V-1", "Z-1"})


# Heavy head, light follower: U (w 5, due 0), V (w 1, due 10), Z (w 2.5, due 25), K1 = 10.
# t=0: U alone 0.5, V 0.1, Z 0.215; macro U+V (5 + 1) / 20 = 0.3 > Z. Once U is placed, V's own
# index (0.1) loses to Z (0.25 * e^-0.05 = 0.238), so only the follow-on rule keeps V right
# behind U: this case goes red when the members do not follow the head.


@pytest.mark.parametrize(
    ("macro", "v_gate", "expected"),
    [
        (False, None, ["U-1", "Z-1", "V-1"]),
        (True, None, ["U-1", "V-1", "Z-1"]),
        # V's predecessor ends at 5, before U finishes at 10: the gate is met, V follows.
        (True, 5.0, ["U-1", "V-1", "Z-1"]),
        # V's predecessor ends at 15, after U finishes: V is released, Z runs at 10.
        (True, 15.0, ["U-1", "Z-1", "V-1"]),
    ],
    ids=["macros-off", "macros-on", "gate-met", "gate-not-met"],
)
def test_macro_members_follow_the_head_only_while_their_gate_is_met(
    macro: bool, v_gate: float | None, expected: list[str]
) -> None:
    problem = _macro_problem(2.5, 10.0, 25.0, v_gate, u_weight=5.0, v_weight=1.0)
    result = _macro_run(problem, macro_jobs=macro)
    assert _order(result, "M1") == expected
    ops = {"U-1", "V-1", "Z-1"} | ({"V-0"} if v_gate is not None else set())
    _assert_one_assignment_per_op(result, ops)


def test_macro_needs_zero_setup_in_both_directions() -> None:
    # U and V as in the merge case, but a setup function makes V -> U cost 5 (U -> V 0):
    # not zero both ways, so no macro; the result equals macros off.
    problem = _macro_problem(2.5, v_due=10.0, z_due=25.0)
    for job, family in zip(problem["jobs"], ("fu", "fv", "fz"), strict=True):
        job["operations"][-1]["setup_family"] = family
    problem["machines"][0]["setup_function_id"] = "s"
    problem["setup_functions"] = [
        {
            "id": "s",
            "terms": [],
            "family_matrix": [{"from_family": "fv", "to_family": "fu", "seconds": 5.0}],
        }
    ]
    on = _macro_run(problem, macro_jobs=True)
    off = _macro_run(problem)
    assert _order(on, "M1") == _order(off, "M1")
    assert not _adjacent(_order(on, "M1"), "U-1", "V-1")


def test_macro_follower_never_runs_over_a_pinned_window() -> None:
    # U+V merge (as above); P-1 is pinned to start exactly at 12 on M1. The member that
    # follows the head would run [10, 20) over P's window, so it must wait (review: the
    # follower path skipped the pinned-window check and the plan failed to verify).
    problem = _macro_problem(2.5, v_due=10.0, z_due=25.0, u_weight=5.0, v_weight=1.0)
    pinned = op(
        "P-1", {"M1": 10.0}, pin={"machine_id": "M1", "earliest_start": 12.0, "latest_start": 12.0}
    )
    problem["jobs"].append(_job("P", [pinned], due=100.0, weight_override=0.1))

    result = _macro_run(problem, macro_jobs=True)

    assert result.verified, [i.code for i in (*result.issues, *result.schedule.issues)]
    assert _start(result, "P-1") == 12.0


# --- every option on, generated problems (adversarial) ---------------------------------------


@pytest.mark.parametrize("seed", range(40))
def test_atcs_with_every_option_on_keeps_the_contract(seed: int) -> None:
    request = _with_params(
        random_request(seed),
        "atcs",
        {"flexibility": True, "suitability": True, "macro_jobs": True},
    )
    result = solve(request, solver=get_solver("atcs"))
    codes = {issue.code for issue in result.issues}
    assert "solver_error" not in codes, (seed, result.issues)
    if result.schedule.status in ("FEASIBLE", "OPTIMAL"):
        assert result.verified, (seed, result.issues)
        ops = [a.operation_id for a in result.schedule.assignments]
        assert len(ops) == len(set(ops)), seed
    else:
        assert result.schedule.issues or result.issues, seed
