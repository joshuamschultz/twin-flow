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
    machine_sequences,
    only_choice,
    random_job_shop,
)

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
