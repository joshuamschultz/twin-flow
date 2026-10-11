"""Shifting bottleneck for makespan and weighted tardiness (T-141, T-142; COMP-013, COMP-005,
REQ-099, REQ-100, D-077; Pinedo Alg 7.2.1 p.189-195, Sec 7.3 p.196-202).

`twinflow.scheduling.algorithms.shifting_bottleneck` registers `sb_makespan` and `sb_wt`
(family `decomposition`, `deterministic=True`, every `Feature`). API specified here:

- `shifting_bottleneck(index, mode, params) -> SbOutcome`, a pure function: `mode` is
  `"makespan"` or `"weighted_tardiness"`, `params` the solver's `SolveContext.params`
  (`K`, `Kc`, `criticality` in {"completion_delta", "subproblem_value"}, `reopt_rounds`,
  `subproblem`). An unknown `criticality` value raises ValueError (never a silent default).
- `SbOutcome(choice, sequences, trace)`: op -> machine, machine -> op ids (the decoder's
  input), and one `SbIteration` per main iteration (re-optimization not traced).
- `SbIteration(chosen, subproblems)`: the work center fixed in that iteration and, per
  center still open, its `SbSubproblem(sequences, value, criticality)`: the subproblem's
  machine sequences, its value (Lmax for makespan; sum w_k (C - d^k)^+ for weighted
  tardiness) and its criticality (the value for makespan and for `subproblem_value`).

The registered solvers call this function with `profile.method_params[name]` and decode
the outcome, so `solve()` results are the pure function's sequences, verified.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

import pytest
from kernel_cases import (
    compile_request,
    load_request,
    machine,
    machine_choice,
    machine_sequences,
    only_choice,
    random_job_shop,
    request_from,
)
from kernel_cases import op as kernel_op

from twinflow.scheduling.algorithms.sb_subproblems import SubOp, lmax_exact
from twinflow.scheduling.algorithms.shifting_bottleneck import (
    SbIteration,
    SbOutcome,
    SbSubproblem,
    shifting_bottleneck,
)
from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.graph import DisjunctiveGraph, GraphCycle
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import ObjectiveKind, SolveRequest, SolveResult
from twinflow.scheduling.registry import available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature, ParamValue
from twinflow.scheduling.solver import solve

Mode = Literal["makespan", "weighted_tardiness"]

BOOK_7_3_1: dict[str, ParamValue] = {"K": 0.1, "criticality": "subproblem_value", "reopt_rounds": 0}
"""Example 7.3.1 settings (p.200-203): K = 0.1, criticality by subproblem value, no reopt."""


# --- helpers ---------------------------------------------------------------------------------


def _with_params(
    request: SolveRequest, name: str, params: Mapping[str, ParamValue]
) -> SolveRequest:
    data = request.model_dump()
    data["profile"]["method_params"] = {name: dict(params)}
    return SolveRequest.model_validate(data)


def _run(name: str, request: SolveRequest) -> SolveResult:
    result = solve(request, solver=get_solver(name))
    assert result.verified, (name, result.issues)
    return result


def _outcome(fixture: str, mode: Mode, params: Mapping[str, ParamValue]) -> SbOutcome:
    index = ProblemIndex.build(compile_request(load_request(fixture)).problem)
    return shifting_bottleneck(index, mode, params)


def _values(iteration: SbIteration) -> dict[str, float]:
    return {center: sub.value for center, sub in iteration.subproblems.items()}


def _nonempty(sequences: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    """Machine -> op ids as tuples, machines without ops left out (comparable forms)."""
    return {m: tuple(seq) for m, seq in sequences.items() if seq}


# --- registry ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["sb_makespan", "sb_wt"])
def test_registered_as_deterministic_decomposition_with_every_feature(name: str) -> None:
    info = {i.name: i for i in available()}[name]

    assert info.family == "decomposition"
    assert info.deterministic is True
    assert info.capabilities == frozenset(Feature)
    assert info.module == "twinflow.scheduling.algorithms.shifting_bottleneck"


# --- T-141: makespan, Pinedo Example 7.1.1 (p.190-193) -----------------------------------------


def test_7_1_1_sb_makespan_returns_verified_makespan_28() -> None:
    result = _run("sb_makespan", load_request("pinedo_7_1_1"))

    assert result.objective_values[ObjectiveKind.MAKESPAN] == 28.0


def test_7_1_1_first_bottleneck_is_machine_1_with_the_book_lmax_values() -> None:
    outcome = _outcome("pinedo_7_1_1", "makespan", {})

    first = outcome.trace[0]
    assert _values(first) == {"WC-M1": 5.0, "WC-M2": 5.0, "WC-M3": 4.0, "WC-M4": 0.0}  # p.192
    assert first.chosen == "WC-M1"  # tie with WC-M2 broken by center id
    assert first.subproblems["WC-M1"] == SbSubproblem(
        sequences={"M1": ("1-1", "2-2", "3-1")}, value=5.0, criticality=5.0
    )  # the unique Lmax-5 sequence 1, 2, 3


def test_7_1_1_second_iteration_matches_the_book() -> None:
    outcome = _outcome("pinedo_7_1_1", "makespan", {})

    second = outcome.trace[1]
    assert _values(second) == {"WC-M2": 1.0, "WC-M3": 1.0, "WC-M4": 0.0}  # p.193, Cmax 27
    assert second.chosen == "WC-M2"


def test_7_1_1_trace_fixes_each_center_once() -> None:
    outcome = _outcome("pinedo_7_1_1", "makespan", {})

    assert sorted(it.chosen for it in outcome.trace) == ["WC-M1", "WC-M2", "WC-M3", "WC-M4"]
    for k, iteration in enumerate(outcome.trace):
        assert iteration.chosen in iteration.subproblems
        assert len(iteration.subproblems) == 4 - k


def test_7_1_1_outcome_decodes_to_28_and_is_what_solve_returns() -> None:
    request = load_request("pinedo_7_1_1")
    index = ProblemIndex.build(compile_request(request).problem)
    outcome = shifting_bottleneck(index, "makespan", {})

    decoded = decode(index, outcome.choice, outcome.sequences)

    assert isinstance(decoded, Decoded), decoded
    assert max(decoded.ends) == 28.0
    result = _run("sb_makespan", request)
    assert _nonempty(machine_sequences(result.schedule)) == _nonempty(outcome.sequences)


# --- T-141: Pinedo Example 7.2.3, delayed precedence (p.195-196) -------------------------------


def test_7_2_3_sb_makespan_returns_verified_makespan_8_without_a_cycle() -> None:
    result = _run("sb_makespan", load_request("pinedo_7_2_3"))

    assert result.objective_values[ObjectiveKind.MAKESPAN] == 8.0


def test_7_2_3_first_bottleneck_is_machine_3() -> None:
    outcome = _outcome("pinedo_7_2_3", "makespan", {})

    assert outcome.trace[0].chosen == "WC-M3"
    assert outcome.trace[0].subproblems["WC-M3"].value == 4.0


def test_7_2_3_outcome_builds_an_acyclic_graph() -> None:
    request = load_request("pinedo_7_2_3")
    index = ProblemIndex.build(compile_request(request).problem)
    outcome = shifting_bottleneck(index, "makespan", {})

    graph = DisjunctiveGraph.build(index, outcome.choice, outcome.sequences)

    assert max(graph.completions().values()) == 8.0


def test_7_2_3_the_book_cycle_is_reported_by_the_graph() -> None:
    """Characterization of the cycle the book describes: M1 = job 2 then job 1 and M2 =
    job 1 then job 2 close 2-1 -> 2-2 -> 1-1 -> 1-2 -> 2-1 (the dp forbids it, see
    test_sb_subproblems)."""
    request = load_request("pinedo_7_2_3")
    index = ProblemIndex.build(compile_request(request).problem)
    sequences = {"M3": ("3-1", "4-1"), "M1": ("2-2", "1-1"), "M2": ("1-2", "2-1")}

    with pytest.raises(GraphCycle):
        DisjunctiveGraph.build(index, only_choice(request), sequences)


# --- T-141: re-optimization, determinism, params ------------------------------------------------


def _per_machine_centers(request: SolveRequest) -> SolveRequest:
    """A `random_job_shop` request with one work center per machine (a true job shop)."""
    data = request.model_dump()
    problem = data["problem"]
    for m in problem["machines"]:
        m["work_center_id"] = f"WC-{m['id']}"
    for job in problem["jobs"]:
        for operation in job["operations"]:
            operation["work_center_id"] = f"WC-{operation['options'][0]['machine_id']}"
    problem["work_centers"] = [{"id": f"WC-{m['id']}"} for m in problem["machines"]]
    return SolveRequest.model_validate(data)


@pytest.mark.parametrize("seed", range(20))
def test_reopt_round_1_is_never_worse_than_round_0(seed: int) -> None:
    request = _per_machine_centers(random_job_shop(seed)[0])

    without = _run("sb_makespan", _with_params(request, "sb_makespan", {"reopt_rounds": 0}))
    with_reopt = _run("sb_makespan", _with_params(request, "sb_makespan", {"reopt_rounds": 1}))

    makespan = ObjectiveKind.MAKESPAN
    assert with_reopt.objective_values[makespan] <= without.objective_values[makespan], seed


@pytest.mark.parametrize("name", ["sb_makespan", "sb_wt"])
@pytest.mark.parametrize("seed", [2, 7, 11])
def test_same_input_gives_the_same_schedule(name: str, seed: int) -> None:
    request = _per_machine_centers(random_job_shop(seed)[0])

    first, second = _run(name, request), _run(name, request)

    assert first.schedule.assignments == second.schedule.assignments


def test_unknown_criticality_is_refused_not_defaulted() -> None:
    index = ProblemIndex.build(compile_request(load_request("pinedo_7_3_1")).problem)

    with pytest.raises(ValueError, match="criticality"):
        shifting_bottleneck(index, "weighted_tardiness", {"criticality": "loudest"})


# --- T-142: weighted tardiness, Pinedo Example 7.3.1 (p.200-203) -------------------------------


def test_7_3_1_iteration_1_subproblems_match_the_book() -> None:
    outcome = _outcome("pinedo_7_3_1", "weighted_tardiness", BOOK_7_3_1)

    first = outcome.trace[0]
    assert first.subproblems == {
        "WC-M1": SbSubproblem(
            sequences={"M1": ("1-1", "2-2", "3-3")}, value=18.0, criticality=18.0
        ),
        "WC-M2": SbSubproblem(
            sequences={"M2": ("3-2", "1-2", "2-3")}, value=16.0, criticality=16.0
        ),
        "WC-M3": SbSubproblem(sequences={"M3": ("3-1", "2-1", "1-3")}, value=4.0, criticality=4.0),
    }  # (1,1),(1,2),(1,3) / (2,3),(2,1),(2,2) / (3,3),(3,2),(3,1)
    assert first.chosen == "WC-M1"


def test_7_3_1_book_settings_give_verified_weighted_tardiness_28() -> None:
    request = _with_params(load_request("pinedo_7_3_1"), "sb_wt", BOOK_7_3_1)

    result = _run("sb_wt", request)

    assert result.objective_values[ObjectiveKind.WEIGHTED_TARDINESS] == 28.0  # p.203
    outcome = _outcome("pinedo_7_3_1", "weighted_tardiness", BOOK_7_3_1)
    assert _nonempty(machine_sequences(result.schedule)) == _nonempty(outcome.sequences)


def test_7_3_1_with_reopt_1_is_at_most_28() -> None:
    params: dict[str, ParamValue] = {**BOOK_7_3_1, "reopt_rounds": 1}
    request = _with_params(load_request("pinedo_7_3_1"), "sb_wt", params)

    value = _run("sb_wt", request).objective_values[ObjectiveKind.WEIGHTED_TARDINESS]

    print(f"\n7.3.1 sb_wt K 0.1 subproblem_value reopt 1: weighted tardiness {value}")
    assert 18.0 <= value <= 28.0  # 18 is the oracle optimum (test_fixtures)


def test_7_3_1_default_criticality_completion_delta_runs_and_verifies() -> None:
    result = _run("sb_wt", load_request("pinedo_7_3_1"))

    value = result.objective_values[ObjectiveKind.WEIGHTED_TARDINESS]
    print(f"\n7.3.1 sb_wt defaults (completion_delta, K 2.0, reopt 1): {value}")
    assert value >= 18.0


def test_sb_wt_without_due_dates_still_verifies() -> None:
    """7.1.1 has no due dates: every local due is +inf, every criticality 0."""
    result = _run("sb_wt", load_request("pinedo_7_1_1"))

    assert result.objective_values[ObjectiveKind.WEIGHTED_TARDINESS] == 0.0


# --- T-143: multi-machine work centers (FJc; REQ-101, D-077, Pinedo p.212) ---------------------
#
# Three centers A {MA}, B {MB1, MB2}, C {MC}; no setups, no due dates. Fixed seconds:
#   job 1: A 1 -> B 2 -> C 6      job 2: B 6 -> C 1 -> A 1      job 3: C 1 -> A 1 -> B 1
# With B on one machine, iteration 1 has only job arcs: Cmax(G) = 9 and B's single-machine
# Lmax subproblem is, by hand (r = head, d = Cmax - work after the op):
#   1-2: r 1, p 2, d 3     2-1: r 0, p 6, d 7     3-3: r 2, p 1, d 9
# The exact optimum idles until 1: 1-2, 2-1, 3-3 gives L = 0, 2, 1, so Lmax 2 (unique; every
# order starting with 2-1 is at least 5, 3-3 first is 4, 1-2, 3-3, 2-1 is 3). A non-delay
# dispatch starts 2-1 at 0 and reaches 5, so this case tells the single-machine algorithm
# (exact 1|r_j,dp|Lmax) from the parallel-machine dispatch.
# With two machines, MB1 is the fast one (1-2 runs 2 vs 3, 3-3 runs 1 vs 1.5) and 2-1 is
# eligible only on MB2.

_CENTERS_3 = ("A", "B", "C")
_ROUTES_3 = {
    "1": (("A", 1.0), ("B", 2.0), ("C", 6.0)),
    "2": (("B", 6.0), ("C", 1.0), ("A", 1.0)),
    "3": (("C", 1.0), ("A", 1.0), ("B", 1.0)),
}
_SLOW_MB2 = {"1-2": 3.0, "2-1": 6.0, "3-3": 1.5}
_MB2_ONLY = {"2-1"}


def _three_center_request(b_machines: Sequence[str]) -> SolveRequest:
    """The problem above with center B on `b_machines` ("MB1", "MB2" or both)."""
    machines = [machine("MA", center="A"), machine("MC", center="C")]
    machines += [machine(m, center="B") for m in b_machines]
    jobs = []
    for job_id, route in _ROUTES_3.items():
        ops = []
        for k, (center, run) in enumerate(route):
            op_id = f"{job_id}-{k + 1}"
            options = {f"M{center}": run} if center != "B" else _b_options(op_id, run, b_machines)
            after = [f"{job_id}-{k}"] if k else []
            ops.append(kernel_op(op_id, options, center=center, after=after))
        jobs.append({"id": job_id, "operations": ops})
    problem = {
        "id": "fjc-3-centers",
        "work_centers": [{"id": c} for c in _CENTERS_3],
        "machines": machines,
        "jobs": jobs,
    }
    return request_from(problem)


def _b_options(op_id: str, run: float, b_machines: Sequence[str]) -> dict[str, float]:
    """One machine: the hand-computed runs. Both: MB1 as fast, MB2 slower, 2-1 on MB2 only."""
    if len(b_machines) == 1:
        return {b_machines[0]: run}
    options = {} if op_id in _MB2_ONLY else {"MB1": run}
    return options | {"MB2": _SLOW_MB2[op_id]}


def _three_center_outcome(request: SolveRequest) -> SbOutcome:
    index = ProblemIndex.build(compile_request(request).problem)
    return shifting_bottleneck(index, "makespan", {})


def test_fjc_one_machine_center_runs_the_single_machine_lmax_subproblem() -> None:
    outcome = _three_center_outcome(_three_center_request(["MB1"]))

    by_hand = lmax_exact(
        [SubOp("1-2", 1.0, 2.0, 3.0), SubOp("2-1", 0.0, 6.0, 7.0), SubOp("3-3", 2.0, 1.0, 9.0)],
        [],
    )
    assert by_hand.sequence == ("1-2", "2-1", "3-3") and by_hand.value == 2.0
    assert outcome.trace[0].subproblems["B"] == SbSubproblem(
        sequences={"MB1": by_hand.sequence}, value=by_hand.value, criticality=by_hand.value
    )


def test_fjc_center_with_an_idle_second_machine_equals_the_hand_split_problem() -> None:
    """B lists MB2 but no op can use it: the same result as B written with MB1 alone."""
    one = _three_center_request(["MB1"])
    data = one.model_dump()
    data["problem"]["machines"] = [*data["problem"]["machines"], machine("MB2", center="B")]
    with_idle = SolveRequest.model_validate(data)

    expected = _three_center_outcome(one)
    actual = _three_center_outcome(with_idle)

    assert actual.trace == expected.trace
    assert _nonempty(actual.sequences) == _nonempty(expected.sequences)
    for name in ("sb_makespan", "sb_wt"):
        assert _run(name, with_idle).schedule.assignments == _run(name, one).schedule.assignments


def test_fjc_two_machine_center_assigns_by_eligibility_and_speed_and_verifies() -> None:
    request = _three_center_request(["MB1", "MB2"])

    outcome = _three_center_outcome(request)
    result = _run("sb_makespan", request)

    choice = machine_choice(result.schedule)
    assert choice["2-1"] == "MB2"  # eligibility: its only option
    assert choice["1-2"] == "MB1"  # speed: 2 on MB1 vs 3 on MB2, and MB2 is busy 0-6
    assert outcome.choice["2-1"] == "MB2" and outcome.choice["1-2"] == "MB1"
    sub_b = outcome.trace[[it.chosen for it in outcome.trace].index("B")].subproblems["B"]
    assert set(sub_b.sequences) <= {"MB1", "MB2"}
    assert sorted(o for seq in sub_b.sequences.values() for o in seq) == ["1-2", "2-1", "3-3"]
    single = _run("sb_makespan", _three_center_request(["MB1"]))
    makespan = ObjectiveKind.MAKESPAN
    assert result.objective_values[makespan] <= single.objective_values[makespan]


def test_fjc_two_machine_center_sb_wt_verifies_with_eligible_machines() -> None:
    request = _three_center_request(["MB1", "MB2"])

    result = _run("sb_wt", request)

    choice = machine_choice(result.schedule)
    assert choice["2-1"] == "MB2"
    assert all(choice[o] in ("MB1", "MB2") for o in ("1-2", "3-3"))


# --- T-143: scale, anytime answer, solver-objective acceptance ----------------------------------
#
# - Deadline: past `deadline` before every center is fixed, the remaining centers are each
#   fixed by their subproblem on the current graph, in center-id order, without a
#   criticality comparison or re-optimization (an anytime answer, never TimeoutError); the
#   trace then records one iteration per such center with only that center's subproblem.
# - `shifting_bottleneck(..., evaluate=f)`: f(choice, sequences) -> a sort key (smaller is
#   better) of a complete plan. Once every center is fixed, re-optimization keeps a re-solved
#   center only when f does not rise (the solver passes its `Scorer` over `fast_values` of
#   the decoded plan); a final round then re-optimizes every center. So the returned plan has
#   the smallest key f ever returned.
# - A machine's calendar slows it in the center subproblem: run / (working share of its
#   calendar span) (a [mine] reading of "speed").
# - `K2` (positive) sets the setup factor's K2 for every center.


def _scheduled_once(index: ProblemIndex, sequences: Mapping[str, Sequence[str]]) -> bool:
    placed = sorted(o for seq in sequences.values() for o in seq)
    return placed == sorted(index.op_ids)


@pytest.mark.parametrize("mode", ["makespan", "weighted_tardiness"])
@pytest.mark.parametrize("seed", [2, 7])
def test_past_deadline_completes_every_center_instead_of_timing_out(mode: Mode, seed: int) -> None:
    request = _per_machine_centers(random_job_shop(seed)[0])
    index = ProblemIndex.build(compile_request(request).problem)

    outcome = shifting_bottleneck(index, mode, {}, deadline=0.0)

    centers = sorted({op.work_center_id for op in index.ops})
    assert [it.chosen for it in outcome.trace] == centers  # center-id order, no comparison
    assert all(list(it.subproblems) == [it.chosen] for it in outcome.trace)
    assert _scheduled_once(index, outcome.sequences)
    assert isinstance(decode(index, outcome.choice, outcome.sequences), Decoded)


@pytest.mark.parametrize("name", ["sb_makespan", "sb_wt"])
def test_a_spent_budget_still_returns_a_verified_schedule(name: str) -> None:
    request = _per_machine_centers(random_job_shop(7)[0])

    result = solve(request, solver=get_solver(name), budget_seconds=1e-9)

    assert result.verified, (result.schedule.status, [i.code for i in result.issues])


@pytest.mark.parametrize("seed", range(6))
def test_reoptimization_keeps_the_plan_with_the_smallest_evaluated_key(seed: int) -> None:
    request = _per_machine_centers(random_job_shop(seed)[0])
    index = ProblemIndex.build(compile_request(request).problem)
    keys: list[tuple[float, ...]] = []

    def total_completion(
        choice: Mapping[str, str], sequences: Mapping[str, tuple[str, ...]]
    ) -> tuple[float, ...]:
        assert _scheduled_once(index, sequences), "evaluate sees complete plans only"
        decoded = decode(index, choice, sequences)
        assert isinstance(decoded, Decoded)
        keys.append((sum(decoded.completion(job.id) for job in index.jobs),))
        return keys[-1]

    outcome = shifting_bottleneck(index, "makespan", {}, evaluate=total_completion)

    assert keys, "the evaluator was consulted"
    assert total_completion(outcome.choice, outcome.sequences) == min(keys)


def test_a_calendar_slows_its_machine_in_the_center_subproblem() -> None:
    """MA works half the time, MB always; same runs: the op goes to MB (MA wins the id tie
    without the slowdown)."""
    problem = {
        "id": "calendar-speed",
        "work_centers": [{"id": "WC"}],
        "machines": [machine("MA", calendar_id="HALF"), machine("MB")],
        "calendars": [{"id": "HALF", "intervals": [(0.0, 50.0), (100.0, 150.0)]}],
        "jobs": [{"id": "1", "operations": [kernel_op("1-1", {"MA": 10.0, "MB": 10.0})]}],
    }
    index = ProblemIndex.build(compile_request(request_from(problem)).problem)

    for mode in ("makespan", "weighted_tardiness"):
        assert shifting_bottleneck(index, mode, {}).choice == {"1-1": "MB"}, mode


@pytest.mark.parametrize("value", [0, -1.0, "big", True])
def test_a_bad_k2_is_refused(value: ParamValue) -> None:
    index = ProblemIndex.build(compile_request(load_request("pinedo_7_3_1")).problem)

    with pytest.raises(ValueError, match="K2"):
        shifting_bottleneck(index, "weighted_tardiness", {"K2": value})
