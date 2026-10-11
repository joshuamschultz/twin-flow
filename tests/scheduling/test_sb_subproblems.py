"""Shifting bottleneck subproblem solvers (T-140, COMP-013, REQ-099, REQ-101, D-077).

`twinflow.scheduling.algorithms.sb_subproblems` holds pure functions over plain data (no
Problem, no index), so shifting bottleneck, five-phase and tests share them. API specified
by these tests:

- `SubOp(id, release, run, due=inf, dues=())`: one op of a one-machine subproblem; `due` is
  the Lmax due date, `dues` the (weight, local due) pairs of the weighted-tardiness
  subproblem (one per job sink the op can delay, Pinedo p.198).
- `CenterOp(id, release, runs, due=inf, dues=())`: the same for a multi-machine center;
  `runs` maps each eligible machine id to the op's run there (eligibility = its keys).
- `DelayedPrecedence(before, after, delay)`: `after` may start no earlier than `delay`
  after `before` completes (Pinedo p.194-196 reading: completion to start). The caller
  derives `delay` from a graph path; a sequence with `after` ahead of `before` is
  infeasible (it would close a cycle).
- Timing of a sequence (semi-active): start = max(release, the machine's previous
  completion, completion(a) + delay for every dp a -> op); completion = start +
  setup(previous op or None, op) + run. The setup is attached (it starts at `start`).
- `Setup = Callable[[str | None, str], float]` (previous op id or None for the machine
  state, op id) for one machine; `CenterSetup = Callable[[str, str | None, str], float]`
  (machine id first) for a center. Absent means zero setups.
- `SubResult(sequence, value)` and `CenterResult(sequences, value)`; `value` is Lmax =
  max(C - due) for the Lmax solvers and sum over dues of w * (C - d)^+ for weighted
  tardiness.
- `sequence_lmax(ops, sequence, dp, *, setup=None) -> float` evaluates one sequence; it
  raises `DelayedPrecedenceViolation` (a ValueError) when the sequence breaks a dp.
- `lmax_exact(ops, dp, *, setup=None)`: exact branch and bound for 1|r_j,dp|Lmax.
- `lmax_schrage(ops, dp, *, setup=None)`: Schrage (non-delay EDD over released, dp-ready
  ops); `lmax_schrage_swaps(...)`: Schrage then adjacent-swap improvement.
- `solve_lmax(ops, dp, *, setup=None, exact_limit=10)`: exact up to `exact_limit` ops,
  Schrage plus swaps beyond (SDD COMP-013).
- `multi_due_atc_index(op, t, *, pbar, K) -> float`: sum over (w, d) in `op.dues` of
  (w / p) * exp(-(d - p + (r - t)^+)^+ / (K * pbar)) (Pinedo p.199, setup factor 1).
- `wt_atc_sequence(ops, dp, *, K, pbar=None, setup=None, setup_scale=inf) -> SubResult`:
  dispatch by the multi-due index whenever the machine frees (t = earliest time the machine
  is usable); `pbar` defaults to the integer part of the mean run (p.199); `setup_scale`
  (K2 sbar) multiplies the index by the setup factor exp(-s / setup_scale) (T-143).
- `solve_center_parallel(ops, machines, dp, *, setup=None, objective="lmax", K=2.0,
  setup_scale=inf) -> CenterResult`: the Pm|r_j,s_jk,M_j,dp|Lmax (or weighted tardiness)
  center version (p.212); with one machine it reduces to the single-machine algorithm.

Every brute force below is written here, independent of the module.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Mapping, Sequence

import numpy as np
import pytest
from kernel_cases import compile_request, load_request, machine, only_choice, op, request_from

from twinflow.scheduling.algorithms.sb_subproblems import (
    CenterOp,
    CenterResult,
    DelayedPrecedence,
    DelayedPrecedenceViolation,
    SubOp,
    SubResult,
    lmax_exact,
    lmax_schrage,
    lmax_schrage_swaps,
    multi_due_atc_index,
    sequence_lmax,
    solve_center_parallel,
    solve_lmax,
    wt_atc_sequence,
)
from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.graph import DisjunctiveGraph
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import CompiledProblem, SolveRequest
from twinflow.scheduling.verifier import verify

EPS = 1e-9
Setup = Callable[[str | None, str], float]
CenterSetup = Callable[[str, str | None, str], float]


# --- independent evaluators and brute force ----------------------------------------------------


def _completions(
    ops: Sequence[SubOp],
    sequence: Sequence[str],
    dp: Sequence[DelayedPrecedence],
    setup: Setup | None = None,
) -> dict[str, float] | None:
    """Semi-active completions of `sequence`; None when it breaks a delayed precedence."""
    by_id = {o.id: o for o in ops}
    position = {op_id: k for k, op_id in enumerate(sequence)}
    if any(position[c.before] > position[c.after] for c in dp):
        return None
    done: dict[str, float] = {}
    free, previous = -math.inf, None
    for op_id in sequence:
        o = by_id[op_id]
        gates = [done[c.before] + c.delay for c in dp if c.after == op_id]
        start = max([o.release, free, *gates])
        free = start + (setup(previous, op_id) if setup else 0.0) + o.run
        done[op_id], previous = free, op_id
    return done


def _lmax(ops: Sequence[SubOp], completions: Mapping[str, float]) -> float:
    return max(completions[o.id] - o.due for o in ops)


def _weighted_tardiness(ops: Sequence[SubOp], completions: Mapping[str, float]) -> float:
    return sum(w * max(completions[o.id] - d, 0.0) for o in ops for w, d in o.dues)


def _brute_lmax(
    ops: Sequence[SubOp], dp: Sequence[DelayedPrecedence], setup: Setup | None = None
) -> float:
    best = math.inf
    for perm in itertools.permutations([o.id for o in ops]):
        completions = _completions(ops, perm, dp, setup)
        if completions is not None:
            best = min(best, _lmax(ops, completions))
    return best


def _check_single(
    ops: Sequence[SubOp],
    dp: Sequence[DelayedPrecedence],
    result: SubResult,
    setup: Setup | None = None,
) -> float:
    """The result is a dp-feasible permutation whose true Lmax is the reported value."""
    assert sorted(result.sequence) == sorted(o.id for o in ops)
    completions = _completions(ops, result.sequence, dp, setup)
    assert completions is not None, f"sequence {result.sequence} breaks a delayed precedence"
    assert result.value == pytest.approx(_lmax(ops, completions), abs=EPS)
    return result.value


# --- random instances --------------------------------------------------------------------------


def _instance(
    seed: int, low: int, high: int
) -> tuple[list[SubOp], list[DelayedPrecedence], Setup | None]:
    """Seeded ops (integer r, p, d), a dp set consistent with one hidden order, and setups
    on every other seed (|a_prev - a_next| from a per-op param, state param 0)."""
    rng = np.random.default_rng((seed, 140))
    n = int(rng.integers(low, high + 1))
    ops = [
        SubOp(
            id=f"o{k}",
            release=float(rng.integers(0, 15)),
            run=float(rng.integers(1, 9)),
            due=float(rng.integers(5, 40)),
        )
        for k in range(n)
    ]
    hidden = [str(x) for x in rng.permutation([o.id for o in ops])]
    dp = [
        DelayedPrecedence(hidden[i], hidden[j], float(rng.integers(0, 4)))
        for i in range(n)
        for j in range(i + 1, n)
        if rng.random() < 0.15
    ]
    if seed % 2 == 0:
        return ops, dp, None
    params = {o.id: float(rng.integers(0, 5)) for o in ops}

    def setup(previous: str | None, nxt: str) -> float:
        return abs((0.0 if previous is None else params[previous]) - params[nxt])

    return ops, dp, setup


# --- 1|r_j,dp|Lmax: exact ----------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(200))
def test_lmax_exact_equals_brute_force_on_random_dp_instances(seed: int) -> None:
    ops, dp, setup = _instance(seed, 2, 8)

    result = lmax_exact(ops, dp, setup=setup)

    assert _check_single(ops, dp, result, setup) == pytest.approx(
        _brute_lmax(ops, dp, setup), abs=EPS
    ), f"seed {seed}"


def test_lmax_exact_single_op_value_is_its_lateness() -> None:
    ops = [SubOp(id="a", release=3.0, run=4.0, due=5.0)]

    result = lmax_exact(ops, [])

    assert result == SubResult(sequence=("a",), value=2.0)


def test_lmax_exact_obeys_a_dp_that_reverses_edd() -> None:
    # EDD alone puts b first; the dp forces a first and the delay pushes b to 1 + 5.
    ops = [
        SubOp(id="a", release=0.0, run=1.0, due=20.0),
        SubOp(id="b", release=0.0, run=1.0, due=2.0),
    ]
    dp = [DelayedPrecedence("a", "b", 5.0)]

    result = lmax_exact(ops, dp)

    assert result.sequence == ("a", "b")
    assert result.value == 7.0 - 2.0


def test_sequence_lmax_rejects_a_sequence_that_breaks_a_dp() -> None:
    ops = [
        SubOp(id="a", release=0.0, run=1.0, due=9.0),
        SubOp(id="b", release=0.0, run=1.0, due=9.0),
    ]
    dp = [DelayedPrecedence("a", "b", 0.0)]

    assert sequence_lmax(ops, ("a", "b"), dp) == -7.0
    with pytest.raises(DelayedPrecedenceViolation):
        sequence_lmax(ops, ("b", "a"), dp)
    assert issubclass(DelayedPrecedenceViolation, ValueError)


@pytest.mark.parametrize("seed", range(40))
def test_sequence_lmax_matches_the_independent_evaluator(seed: int) -> None:
    ops, dp, setup = _instance(seed, 2, 8)
    sequence = lmax_exact(ops, dp, setup=setup).sequence
    completions = _completions(ops, sequence, dp, setup)
    assert completions is not None

    assert sequence_lmax(ops, sequence, dp, setup=setup) == pytest.approx(_lmax(ops, completions))


# --- Schrage and adjacent swaps ----------------------------------------------------------------


@pytest.mark.parametrize("seed", range(200))
def test_schrage_plus_swaps_is_never_worse_than_schrage(seed: int) -> None:
    ops, dp, setup = _instance(seed, 2, 30)

    plain = _check_single(ops, dp, lmax_schrage(ops, dp, setup=setup), setup)
    improved = _check_single(ops, dp, lmax_schrage_swaps(ops, dp, setup=setup), setup)

    assert improved <= plain + EPS, f"seed {seed}: swaps {improved} > schrage {plain}"


def test_schrage_is_non_delay_edd_over_released_ops() -> None:
    # t=0: only a is released, Schrage starts it though c (due 3) arrives at 1.
    ops = [
        SubOp(id="a", release=0.0, run=4.0, due=30.0),
        SubOp(id="c", release=1.0, run=1.0, due=3.0),
        SubOp(id="b", release=1.0, run=2.0, due=10.0),
    ]

    result = lmax_schrage(ops, [])

    assert result.sequence == ("a", "c", "b")
    assert result.value == 5.0 - 3.0


def test_no_heuristic_value_beats_the_exact_optimum() -> None:
    """Sanity bound: no heuristic beats the exact optimum (a lying value would)."""
    for seed in range(60):
        ops, dp, setup = _instance(seed, 2, 8)
        exact = lmax_exact(ops, dp, setup=setup).value
        heuristic = _check_single(ops, dp, lmax_schrage_swaps(ops, dp, setup=setup), setup)
        assert heuristic >= exact - EPS, f"seed {seed}"


def test_solve_lmax_is_exact_up_to_the_limit_and_heuristic_beyond() -> None:
    small, small_dp, _ = _instance(3, 9, 9)
    large, large_dp, _ = _instance(5, 14, 14)

    assert solve_lmax(small, small_dp).value == pytest.approx(lmax_exact(small, small_dp).value)
    assert solve_lmax(large, large_dp) == lmax_schrage_swaps(large, large_dp)
    assert solve_lmax(small, small_dp, exact_limit=4) == lmax_schrage_swaps(small, small_dp)


# --- Pinedo Example 7.2.3, iteration 3 (p.195-196) ----------------------------------------------


def _graph_7_2_3(sequences: Mapping[str, Sequence[str]]) -> tuple[DisjunctiveGraph, ProblemIndex]:
    request = load_request("pinedo_7_2_3")
    index = ProblemIndex.build(compile_request(request).problem)
    return DisjunctiveGraph.build(index, only_choice(request), sequences), index


def _book_7_2_3_iteration_3() -> tuple[list[SubOp], list[DelayedPrecedence]]:
    """Machine 2's subproblem after M3 = (3-1, 4-1) and M1 = job 2 then job 1 (2-2, 1-1).

    Pinedo (i, j) = machine i, job j: (2,1) is op 1-2, (2,2) is op 2-1. Derived from the
    kernel graph: r = heads, d = Cmax - tail + p, delay = longest path - p(before).
    """
    graph, index = _graph_7_2_3({"M3": ("3-1", "4-1"), "M1": ("2-2", "1-1")})
    heads, tails = graph.heads(), graph.tails()
    cmax = max(graph.completions().values())
    run = {o: index.op(o).options[0].run for o in ("1-2", "2-1")}
    ops = [SubOp(id=o, release=heads[o], run=run[o], due=cmax - tails[o] + run[o]) for o in run]
    path = graph.longest_path("2-1", "1-2")
    assert path is not None
    return ops, [DelayedPrecedence("2-1", "1-2", path - run["2-1"])]


def test_7_2_3_iteration_3_inputs_match_the_book() -> None:
    ops, dp = _book_7_2_3_iteration_3()

    by_id = {o.id: o for o in ops}
    assert (by_id["1-2"].release, by_id["2-1"].release) == (3.0, 0.0)  # r(2,1), r(2,2)
    assert (by_id["1-2"].due, by_id["2-1"].due) == (8.0, 5.0)  # d(2,1), d(2,2)
    assert dp == [DelayedPrecedence("2-1", "1-2", 2.0)]  # (2,2) -> (2,1), delay 2


def test_7_2_3_iteration_3_orders_op_2_2_before_op_2_1() -> None:
    ops, dp = _book_7_2_3_iteration_3()

    result = lmax_exact(ops, dp)

    assert result.sequence == ("2-1", "1-2")
    assert result.value == -4.0


def test_7_2_3_book_trap_the_cycle_order_does_not_raise_cmax_without_dp() -> None:
    """The book's point: (2,1) first has Lmax 0, so a makespan-indifferent subproblem could
    keep it; it closes the cycle 2-1 -> 2-2 -> 1-1 -> 1-2 -> 2-1. The dp forbids it."""
    ops, dp = _book_7_2_3_iteration_3()

    assert sequence_lmax(ops, ("1-2", "2-1"), []) == 0.0
    with pytest.raises(DelayedPrecedenceViolation):
        sequence_lmax(ops, ("1-2", "2-1"), dp)


# --- multi-due ATC, Pinedo Example 7.3.1 iteration 1 (p.200-202) -------------------------------

BOOK_7_3_1_M1_INDEX = {"1-1": 1.23e-6, "2-2": 3.3e-7, "3-3": 1.46e-12}
"""I(1,1), I(1,2), I(1,3) at t = 4 (p.201); Pinedo (1, j) is job j's op on M1."""


def _subproblem_7_3_1(machine_id: str) -> list[SubOp]:
    """Iteration-1 subproblem of `machine_id`: graph with job arcs only; r = heads (job
    releases), d^k(o) = d_k - tail_k(o) + p(o) for every job sink k that o reaches."""
    request = load_request("pinedo_7_3_1")
    index = ProblemIndex.build(compile_request(request).problem)
    graph = DisjunctiveGraph.build(index, only_choice(request), {})
    heads = graph.heads()
    tails = {job.id: graph.tails(job.id) for job in index.jobs}
    ops: list[SubOp] = []
    for op_id, machine_id_of in only_choice(request).items():
        if machine_id_of != machine_id:
            continue
        run = index.op(op_id).options[0].run
        dues = tuple(
            (job.weight, float(job.due) - tails[job.id][op_id] + run)
            for job in index.jobs
            if job.due is not None and op_id in tails[job.id]
        )
        ops.append(SubOp(id=op_id, release=heads[op_id], run=run, dues=dues))
    return ops


def test_7_3_1_machine_1_inputs_match_the_book() -> None:
    ops = {o.id: o for o in _subproblem_7_3_1("M1")}

    assert {o: (s.release, s.run) for o, s in ops.items()} == {
        "1-1": (5.0, 5.0),
        "2-2": (4.0, 5.0),
        "3-3": (8.0, 7.0),
    }
    assert {o: s.dues for o, s in ops.items()} == {
        "1-1": ((1.0, 10.0),),
        "2-2": ((2.0, 12.0),),
        "3-3": ((2.0, 16.0),),
    }


@pytest.mark.parametrize("op_id", sorted(BOOK_7_3_1_M1_INDEX))
def test_7_3_1_multi_due_atc_index_at_t4_matches_the_book(op_id: str) -> None:
    ops = {o.id: o for o in _subproblem_7_3_1("M1")}

    index = multi_due_atc_index(ops[op_id], 4.0, pbar=5.0, K=0.1)

    assert index == pytest.approx(BOOK_7_3_1_M1_INDEX[op_id], rel=0.01)


def test_multi_due_atc_index_sums_its_local_due_dates() -> None:
    """p.202: I_32(0) = 1.53e-7 + 1.53e-7 for an op with two local due dates."""
    single = SubOp(id="x", release=0.0, run=4.0, dues=((2.0, 10.0),))
    double = SubOp(id="x", release=0.0, run=4.0, dues=((2.0, 10.0), (2.0, 10.0)))

    one = multi_due_atc_index(single, 0.0, pbar=4.0, K=0.1)

    assert multi_due_atc_index(double, 0.0, pbar=4.0, K=0.1) == pytest.approx(2 * one)
    assert one == pytest.approx((2.0 / 4.0) * math.exp(-6.0 / 0.4))


def test_multi_due_atc_index_is_zero_without_a_reachable_sink() -> None:
    assert multi_due_atc_index(SubOp(id="x", release=0.0, run=3.0), 0.0, pbar=3.0, K=2.0) == 0.0


@pytest.mark.parametrize(
    ("machine_id", "sequence", "value"),
    [
        ("M1", ("1-1", "2-2", "3-3"), 18.0),  # (1,1), (1,2), (1,3)
        ("M2", ("3-2", "1-2", "2-3"), 16.0),  # (2,3), (2,1), (2,2)
        ("M3", ("3-1", "2-1", "1-3"), 4.0),  # (3,3), (3,2), (3,1)
    ],
)
def test_7_3_1_iteration_1_atc_sequences_and_values(
    machine_id: str, sequence: tuple[str, ...], value: float
) -> None:
    ops = _subproblem_7_3_1(machine_id)

    result = wt_atc_sequence(ops, [], K=0.1)

    assert result == SubResult(sequence=sequence, value=value)
    completions = _completions(ops, sequence, [])
    assert completions is not None
    assert _weighted_tardiness(ops, completions) == value


def test_wt_atc_sequence_respects_delayed_precedence() -> None:
    # Without the dp, b (urgent, heavy) goes first; the dp makes a go first.
    ops = [
        SubOp(id="a", release=0.0, run=2.0, dues=((1.0, 50.0),)),
        SubOp(id="b", release=0.0, run=2.0, dues=((5.0, 2.0),)),
    ]
    assert wt_atc_sequence(ops, [], K=0.1).sequence == ("b", "a")

    result = wt_atc_sequence(ops, [DelayedPrecedence("a", "b", 1.0)], K=0.1)

    assert result.sequence == ("a", "b")
    assert result.value == 5.0 * (2.0 + 1.0 + 2.0 - 2.0)


# --- parallel-machine center (p.212) ------------------------------------------------------------


def _center_completions(
    ops: Sequence[CenterOp],
    sequences: Mapping[str, Sequence[str]],
    dp: Sequence[DelayedPrecedence],
    setup: CenterSetup | None,
) -> dict[str, float]:
    """Semi-active completions across machines; fails on an unplaceable (cyclic) order."""
    by_id = {o.id: o for o in ops}
    machine_of = {o: m for m, seq in sequences.items() for o in seq}
    previous = {
        o: (seq[k - 1] if k else None) for seq in sequences.values() for k, o in enumerate(seq)
    }
    done: dict[str, float] = {}
    while len(done) < len(ops):
        ready = [
            o
            for o in by_id
            if o not in done
            and (previous[o] is None or previous[o] in done)
            and all(c.before in done for c in dp if c.after == o)
        ]
        assert ready, f"sequences and dp close a cycle: {dict(sequences)}"
        for o in ready:
            m, prev = machine_of[o], previous[o]
            gates = [done[c.before] + c.delay for c in dp if c.after == o]
            free = -math.inf if prev is None else done[prev]
            start = max([by_id[o].release, free, *gates])
            done[o] = start + (setup(m, prev, o) if setup else 0.0) + by_id[o].runs[m]
    return done


def _check_center(
    ops: Sequence[CenterOp],
    machines: Sequence[str],
    dp: Sequence[DelayedPrecedence],
    result: CenterResult,
    setup: CenterSetup | None,
) -> dict[str, float]:
    placed = [o for seq in result.sequences.values() for o in seq]
    assert sorted(placed) == sorted(o.id for o in ops), "every op placed exactly once"
    assert set(result.sequences) <= set(machines)
    runs = {o.id: o.runs for o in ops}
    for m, seq in result.sequences.items():
        for o in seq:
            assert m in runs[o], f"{o} placed on ineligible {m}"
    completions = _center_completions(ops, result.sequences, dp, setup)
    lmax = max(completions[o.id] - o.due for o in ops)
    assert result.value == pytest.approx(lmax, abs=EPS)
    return completions


def _center_instance(
    seed: int,
) -> tuple[list[CenterOp], list[str], list[DelayedPrecedence], CenterSetup]:
    rng = np.random.default_rng((seed, 212))
    machines = ["A", "B"]
    n = int(rng.integers(3, 9))
    ops: list[CenterOp] = []
    for k in range(n):
        eligible = machines if rng.random() < 0.6 else [machines[int(rng.integers(2))]]
        runs = {m: float(rng.integers(1, 9)) for m in eligible}
        ops.append(
            CenterOp(
                id=f"o{k}",
                release=float(rng.integers(0, 10)),
                runs=runs,
                due=float(rng.integers(5, 30)),
            )
        )
    hidden = [str(x) for x in rng.permutation([o.id for o in ops])]
    dp = [
        DelayedPrecedence(hidden[i], hidden[j], float(rng.integers(0, 3)))
        for i in range(n)
        for j in range(i + 1, n)
        if rng.random() < 0.2
    ]
    params = {o.id: float(rng.integers(0, 5)) for o in ops}
    state = {"A": 0.0, "B": 2.0}

    def setup(machine_id: str, previous: str | None, nxt: str) -> float:
        before = state[machine_id] if previous is None else params[previous]
        return abs(before - params[nxt])

    return ops, machines, dp, setup


def _on_any_machine(setup: Setup) -> CenterSetup:
    def center_setup(machine_id: str, previous: str | None, nxt: str) -> float:
        return setup(previous, nxt)

    return center_setup


@pytest.mark.parametrize("seed", range(100))
def test_center_parallel_respects_eligibility_setups_and_dp(seed: int) -> None:
    ops, machines, dp, setup = _center_instance(seed)

    result = solve_center_parallel(ops, machines, dp, setup=setup)

    _check_center(ops, machines, dp, result, setup)


def test_center_parallel_with_one_machine_reduces_to_the_single_machine_solver() -> None:
    for seed in range(30):
        ops, dp, setup = _instance(seed, 2, 8)
        center_ops = [
            CenterOp(id=o.id, release=o.release, runs={"A": o.run}, due=o.due) for o in ops
        ]
        center_setup = None if setup is None else _on_any_machine(setup)

        result = solve_center_parallel(center_ops, ["A"], dp, setup=center_setup)

        single = solve_lmax(ops, dp, setup=setup)
        assert result == CenterResult(sequences={"A": single.sequence}, value=single.value)


def test_center_parallel_uses_the_second_machine_when_it_helps() -> None:
    ops = [CenterOp(id=f"o{k}", release=0.0, runs={"A": 5.0, "B": 5.0}, due=5.0) for k in range(2)]

    result = solve_center_parallel(ops, ["A", "B"], [])

    assert result.value == 0.0
    assert sorted(len(seq) for seq in result.sequences.values()) == [1, 1]


def test_center_parallel_weighted_tardiness_value_matches_its_sequences() -> None:
    ops = [
        CenterOp(id="a", release=0.0, runs={"A": 4.0}, dues=((2.0, 3.0),)),
        CenterOp(id="b", release=0.0, runs={"A": 2.0, "B": 6.0}, dues=((1.0, 4.0),)),
        CenterOp(id="c", release=1.0, runs={"B": 3.0}, dues=((3.0, 4.0),)),
    ]

    result = solve_center_parallel(ops, ["A", "B"], [], objective="weighted_tardiness", K=0.5)

    completions = _center_completions(ops, result.sequences, [], None)
    expected = sum(w * max(completions[o.id] - d, 0.0) for o in ops for w, d in o.dues)
    assert result.value == pytest.approx(expected)
    assert "c" in result.sequences["B"] and "a" in result.sequences["A"]


# --- center result decoded and verified on a real problem ---------------------------------------


def _center_request(seed: int) -> tuple[ProblemIndex, SolveRequest, CompiledProblem]:
    """One 2-machine center WC (A, B), 4-6 jobs of two ops both in WC (link min_lag 0-2),
    some ops eligible on one machine only, abs_diff setups on param `w`."""
    rng = np.random.default_rng((seed, 2120))
    jobs = []
    for j in range(int(rng.integers(4, 7))):
        ops = []
        for k in range(2):
            eligible = ["A", "B"] if rng.random() < 0.6 else [["A", "B"][int(rng.integers(2))]]
            after = (
                []
                if k == 0
                else [{"operation_id": f"J{j}-0", "min_lag": float(rng.integers(0, 3))}]
            )
            ops.append(
                op(
                    f"J{j}-{k}",
                    {m: float(rng.integers(1, 8)) for m in eligible},
                    after=after,
                    setup_params={"w": float(rng.integers(0, 5))},
                )
            )
        jobs.append(
            {
                "id": f"J{j}",
                "release": float(rng.integers(0, 6)),
                "due": float(rng.integers(8, 30)),
                "operations": ops,
            }
        )
    problem = {
        "id": f"center-{seed}",
        "jobs": jobs,
        "machines": [machine("A"), machine("B")],
        "work_centers": [{"id": "WC", "setup_function_id": "S"}],
        "setup_functions": [
            {"id": "S", "terms": [{"kind": "abs_diff", "param": "w", "seconds_per_unit": 1.0}]}
        ],
    }
    request = request_from(problem)
    compiled = compile_request(request)
    return ProblemIndex.build(compiled.problem), request, compiled


@pytest.mark.parametrize("seed", range(20))
def test_center_parallel_result_decodes_to_the_same_times_and_verifies(seed: int) -> None:
    index, request, compiled = _center_request(seed)
    ops: list[CenterOp] = []
    dp: list[DelayedPrecedence] = []
    for job in index.jobs:
        first, second = (index.ops[p] for p in job.ops)
        lag = second.predecessors[0].min_lag
        assert job.due is not None
        shortest_second = min(o.run for o in second.options)
        ops.append(
            CenterOp(
                id=first.id,
                release=first.release,
                runs={o.machine_id: o.run for o in first.options},
                due=job.due - shortest_second - lag,
            )
        )
        ops.append(
            CenterOp(
                id=second.id,
                release=second.release,
                runs={o.machine_id: o.run for o in second.options},
                due=job.due,
            )
        )
        dp.append(DelayedPrecedence(first.id, second.id, lag))

    def setup(machine_id: str, previous: str | None, nxt: str) -> float:
        return index.setups.seconds(machine_id, previous, nxt)

    result = solve_center_parallel(ops, ["A", "B"], dp, setup=setup)

    completions = _check_center(ops, ["A", "B"], dp, result, setup)
    choice = {o: m for m, seq in result.sequences.items() for o in seq}
    decoded = decode(index, choice, result.sequences)
    assert isinstance(decoded, Decoded), decoded
    for op_id, end in completions.items():
        assert decoded.end(op_id) == pytest.approx(end), f"seed {seed}: end of {op_id}"
    verification = verify(compiled, request.overrides, decoded.schedule("sb-center"))
    assert verification.valid, verification.issues


# --- setup-aware weighted-tardiness subproblems (T-143; SDD setup factor, p.212) -----------------
#
# `setup_scale` (keyword, default inf = factor 1) is K2 * sbar_c of the SDD index: each op's
# multi-due ATC index is multiplied by exp(-s / setup_scale), s the setup from the op that
# precedes it on that machine (the machine state when first). On a center the decision is
# the (op, machine) pair, among released ops and machines free at t, with the largest
# index (ties: earlier finish, then op id, then machine id).


def test_wt_atc_sequence_setup_factor_prefers_the_op_with_the_smaller_setup() -> None:
    ops = [
        SubOp(id="a", release=0.0, run=2.0, dues=((1.0, 2.0),)),
        SubOp(id="b", release=0.0, run=2.0, dues=((1.0, 2.0),)),
    ]
    from_state = {"a": 4.0, "b": 0.0}

    def setup(previous: str | None, nxt: str) -> float:
        return from_state[nxt] if previous is None else 0.0

    assert wt_atc_sequence(ops, [], K=1.0, setup=setup).sequence == ("a", "b")  # tie: op id

    result = wt_atc_sequence(ops, [], K=1.0, setup=setup, setup_scale=2.0)

    assert result.sequence == ("b", "a")
    assert result.value == pytest.approx(1.0 * (2.0 - 2.0) + 1.0 * (4.0 - 2.0))


def test_center_weighted_tardiness_picks_the_op_machine_pair_with_the_setup_factor() -> None:
    """y is slightly more urgent than x, but y needs a 10 s setup on either machine and x
    none on A: with the factor x starts first on A; without it y starts at 0."""
    ops = [
        CenterOp(id="x", release=0.0, runs={"A": 3.0, "B": 3.0}, dues=((1.0, 6.0),)),
        CenterOp(id="y", release=0.0, runs={"A": 3.0, "B": 3.0}, dues=((1.0, 5.0),)),
    ]

    def setup(machine_id: str, previous: str | None, nxt: str) -> float:
        if nxt == "y":
            return 10.0
        return 0.0 if machine_id == "A" else 10.0

    plain = solve_center_parallel(ops, ["A", "B"], [], setup=setup, objective="weighted_tardiness")
    aware = solve_center_parallel(
        ops, ["A", "B"], [], setup=setup, objective="weighted_tardiness", setup_scale=2.0
    )

    assert plain.sequences["A"][0] == "y"  # most urgent first, fastest machine (tie: A)
    assert aware.sequences["A"] == ("x",)
    assert aware.sequences["B"] == ("y",)
    completions = _center_completions(ops, aware.sequences, [], setup)
    assert completions == {"x": 3.0, "y": 13.0}
    assert aware.value == pytest.approx(1.0 * (13.0 - 5.0))


def test_center_parallel_refuses_delayed_precedences_that_close_a_cycle() -> None:
    ops = [CenterOp(id=o, release=0.0, runs={"A": 1.0, "B": 1.0}) for o in ("a", "b")]
    cycle = [DelayedPrecedence("a", "b", 0.0), DelayedPrecedence("b", "a", 0.0)]

    with pytest.raises(DelayedPrecedenceViolation):
        solve_center_parallel(ops, ["A", "B"], cycle)
