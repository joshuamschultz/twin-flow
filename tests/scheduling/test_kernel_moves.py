"""Moves, neighborhoods and incremental evaluation (T-145, T-146; COMP-017, REQ-108, REQ-111).

SDD pseudo-code "Tabu search (Alg 14.3.3) and its neighborhood (p.378-380)", D-076, Data
Model "Disjunctive graph". Contract defined here for `twinflow.scheduling.kernel.moves`:

- `Move` (frozen, hashable), built by `Move.swap(u, v)` (u directly before v on one
  machine), `Move.lookback(u, v, a, b)` (swap(u, v) plus swap(a, b), where b = pj(v), v's
  job predecessor, and a = b's machine predecessor, Pinedo p.379-380, Fig 14.1) and
  `Move.reassign(op, old_machine, machine, position)` (op leaves `old_machine` and enters
  `machine`'s sequence at index `position`). `kind` is "swap", "lookback" or "reassign";
  `key` is a tuple, a total order over distinct moves of every kind; `attribute` is the
  tabu attribute: frozenset({u, v}) for swap and lookback (the primary pair), (op,
  old_machine) for reassign.
- `moves(graph, spec, index) -> list[Move]` sorted by key. Critical paths: a makespan
  objective uses one longest path (`graph.critical_path()`); a due-date objective uses
  `graph.critical_path(job)` for the up to 5 tardy jobs (C_k > d_k) with the largest
  w_k T_k, all tardy jobs when fewer. Swaps: consecutive path entries on the same machine
  of different jobs; an in-progress op is never swapped (it must stay first). Look-back
  per swap when b exists, a exists and a, b are of different jobs. Reassign: ops on those
  paths with another option machine in their work center, at the least-added-setup
  insertion position (added = s(prev, op) + s(op, next) - s(prev, next); prev None = the
  machine state; ties: lowest position).
- `apply(move, choice, sequences) -> (choice', sequences')`: pure; ValueError when the
  move does not fit the sequences.
- `evaluate(graph, move) -> Decoded`: the graph's schedule after the move (starts = heads,
  setup_ends = head + setup, ends = head + dur, batch ids None); the graph is left as it
  was. Raises `GraphCycle` (graph unchanged) when the move closes a cycle.
- `confirm(index, estimate, choice, sequences, scorer, best) -> Confirmed | ScheduleIssue`:
  decodes; `Confirmed(decoded, values, improved)` carries the *decoded* `fast_values` and
  `improved = scorer.better(values, best)`; the graph estimate never decides (D-076).
  Keyword-only `previous_plan` / `churn_tolerance` (default None / 0.0) score churn.
- `SetupTable.cache_info() -> (hits, misses, size)` named fields: misses = computations.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pytest
from generators import random_request
from kernel_cases import (
    OPTIMAL_7_1_1,
    STRIPPABLE,
    compile_request,
    load_request,
    machine,
    machine_choice,
    machine_sequences,
    only_choice,
    op,
    request_from,
    without_features,
)

from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.graph import DisjunctiveGraph, GraphCycle, graph_exact
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.moves import Confirmed, Move, apply, confirm, evaluate, moves
from twinflow.scheduling.kernel.score import Scorer, fast_values
from twinflow.scheduling.model import (
    Lexicographic,
    LexicographicLevel,
    ObjectiveKind,
    ObjectiveSpec,
    PreviousAssignment,
    PreviousPlan,
    ScheduleIssue,
    SolveRequest,
)
from twinflow.scheduling.seam import Feature
from twinflow.scheduling.solver import problem_features, solve

EPS = 1e-6
MOVES_PER_PROBLEM = 20
MIN_PROBLEMS = 100
MIN_MOVES_CHECKED = 1500
WT = Lexicographic(levels=(LexicographicLevel(kind=ObjectiveKind.WEIGHTED_TARDINESS),))
MAKESPAN = Lexicographic(levels=(LexicographicLevel(kind=ObjectiveKind.MAKESPAN),))


# --- helpers ---------------------------------------------------------------------------------


def _setup(
    request: SolveRequest, sequences: Mapping[str, Sequence[str]], choice: Mapping[str, str]
) -> tuple[ProblemIndex, DisjunctiveGraph]:
    index = ProblemIndex.build(compile_request(request).problem)
    return index, DisjunctiveGraph.build(index, choice, sequences)


def _single_machine(order: Sequence[str]) -> tuple[ProblemIndex, DisjunctiveGraph, ObjectiveSpec]:
    """Pinedo 14.3.4 with job order `order` (job ids) on M1."""
    request = load_request("pinedo_14_3_4")
    sequences = {"M1": [f"{j}-1" for j in order]}
    index, graph = _setup(request, sequences, only_choice(request))
    return index, graph, request.profile.objective


def _swaps(found: Sequence[Move]) -> list[Move]:
    return [m for m in found if m.kind == "swap"]


def _sorted(expected: Sequence[Move]) -> list[Move]:
    return sorted(expected, key=lambda m: m.key)


def _tuples(sequences: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    """Sequences as tuples, empty machines dropped (apply may keep or drop them)."""
    return {m: tuple(seq) for m, seq in sequences.items() if seq}


def _shop(
    jobs: Sequence[tuple[str, Sequence[dict[str, Any]]]],
    machines: Sequence[dict[str, Any]],
    *,
    dues: Mapping[str, float] | None = None,
    weights: Mapping[str, float] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> SolveRequest:
    centers = sorted({m["work_center_id"] for m in machines})
    problem: dict[str, Any] = {
        "id": "moves-case",
        "jobs": [
            {"id": job_id, "operations": list(ops)}
            | ({"due": dues[job_id]} if dues and job_id in dues else {})
            | ({"weight_override": weights[job_id]} if weights and job_id in weights else {})
            for job_id, ops in jobs
        ],
        "machines": list(machines),
        "work_centers": [{"id": c} for c in centers],
    }
    return request_from(problem | dict(extra or {}))


# --- T-145: Pinedo 14.3.4, single machine, weighted tardiness ---------------------------------


def test_14_3_4_sequence_2_1_4_3_swaps_are_every_adjacent_pair() -> None:
    """C = 10, 20, 24, 37 for 2,1,4,3: all four jobs are tardy, so the path to V_3 spans
    the whole machine and the three adjacent pairs are the neighborhood (Pinedo p.383)."""
    index, graph, spec = _single_machine(["2", "1", "4", "3"])

    found = moves(graph, spec, index)

    assert found == _sorted(
        [Move.swap("2-1", "1-1"), Move.swap("1-1", "4-1"), Move.swap("4-1", "3-1")]
    )


def test_14_3_4_s3_4_2_1_3_swaps_include_the_on_time_first_job() -> None:
    """Job 4 is on time (C = 4 <= 12) but lies on the path to tardy job 3's sink."""
    index, graph, spec = _single_machine(["4", "2", "1", "3"])

    found = moves(graph, spec, index)

    assert found == _sorted(
        [Move.swap("4-1", "2-1"), Move.swap("2-1", "1-1"), Move.swap("1-1", "3-1")]
    )


def test_single_machine_no_lookback_or_reassign_without_job_predecessors_or_machines() -> None:
    index, graph, spec = _single_machine(["2", "1", "4", "3"])

    kinds = {m.kind for m in moves(graph, spec, index)}

    assert kinds == {"swap"}


# --- T-145: due-date critical paths (the up-to-5 tardy-job rule) ------------------------------


def _line(weights: Sequence[float], dues: Sequence[float]) -> tuple[ProblemIndex, DisjunctiveGraph]:
    """Jobs J0..Jn-1 of one 1 s op each on M, sequenced in id order: C_k = k + 1."""
    jobs = [(f"J{k}", [op(f"J{k}-0", {"M": 1.0})]) for k in range(len(weights))]
    request = _shop(
        jobs,
        [machine("M")],
        dues={f"J{k}": d for k, d in enumerate(dues)},
        weights={f"J{k}": w for k, w in enumerate(weights)},
    )
    sequences = {"M": [f"J{k}-0" for k in range(len(weights))]}
    return _setup(request, sequences, {f"J{k}-0": "M" for k in range(len(weights))})


def _pairs(found: Sequence[Move]) -> set[tuple[str, str]]:
    return {(m.ops[0], m.ops[1]) for m in _swaps(found)}


def test_due_date_paths_cover_only_the_five_largest_weighted_tardiness_jobs() -> None:
    """Seven tardy jobs; J5 and J6 have the smallest w T (6, 7 vs 100..500): their
    paths are not used, so the pairs after J4 are not in the neighborhood."""
    index, graph = _line([100.0] * 5 + [1.0, 1.0], [0.0] * 7)

    found = moves(graph, WT, index)

    assert _pairs(found) == {("J0-0", "J1-0"), ("J1-0", "J2-0"), ("J2-0", "J3-0"), ("J3-0", "J4-0")}


def test_due_date_paths_use_every_tardy_job_when_fewer_than_five() -> None:
    index, graph = _line([1.0, 1.0, 1.0, 1.0], [0.0] * 4)

    found = moves(graph, WT, index)

    assert _pairs(found) == {("J0-0", "J1-0"), ("J1-0", "J2-0"), ("J2-0", "J3-0")}


def test_due_date_paths_skip_on_time_jobs_sinks() -> None:
    """J2 (last) is on time: the pair (J1, J2) is on no tardy job's path."""
    index, graph = _line([1.0, 1.0, 1.0], [0.0, 0.0, 100.0])

    found = moves(graph, WT, index)

    assert _pairs(found) == {("J0-0", "J1-0")}


def test_due_date_objective_without_tardy_jobs_has_no_moves() -> None:
    index, graph = _line([1.0, 1.0, 1.0], [100.0, 100.0, 100.0])

    assert moves(graph, WT, index) == []


def test_makespan_objective_uses_the_longest_path_even_when_jobs_are_on_time() -> None:
    index, graph = _line([1.0, 1.0, 1.0], [100.0, 100.0, 100.0])

    found = moves(graph, MAKESPAN, index)

    assert _pairs(found) == {("J0-0", "J1-0"), ("J1-0", "J2-0")}


# --- T-145: Pinedo 7.1.1, optimal sequences, makespan -----------------------------------------


def test_7_1_1_swaps_are_the_adjacent_same_machine_pairs_of_the_critical_path() -> None:
    request = load_request("pinedo_7_1_1")
    choice = only_choice(request)
    index, graph = _setup(request, OPTIMAL_7_1_1, choice)
    path = graph.critical_path()
    expected = [
        Move.swap(u, v)
        for u, v in zip(path, path[1:], strict=False)
        if choice[u] == choice[v] and index.job_of(u) != index.job_of(v)
    ]
    assert expected  # 1-1, 1-2, 3-2, 3-3: (1-2, 3-2) on M2

    found = moves(graph, request.profile.objective, index)

    assert _swaps(found) == _sorted(expected)


def test_7_1_1_full_neighborhood_is_the_swap_and_its_lookback() -> None:
    """Path 1-1, 1-2, 3-2, 3-3. Swap (1-2, 3-2) on M2; pj(3-2) = 3-1, whose machine
    predecessor on M1 is 2-2 (job 2 vs job 3): look-back adds swap (2-2, 3-1)."""
    request = load_request("pinedo_7_1_1")
    index, graph = _setup(request, OPTIMAL_7_1_1, only_choice(request))
    assert graph.critical_path() == ["1-1", "1-2", "3-2", "3-3"]

    found = moves(graph, request.profile.objective, index)

    assert found == _sorted([Move.swap("1-2", "3-2"), Move.lookback("1-2", "3-2", "2-2", "3-1")])


# --- T-145: look-back ---------------------------------------------------------------------------


def _lookback_case(b1_job: str) -> tuple[ProblemIndex, DisjunctiveGraph]:
    """M2: a1 (10 s) then b3; M1: x then b2; b3 follows b2. x belongs to job `b1_job`.

    Path a1 -> b3 (b3 waits for M2 until 10, b2 ends at 2): swap (a1, b3); pj(b3) = b2,
    machine predecessor x.
    """
    x_op = op("x", {"M1": 1.0})
    b2 = op("b2", {"M1": 1.0}, after=["x"] if b1_job == "B" else [])
    b3 = op("b3", {"M2": 5.0}, after=["b2"])
    jobs: list[tuple[str, list[dict[str, Any]]]] = [("A", [op("a1", {"M2": 10.0})])]
    if b1_job == "B":
        jobs.append(("B", [x_op, b2, b3]))
    else:
        jobs += [("B", [b2, b3]), ("C", [x_op])]
    request = _shop(jobs, [machine("M1"), machine("M2")])
    choice = {"a1": "M2", "x": "M1", "b2": "M1", "b3": "M2"}
    return _setup(request, {"M1": ["x", "b2"], "M2": ["a1", "b3"]}, choice)


def test_lookback_added_when_job_predecessor_and_its_machine_predecessor_differ_in_job() -> None:
    index, graph = _lookback_case("C")

    found = moves(graph, MAKESPAN, index)

    assert found == _sorted([Move.swap("a1", "b3"), Move.lookback("a1", "b3", "x", "b2")])


def test_no_lookback_when_the_predecessor_pair_is_one_job() -> None:
    index, graph = _lookback_case("B")

    found = moves(graph, MAKESPAN, index)

    assert found == [Move.swap("a1", "b3")]


def test_no_lookback_when_the_job_predecessor_is_first_on_its_machine() -> None:
    request = _shop(
        [
            ("A", [op("a1", {"M2": 10.0})]),
            ("B", [op("b2", {"M1": 1.0}), op("b3", {"M2": 5.0}, after=["b2"])]),
        ],
        [machine("M1"), machine("M2")],
    )
    index, graph = _setup(
        request, {"M1": ["b2"], "M2": ["a1", "b3"]}, {"a1": "M2", "b2": "M1", "b3": "M2"}
    )

    assert moves(graph, MAKESPAN, index) == [Move.swap("a1", "b3")]


def test_same_job_adjacent_pair_on_the_path_is_not_a_move() -> None:
    request = _shop(
        [("A", [op("a1", {"M": 2.0}), op("a2", {"M": 3.0}, after=["a1"])])], [machine("M")]
    )
    index, graph = _setup(request, {"M": ["a1", "a2"]}, {"a1": "M", "a2": "M"})

    assert moves(graph, MAKESPAN, index) == []


def test_in_progress_op_is_never_swapped_out_of_first_place() -> None:
    status = {"state": "in_progress", "machine_id": "M", "remaining_seconds": 10.0}
    request = _shop(
        [("A", [op("a1", {"M": 10.0}, status=status)]), ("B", [op("b1", {"M": 5.0})])],
        [machine("M")],
    )
    index, graph = _setup(request, {"M": ["a1", "b1"]}, {"a1": "M", "b1": "M"})
    assert graph.critical_path() == ["a1", "b1"]

    assert moves(graph, MAKESPAN, index) == []


# --- T-145: reassignment within a work center --------------------------------------------------


def _flex_case() -> tuple[ProblemIndex, DisjunctiveGraph, dict[str, str], dict[str, list[str]]]:
    """Center WC: MA (no state) and MB (state w=0); abs_diff setup on w, 1 s per unit.

    a (w0), b (w4), c (w1) run 5 s in sequence on MA (the makespan path); a and c may also
    run on MB, b may not; x (w3) runs only on MB, off the path. Insertion into MB = [x]:
    a at 0 adds 0 + 3 - 3 = 0, at 1 adds 3; c at 0 adds 1 + 2 - 3 = 0, at 1 adds 2.
    """
    setup = {"setup_function_id": "S"}
    request = request_from(
        {
            "id": "flex",
            "jobs": [
                {
                    "id": "A",
                    "operations": [op("a", {"MA": 5.0, "MB": 5.0}, setup_params={"w": 0.0})],
                },
                {"id": "B", "operations": [op("b", {"MA": 5.0}, setup_params={"w": 4.0})]},
                {
                    "id": "C",
                    "operations": [op("c", {"MA": 5.0, "MB": 5.0}, setup_params={"w": 1.0})],
                },
                {"id": "X", "operations": [op("x", {"MB": 5.0}, setup_params={"w": 3.0})]},
            ],
            "machines": [
                machine("MA"),
                machine("MB", state={"last_setup_params": {"w": 0.0}}),
            ],
            "work_centers": [{"id": "WC"} | setup],
            "setup_functions": [
                {"id": "S", "terms": [{"kind": "abs_diff", "param": "w", "seconds_per_unit": 1.0}]}
            ],
        }
    )
    choice = {"a": "MA", "b": "MA", "c": "MA", "x": "MB"}
    sequences = {"MA": ["a", "b", "c"], "MB": ["x"]}
    index, graph = _setup(request, sequences, choice)
    return index, graph, choice, sequences


def test_reassign_moves_only_for_path_ops_with_another_machine_in_their_center() -> None:
    index, graph, _, _ = _flex_case()
    assert graph.critical_path() == ["a", "b", "c"]

    found = moves(graph, MAKESPAN, index)

    assert [m for m in found if m.kind == "reassign"] == _sorted(
        [Move.reassign("a", "MA", "MB", 0), Move.reassign("c", "MA", "MB", 0)]
    )
    assert _swaps(found) == _sorted([Move.swap("a", "b"), Move.swap("b", "c")])


def _added_setup(
    index: ProblemIndex, machine_id: str, seq: Sequence[str], k: int, op_id: str
) -> float:
    prev = seq[k - 1] if k > 0 else None
    nxt = seq[k] if k < len(seq) else None
    added = index.setups.seconds(machine_id, prev, op_id)
    if nxt is not None:
        added += index.setups.seconds(machine_id, op_id, nxt)
        added -= index.setups.seconds(machine_id, prev, nxt)
    return added


def _check_reassign(
    index: ProblemIndex,
    move: Move,
    choice: Mapping[str, str],
    sequences: Mapping[str, Sequence[str]],
) -> None:
    (op_id,) = move.ops
    data = index.op(op_id)
    assert move.old_machine == choice[op_id] != move.machine
    assert move.machine in {o.machine_id for o in data.options}, "not an eligible option"
    assert move.machine in index.machines_of(data.work_center_id), "outside the op's center"
    target = list(sequences.get(move.machine, ()))
    # A running op stays first on its machine (the decoder refuses anything before it).
    first = 1 if target and index.op(target[0]).in_progress is not None else 0
    added = [_added_setup(index, move.machine, target, k, op_id) for k in range(len(target) + 1)]
    assert move.position == first + int(np.argmin(added[first:])), (op_id, added)


def test_reassign_position_is_the_least_added_setup_on_generated_problems() -> None:
    checked = 0
    for seed in range(60):
        compared = _base_solution(seed)
        if compared is None:
            continue
        index, graph, choice, sequences, spec = compared
        for move in moves(graph, spec, index):
            if move.kind == "reassign":
                _check_reassign(index, move, choice, sequences)
                checked += 1
    assert checked >= 20


def test_no_reassign_when_every_op_has_one_option() -> None:
    request = load_request("pinedo_7_1_1")
    index, graph = _setup(request, OPTIMAL_7_1_1, only_choice(request))

    assert all(m.kind != "reassign" for m in moves(graph, request.profile.objective, index))


# --- T-145: Move.key, attribute, frozen ---------------------------------------------------------


def test_swap_attribute_is_the_unordered_op_pair() -> None:
    assert Move.swap("a", "b").attribute == frozenset({"a", "b"})
    assert Move.swap("a", "b").attribute == Move.swap("b", "a").attribute
    assert Move.lookback("a", "b", "x", "y").attribute == frozenset({"a", "b"})


def test_reassign_attribute_is_op_and_old_machine() -> None:
    assert Move.reassign("a", "MA", "MB", 0).attribute == ("a", "MA")


def test_move_is_frozen_and_hashable() -> None:
    move = Move.swap("a", "b")

    with pytest.raises(dataclasses.FrozenInstanceError):
        move.kind = "reassign"  # type: ignore[misc]  # the assignment under test must fail
    assert len({move, Move.swap("a", "b")}) == 1


def test_move_key_is_a_total_order_over_mixed_kinds() -> None:
    pool = [
        Move.swap("a", "b"),
        Move.swap("b", "a"),
        Move.swap("a", "c"),
        Move.lookback("a", "b", "x", "y"),
        Move.lookback("a", "b", "x", "z"),
        Move.reassign("a", "MA", "MB", 0),
        Move.reassign("a", "MA", "MB", 1),
        Move.reassign("a", "MA", "MC", 0),
        Move.reassign("b", "MA", "MB", 0),
    ]
    keys = [m.key for m in pool]

    assert len(set(keys)) == len(pool), "distinct moves must have distinct keys"
    assert sorted(pool, key=lambda m: m.key) == sorted(reversed(pool), key=lambda m: m.key)
    assert Move.swap("a", "b").key == Move.swap("a", "b").key


def test_moves_on_generated_problems_are_sorted_by_key_and_unique() -> None:
    for seed in range(40):
        compared = _base_solution(seed)
        if compared is None:
            continue
        index, graph, _, _, spec = compared
        found = moves(graph, spec, index)
        assert found == sorted(found, key=lambda m: m.key), seed
        assert len({m.key for m in found}) == len(found), seed


# --- T-145: apply ---------------------------------------------------------------------------------


def test_apply_swap_is_pure_and_swaps_the_pair() -> None:
    choice = {"1-1": "M1", "2-1": "M1"}
    sequences = {"M1": ["2-1", "1-1"]}
    before = copy.deepcopy((choice, sequences))

    new_choice, new_sequences = apply(Move.swap("2-1", "1-1"), choice, sequences)

    assert (choice, sequences) == before
    assert dict(new_choice) == choice
    assert _tuples(new_sequences) == {"M1": ("1-1", "2-1")}


def test_apply_lookback_swaps_both_pairs() -> None:
    choice = only_choice(load_request("pinedo_7_1_1"))
    sequences = {m: list(s) for m, s in OPTIMAL_7_1_1.items()}

    _, new_sequences = apply(Move.lookback("1-2", "3-2", "2-2", "3-1"), choice, sequences)

    assert _tuples(new_sequences) == {
        "M1": ("1-1", "3-1", "2-2"),
        "M2": ("2-1", "3-2", "1-2"),
        "M3": ("1-3", "2-4"),
        "M4": ("2-3", "3-3"),
    }


def test_apply_reassign_moves_the_op_and_its_choice() -> None:
    _, _, choice, sequences = _flex_case()
    before = copy.deepcopy((choice, sequences))

    new_choice, new_sequences = apply(Move.reassign("c", "MA", "MB", 0), choice, sequences)

    assert (choice, sequences) == before
    assert new_choice == {**choice, "c": "MB"}
    assert _tuples(new_sequences) == {"MA": ("a", "b"), "MB": ("c", "x")}


def test_apply_refuses_a_swap_that_is_not_adjacent_in_that_order() -> None:
    choice = {"1-1": "M1", "2-1": "M1", "4-1": "M1"}
    sequences = {"M1": ["2-1", "1-1", "4-1"]}

    with pytest.raises(ValueError):
        apply(Move.swap("1-1", "2-1"), choice, sequences)
    with pytest.raises(ValueError):
        apply(Move.swap("2-1", "4-1"), choice, sequences)


def test_apply_refuses_a_reassign_from_a_machine_the_op_is_not_on() -> None:
    _, _, choice, sequences = _flex_case()

    with pytest.raises(ValueError):
        apply(Move.reassign("c", "MB", "MA", 0), choice, sequences)


# --- T-146: incremental graph evaluation equals decode on graph-exact problems ----------------


Base = tuple[ProblemIndex, DisjunctiveGraph, dict[str, str], dict[str, list[str]], ObjectiveSpec]


@functools.cache
def _base_solution(seed: int) -> Base | None:
    """A verified baseline solution of a graph-exact generated problem, as graph input."""
    request = without_features(random_request(seed), STRIPPABLE)
    compiled = compile_request(request)
    assert graph_exact(problem_features(compiled.problem)), f"seed {seed} not graph-exact"
    source = solve(request)
    if not source.verified:
        return None
    index = ProblemIndex.build(compiled.problem)
    choice, sequences = machine_choice(source.schedule), machine_sequences(source.schedule)
    graph = DisjunctiveGraph.build(index, choice, sequences)
    return index, graph, choice, sequences, request.profile.objective


def _values_agree(a: Mapping[ObjectiveKind, float], b: Mapping[ObjectiveKind, float]) -> bool:
    return a.keys() == b.keys() and all(abs(a[k] - b[k]) <= EPS * max(1.0, abs(b[k])) for k in a)


def _check_move(index: ProblemIndex, graph: DisjunctiveGraph, base: Base, move: Move) -> bool:
    """True when compared; asserts graph == decode for the move and the graph is restored."""
    _, _, choice, sequences, _ = base
    heads, completions = graph.heads(), graph.completions()
    new_choice, new_sequences = apply(move, choice, sequences)
    decoded = decode(index, new_choice, new_sequences)
    try:
        estimate = evaluate(graph, move)
    except GraphCycle:
        assert isinstance(decoded, ScheduleIssue) and decoded.code == "sequence_cycle", move
        assert graph.heads() == heads, f"{move}: refused move changed the graph"
        return False
    assert graph.heads() == pytest.approx(heads, abs=1e-9), f"{move}: graph not restored"
    assert graph.completions() == pytest.approx(completions, abs=1e-9)
    if not isinstance(decoded, Decoded):
        assert decoded.code != "sequence_cycle", f"{move}: decoder saw a cycle, graph did not"
        return False  # horizon or pin window: the graph does not reject, the decoder does
    for job in index.jobs:
        graph_c = max(estimate.ends[p] for p in job.ops)
        assert abs(graph_c - decoded.completion(job.id)) <= EPS, f"{move}: C of {job.id}"
    for p, op_id in enumerate(index.op_ids):
        assert abs(estimate.starts[p] - decoded.starts[p]) <= EPS, f"{move}: start of {op_id}"
    assert _values_agree(
        fast_values(index, estimate, None, 0.0), fast_values(index, decoded, None, 0.0)
    )
    return True


def test_incremental_graph_equals_decode_for_20_random_moves_on_200_graph_exact_problems() -> None:
    problems = checked = 0
    for seed in range(200):
        base = _base_solution(seed)
        if base is None:
            continue
        index, graph, _, _, spec = base
        found = moves(graph, spec, index)
        if not found:
            continue
        rng = np.random.default_rng((seed, 146))
        picks = rng.integers(len(found), size=MOVES_PER_PROBLEM)
        problems += 1
        checked += sum(_check_move(index, graph, base, found[int(k)]) for k in picks)
    print(f"\ngraph == decode on {checked} moves over {problems} problems")
    assert problems >= MIN_PROBLEMS
    assert checked >= MIN_MOVES_CHECKED


def test_evaluate_covers_every_move_kind_on_generated_problems() -> None:
    kinds: set[str] = set()
    for seed in range(200):
        base = _base_solution(seed)
        if base is None:
            continue
        index, graph, _, _, spec = base
        kinds |= {m.kind for m in moves(graph, spec, index)}
        if kinds == {"swap", "lookback", "reassign"}:
            break
    assert kinds == {"swap", "lookback", "reassign"}


def test_evaluate_on_14_3_4_gives_the_swapped_weighted_tardiness() -> None:
    """2,1,4,3 -> swap (1, 4) -> 2,4,1,3 with weighted tardiness 436 (Pinedo p.383)."""
    index, graph, _ = _single_machine(["2", "1", "4", "3"])

    estimate = evaluate(graph, Move.swap("1-1", "4-1"))

    assert fast_values(index, estimate, None, 0.0)[ObjectiveKind.WEIGHTED_TARDINESS] == 436.0
    assert graph.heads() == {"2-1": 0.0, "1-1": 10.0, "4-1": 20.0, "3-1": 24.0}


def test_evaluate_lookback_on_7_1_1_equals_decode() -> None:
    request = load_request("pinedo_7_1_1")
    choice = only_choice(request)
    index, graph = _setup(request, OPTIMAL_7_1_1, choice)
    move = Move.lookback("1-2", "3-2", "2-2", "3-1")
    decoded = decode(index, *apply(move, choice, OPTIMAL_7_1_1))
    assert isinstance(decoded, Decoded)

    estimate = evaluate(graph, move)

    assert estimate.starts == pytest.approx(decoded.starts)
    assert max(graph.completions().values()) == 28.0


def test_evaluate_reassign_on_the_flex_case_equals_decode() -> None:
    index, graph, choice, sequences = _flex_case()
    move = Move.reassign("c", "MA", "MB", 0)
    decoded = decode(index, *apply(move, choice, sequences))
    assert isinstance(decoded, Decoded)

    estimate = evaluate(graph, move)

    assert estimate.ends == pytest.approx(decoded.ends)
    assert estimate.setup_ends == pytest.approx(decoded.setup_ends)


def test_evaluate_refuses_a_cycle_and_leaves_the_graph_unchanged() -> None:
    """M1: b2, a1; M2: b1, a2; job arcs a1 -> a2, b1 -> b2. Swapping (b1, a2) on M2 adds
    a2 -> b1, closing a2 -> b1 -> b2 -> a1 -> a2."""
    request = _shop(
        [
            ("A", [op("a1", {"M1": 1.0}), op("a2", {"M2": 1.0}, after=["a1"])]),
            ("B", [op("b1", {"M2": 1.0}), op("b2", {"M1": 1.0}, after=["b1"])]),
        ],
        [machine("M1"), machine("M2")],
    )
    choice = {"a1": "M1", "a2": "M2", "b1": "M2", "b2": "M1"}
    index, graph = _setup(request, {"M1": ["b2", "a1"], "M2": ["b1", "a2"]}, choice)
    heads = graph.heads()

    with pytest.raises(GraphCycle):
        evaluate(graph, Move.swap("b1", "a2"))
    assert graph.heads() == heads


# --- T-146: decode confirmation wins on non-graph-exact problems -------------------------------


def _estimate_only_bases(
    limit: int,
) -> list[
    tuple[ProblemIndex, DisjunctiveGraph, dict[str, str], dict[str, list[str]], ObjectiveSpec]
]:
    """Verified baselines of generated problems with calendars or operators."""
    found = []
    for seed in range(200):
        request = random_request(seed)
        compiled = compile_request(request)
        if not {Feature.CALENDARS, Feature.OPERATORS} & problem_features(compiled.problem):
            continue
        source = solve(request)
        if not source.verified:
            continue
        index = ProblemIndex.build(compiled.problem)
        choice, sequences = machine_choice(source.schedule), machine_sequences(source.schedule)
        try:
            graph = DisjunctiveGraph.build(index, choice, sequences)
        except GraphCycle:
            continue
        found.append((index, graph, choice, sequences, request.profile.objective))
        if len(found) >= limit:
            break
    return found


def test_confirm_keeps_the_decoded_values_when_the_graph_estimate_disagrees() -> None:
    disagreed = confirmed = 0
    for index, graph, choice, sequences, spec in _estimate_only_bases(40):
        scorer = Scorer(spec, None)
        for move in moves(graph, spec, index)[:3]:
            try:
                estimate = fast_values(index, evaluate(graph, move), None, 0.0)
            except GraphCycle:
                continue
            new_choice, new_sequences = apply(move, choice, sequences)
            decoded = decode(index, new_choice, new_sequences)
            result = confirm(index, estimate, new_choice, new_sequences, scorer, estimate)
            if not isinstance(decoded, Decoded):
                assert isinstance(result, ScheduleIssue) and result.code == decoded.code
                continue
            assert isinstance(result, Confirmed)
            truth = fast_values(index, decoded, None, 0.0)
            assert result.values == truth
            assert result.decoded.ends == decoded.ends
            assert result.improved == scorer.better(truth, estimate)
            confirmed += 1
            disagreed += not _values_agree(estimate, truth)
    print(f"\nconfirmed {confirmed} moves, graph estimate disagreed on {disagreed}")
    assert confirmed >= 20
    assert disagreed >= 1, "no calendar/operator case where the graph is only an estimate"


def test_confirm_rejects_an_improvement_the_graph_promised_but_decode_does_not_give() -> None:
    """Injected estimate: weighted tardiness 0. The decode of 2,4,1,3 gives 436 against a
    best of 408: not an improvement, whatever the graph said."""
    request = load_request("pinedo_14_3_4")
    index = ProblemIndex.build(compile_request(request).problem)
    choice = only_choice(request)
    scorer = Scorer(WT, None)
    estimate = {kind: 0.0 for kind in ObjectiveKind}
    best = {kind: 0.0 for kind in ObjectiveKind} | {ObjectiveKind.WEIGHTED_TARDINESS: 408.0}

    result = confirm(index, estimate, choice, {"M1": ["2-1", "4-1", "1-1", "3-1"]}, scorer, best)

    assert isinstance(result, Confirmed)
    assert result.values[ObjectiveKind.WEIGHTED_TARDINESS] == 436.0
    assert result.improved is False


def test_confirm_accepts_an_improvement_the_graph_estimate_missed() -> None:
    """Injected estimate: weighted tardiness 1e9. The decode of 1,4,2,3 gives 408 < 436."""
    request = load_request("pinedo_14_3_4")
    index = ProblemIndex.build(compile_request(request).problem)
    choice = only_choice(request)
    scorer = Scorer(WT, None)
    estimate = {kind: 0.0 for kind in ObjectiveKind} | {ObjectiveKind.WEIGHTED_TARDINESS: 1e9}
    best = {kind: 0.0 for kind in ObjectiveKind} | {ObjectiveKind.WEIGHTED_TARDINESS: 436.0}

    result = confirm(index, estimate, choice, {"M1": ["1-1", "4-1", "2-1", "3-1"]}, scorer, best)

    assert isinstance(result, Confirmed)
    assert result.values[ObjectiveKind.WEIGHTED_TARDINESS] == 408.0
    assert result.improved is True


def test_confirm_returns_the_decoder_issue_for_a_cyclic_sequence() -> None:
    request = load_request("pinedo_7_1_1")
    index = ProblemIndex.build(compile_request(request).problem)
    cyclic = {**OPTIMAL_7_1_1, "M2": ("1-2", "2-1", "3-2"), "M1": ("2-2", "1-1", "3-1")}
    best = {kind: 0.0 for kind in ObjectiveKind}

    result = confirm(index, best, only_choice(request), cyclic, Scorer(MAKESPAN, None), best)

    assert isinstance(result, ScheduleIssue)
    assert result.code == "sequence_cycle"


def test_confirm_scores_churn_against_the_previous_plan_when_given() -> None:
    """Previous plan 2,1,4,3 (starts 0, 10, 20, 24); 2,4,1,3 moves op 4 by 10 s and op 1 by
    4 s: churn 2 at tolerance 0, 1 at tolerance 5, and 0 without a previous plan."""
    request = load_request("pinedo_14_3_4")
    index = ProblemIndex.build(compile_request(request).problem)
    choice = only_choice(request)
    starts = {"2-1": 0.0, "1-1": 10.0, "4-1": 20.0, "3-1": 24.0}
    previous = PreviousPlan(
        plan_id="prev",
        problem_digest="d",
        assignments=tuple(
            PreviousAssignment(operation_id=o, machine_id="M1", start=s) for o, s in starts.items()
        ),
    )
    moved = {"M1": ["2-1", "4-1", "1-1", "3-1"]}
    best = {kind: 0.0 for kind in ObjectiveKind}
    scorer = Scorer(WT, None)
    churn = ObjectiveKind.CHURN

    def churn_of(**kwargs: Any) -> float:
        result = confirm(index, best, choice, moved, scorer, best, **kwargs)
        assert isinstance(result, Confirmed)
        return result.values[churn]

    assert churn_of(previous_plan=previous) == 2.0
    assert churn_of(previous_plan=previous, churn_tolerance=5.0) == 1.0
    assert churn_of() == 0.0


# --- T-146: setup table cache ------------------------------------------------------------------


def test_setup_table_counts_hits_and_computes_each_triple_once() -> None:
    table = ProblemIndex.build(compile_request(_flex_request()).problem).setups
    assert (table.cache_info().hits, table.cache_info().misses) == (0, 0)

    for _ in range(3):
        table.seconds("MB", None, "x")
    table.seconds("MB", "x", "c")

    info = table.cache_info()
    assert (info.hits, info.misses, info.size) == (2, 2, 2)


def test_move_evaluation_reuses_cached_setups() -> None:
    """Evaluating every move twice computes no new setup the first round did not."""
    index, graph, _, _ = _flex_case()
    found = moves(graph, MAKESPAN, index)
    for move in found:
        evaluate(graph, move)
    first = index.setups.cache_info()

    for move in found:
        evaluate(graph, move)
    second = index.setups.cache_info()

    assert first.misses == first.size, "a triple was computed more than once"
    assert second.misses == first.misses
    assert second.hits > first.hits


def test_generated_problem_setups_are_computed_once_across_many_moves() -> None:
    hits = 0
    for seed in range(20):
        base = _base_solution(seed)
        if base is None:
            continue
        index, graph, _, _, spec = base
        for move in moves(graph, spec, index)[:10]:
            try:
                evaluate(graph, move)
            except GraphCycle:
                continue
        info = index.setups.cache_info()
        assert info.misses == info.size, f"seed {seed}"
        hits += info.hits
    assert hits > 0


def _flex_request() -> SolveRequest:
    """The `_flex_case` request (its problem only), for a fresh setup table."""
    return request_from(
        {
            "id": "flex",
            "jobs": [
                {
                    "id": "C",
                    "operations": [op("c", {"MA": 5.0, "MB": 5.0}, setup_params={"w": 1.0})],
                },
                {"id": "X", "operations": [op("x", {"MB": 5.0}, setup_params={"w": 3.0})]},
            ],
            "machines": [machine("MA"), machine("MB", state={"last_setup_params": {"w": 0.0}})],
            "work_centers": [{"id": "WC", "setup_function_id": "S"}],
            "setup_functions": [
                {"id": "S", "terms": [{"kind": "abs_diff", "param": "w", "seconds_per_unit": 1.0}]}
            ],
        }
    )
