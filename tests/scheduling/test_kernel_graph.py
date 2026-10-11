"""Disjunctive graph (T-123, COMP-005, D-076; Pinedo p.180-182, p.191).

`DisjunctiveGraph.build(index, choice, sequences)`: nodes are the scheduled ops on their
chosen machine; job arcs from the links, machine arcs between consecutive ops of each
listed sequence (a machine absent from `sequences` adds no arcs). `heads()` maps op id to
its earliest start, `completions()` job id to C_k, `critical_path(sink=None)` the op ids
of one longest path from the source to the makespan sink (or to job `sink`'s V_k),
`has_path(a, b)` / `longest_path(a, b)` (None when no path) for distinct ops, and
`apply_swap(u, v)` swaps adjacent u, v on their machine with an incremental head update.
`tails(sink=None)` (T-140/T-141, shifting bottleneck q(o)) maps op id to the longest path
from the op's start to the makespan sink V, its own duration included, so head + tail <=
Cmax with equality on a critical path; `tails(job_id)` does the same to job sink V_k and
holds only the ops with a path to V_k (the job's own ops and the ops that reach them).
A cycle raises `GraphCycle` (a ValueError) and is never looped on; `apply_swap` that
would close a cycle raises it and leaves the graph unchanged.
`move_node(op, machine, position)` (T-146, reassign moves) takes op out of its machine's
sequence and inserts it at index `position` of `machine`'s sequence (its chosen option
becomes the one on `machine`), with the same incremental head update and the same cycle
refusal. `placement(op)` is (machine, index in its sequence); `sequences()` maps every
machine with nodes to its op ids in order, and `as_decoded()` gives the graph's schedule
as a `Decoded` (starts = heads, setup_ends = head + setup, ends = head + dur, batch ids
None).

`graph_exact(features)`: True when every feature is graph-exact per the SDD Data Model
(alternatives, setups, detached_setup, lags, overlap, machine_state, in_progress, pins).
Pins count as exact: a pin's earliest start is a head bound, and its latest start only
rejects (the decoder then returns an issue); it never moves a decoded op, so whenever the
decoder succeeds its values equal the graph's (proposed deviation, see report).
"""

from __future__ import annotations

import functools
import signal
from collections.abc import Callable, Iterator, Mapping, Sequence
from types import FrameType
from typing import TypeVar

import numpy as np
import pytest
from generators import random_request
from kernel_cases import (
    OPTIMAL_7_1_1,
    RUNS_7_1_1,
    STRIPPABLE,
    compile_request,
    load_request,
    machine_choice,
    machine_sequences,
    only_choice,
    random_job_shop,
    without_features,
)

from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.graph import DisjunctiveGraph, GraphCycle, graph_exact
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.seam import Feature
from twinflow.scheduling.solver import problem_features, solve

EPS = 1e-6
EXACT = (
    Feature.ALTERNATIVES,
    Feature.SETUPS,
    Feature.DETACHED_SETUP,
    Feature.LAGS,
    Feature.OVERLAP,
    Feature.MACHINE_STATE,
    Feature.IN_PROGRESS,
    Feature.PINS,
)
NOT_EXACT = (
    Feature.CALENDARS,
    Feature.PAUSE,
    Feature.OPERATORS,
    Feature.BATCHING,
    Feature.RESTART,
    Feature.MAX_LAGS,
)
MIN_COMPARED = 150
SWAPS = 500
T = TypeVar("T")


def _within_seconds(seconds: int, call: Callable[[], T]) -> T:
    """Run `call`; fail instead of hanging when it loops (SIGALRM, main thread)."""

    def _timeout(signum: int, frame: FrameType | None) -> None:
        raise TimeoutError(f"no answer within {seconds} s: looped on a cycle?")

    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(seconds)
    try:
        return call()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _graph_7_1_1(sequences: Mapping[str, Sequence[str]]) -> DisjunctiveGraph:
    request = load_request("pinedo_7_1_1")
    index = ProblemIndex.build(compile_request(request).problem)
    return DisjunctiveGraph.build(index, only_choice(request), sequences)


# --- graph_exact ---------------------------------------------------------------------------------


def test_graph_exact_accepts_exact_features() -> None:
    assert graph_exact(frozenset())
    assert graph_exact(frozenset(EXACT))


@pytest.mark.parametrize("feature", NOT_EXACT)
def test_graph_exact_rejects_each_estimate_only_feature(feature: Feature) -> None:
    assert not graph_exact(frozenset({feature}))
    assert not graph_exact(frozenset(EXACT) | {feature})


# --- Pinedo 7.1.1 --------------------------------------------------------------------------------


def test_7_1_1_job_arcs_only_longest_path_is_22() -> None:
    graph = _graph_7_1_1({})

    assert max(graph.completions().values()) == 22.0
    assert graph.completions()["2"] == 22.0


def test_7_1_1_optimal_sequences_makespan_28_and_heads_match_decoder() -> None:
    request = load_request("pinedo_7_1_1")
    index = ProblemIndex.build(compile_request(request).problem)
    decoded = decode(index, only_choice(request), OPTIMAL_7_1_1)
    assert isinstance(decoded, Decoded)

    graph = DisjunctiveGraph.build(index, only_choice(request), OPTIMAL_7_1_1)

    assert max(graph.completions().values()) == 28.0
    assert graph.heads() == {o: decoded.start(o) for o in RUNS_7_1_1}


def _adjacent(a: str, b: str) -> bool:
    """a -> b is a job arc (same job, next op) or a machine arc in OPTIMAL_7_1_1."""
    job_a, k_a = a.split("-")
    job_b, k_b = b.split("-")
    if job_a == job_b and int(k_b) == int(k_a) + 1:
        return True
    return any(
        tuple(seq[i : i + 2]) == (a, b) for seq in OPTIMAL_7_1_1.values() for i in range(len(seq))
    )


def test_7_1_1_critical_path_is_a_source_to_sink_path_of_length_28() -> None:
    graph = _graph_7_1_1(OPTIMAL_7_1_1)
    heads = graph.heads()

    path = graph.critical_path()

    assert len(path) >= 2
    assert heads[path[0]] == 0.0
    for a, b in zip(path, path[1:], strict=False):
        assert _adjacent(a, b), (a, b)
        assert heads[b] == heads[a] + RUNS_7_1_1[a], (a, b)
    assert heads[path[-1]] + RUNS_7_1_1[path[-1]] == 28.0


def test_7_1_1_critical_path_to_a_job_sink_ends_at_that_job() -> None:
    graph = _graph_7_1_1(OPTIMAL_7_1_1)

    path = graph.critical_path("1")

    assert path[-1] == "1-3"
    assert graph.heads()[path[-1]] + RUNS_7_1_1["1-3"] == graph.completions()["1"]


def test_cycle_is_reported_not_looped() -> None:
    sequences = dict(OPTIMAL_7_1_1)
    sequences["M1"] = ("2-2", "1-1", "3-1")
    sequences["M2"] = ("1-2", "2-1", "3-2")

    with pytest.raises(GraphCycle):
        _within_seconds(5, lambda: _graph_7_1_1(sequences))


def test_graph_cycle_is_a_value_error() -> None:
    assert issubclass(GraphCycle, ValueError)


# --- paths vs brute force ------------------------------------------------------------------------


def _arcs(
    request_seed: int,
) -> tuple[DisjunctiveGraph, dict[str, list[tuple[str, float]]], list[str]]:
    request, choice, sequences = random_job_shop(request_seed)
    problem = compile_request(request).problem
    run = {o.id: o.options[0].fixed_seconds for job in problem.jobs for o in job.operations}
    arcs: dict[str, list[tuple[str, float]]] = {o: [] for o in run}
    for job in problem.jobs:
        for o in job.operations:
            for link in o.predecessors:
                arcs[link.operation_id].append((o.id, run[link.operation_id] + link.min_lag))
    for seq in sequences.values():
        for a, b in zip(seq, seq[1:], strict=False):
            arcs[a].append((b, run[a]))
    graph = DisjunctiveGraph.build(ProblemIndex.build(problem), choice, sequences)
    return graph, arcs, sorted(run)


def _all_paths(arcs: Mapping[str, list[tuple[str, float]]], a: str, b: str) -> Iterator[float]:
    """Length of every a -> b path, by exhaustive DFS (the graph is a small DAG)."""
    stack: list[tuple[str, float]] = [(a, 0.0)]
    while stack:
        node, length = stack.pop()
        for nxt, weight in arcs[node]:
            if nxt == b:
                yield length + weight
            else:
                stack.append((nxt, length + weight))


@pytest.mark.parametrize("seed", range(20))
def test_has_path_and_longest_path_match_brute_force_dfs(seed: int) -> None:
    graph, arcs, ops = _arcs(seed)

    for a in ops:
        for b in ops:
            if a == b:
                continue
            lengths = list(_all_paths(arcs, a, b))
            assert graph.has_path(a, b) == bool(lengths), (a, b)
            expected = max(lengths) if lengths else None
            assert graph.longest_path(a, b) == expected, (a, b)


# --- incremental swaps ---------------------------------------------------------------------------


def _copy(sequences: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """A private copy, so a graph that keeps or mutates its input cannot touch ours."""
    return {m: list(seq) for m, seq in sequences.items()}


def test_incremental_swap_equals_full_recompute_on_500_swaps() -> None:
    applied = cycles = 0
    for seed in range(200):
        request, choice, sequences = random_job_shop(seed, setups=True)
        index = ProblemIndex.build(compile_request(request).problem)
        graph = DisjunctiveGraph.build(index, choice, _copy(sequences))
        rng = np.random.default_rng((seed, 500))
        for _ in range(20):
            busy = sorted(m for m, seq in sequences.items() if len(seq) >= 2)
            m = busy[int(rng.integers(len(busy)))]
            k = int(rng.integers(len(sequences[m]) - 1))
            u, v = sequences[m][k], sequences[m][k + 1]
            swapped = {**sequences, m: [*sequences[m][:k], v, u, *sequences[m][k + 2 :]]}
            try:
                full = DisjunctiveGraph.build(index, choice, _copy(swapped))
            except GraphCycle:
                before = graph.heads()
                with pytest.raises(GraphCycle):
                    graph.apply_swap(u, v)
                assert graph.heads() == before, f"seed {seed}: failed swap changed heads"
                cycles += 1
                continue
            graph.apply_swap(u, v)
            sequences = swapped
            assert graph.heads() == pytest.approx(full.heads()), f"seed {seed}: {u}<->{v}"
            assert graph.completions() == pytest.approx(full.completions())
            applied += 1
        if applied >= SWAPS:
            break
    print(f"\nswaps applied {applied}, cycle swaps refused {cycles}")
    assert applied >= SWAPS


# --- graph vs decoder on generated problems -----------------------------------------------------


@functools.cache
def _graph_vs_decoder(seed: int) -> bool | None:
    """True when graph and decoder agree; None when the seed is not comparable."""
    request = without_features(random_request(seed), STRIPPABLE)
    compiled = compile_request(request)
    assert graph_exact(problem_features(compiled.problem)), f"seed {seed} not graph-exact"
    source = solve(request)
    if not source.verified:
        return None
    index = ProblemIndex.build(compiled.problem)
    choice, sequences = machine_choice(source.schedule), machine_sequences(source.schedule)
    decoded = decode(index, choice, sequences)
    if not isinstance(decoded, Decoded):
        return None
    graph = DisjunctiveGraph.build(index, choice, sequences)
    heads = graph.heads()
    for op_id in choice:
        assert abs(heads[op_id] - decoded.start(op_id)) <= EPS, f"seed {seed}: head of {op_id}"
    for job_id, completion in graph.completions().items():
        assert abs(completion - decoded.completion(job_id)) <= EPS, f"seed {seed}: C of {job_id}"
    return True


def test_heads_and_completions_equal_decoder_on_200_graph_exact_problems() -> None:
    compared = sum(1 for seed in range(200) if _graph_vs_decoder(seed))
    print(f"\ngraph == decoder on {compared} of 200 graph-exact seeds")
    assert compared >= MIN_COMPARED


# --- tails (shifting bottleneck q(o), SDD pseudo-code "Shifting bottleneck, makespan") --------


def _job_of(op_id: str) -> str:
    return op_id.split("-")[0]


def _brute_tails(
    arcs: Mapping[str, list[tuple[str, float]]], run: Mapping[str, float], sink: str | None
) -> dict[str, float]:
    """max over reachable-or-self x (in job `sink` when given) of path(o, x) + run(x)."""
    tails: dict[str, float] = {}
    for o in run:
        best: float | None = None
        for x in run:
            if sink is not None and _job_of(x) != sink:
                continue
            length = 0.0 if x == o else max(_all_paths(arcs, o, x), default=None)
            if length is not None:
                best = length + run[x] if best is None else max(best, length + run[x])
        if best is not None:
            tails[o] = best
    return tails


def test_7_1_1_tails_with_job_arcs_only_are_the_remaining_runs() -> None:
    graph = _graph_7_1_1({})

    assert graph.tails() == {
        "1-1": 22.0, "1-2": 12.0, "1-3": 4.0,
        "2-1": 22.0, "2-2": 14.0, "2-3": 11.0, "2-4": 6.0,
        "3-1": 14.0, "3-2": 10.0, "3-3": 3.0,
    }  # fmt: skip
    assert graph.tails("3") == {"3-1": 14.0, "3-2": 10.0, "3-3": 3.0}


def test_7_1_1_head_plus_tail_is_cmax_exactly_on_the_critical_path() -> None:
    graph = _graph_7_1_1(OPTIMAL_7_1_1)
    heads, tails = graph.heads(), graph.tails()
    critical = set(graph.critical_path())

    for op_id in RUNS_7_1_1:
        assert heads[op_id] + tails[op_id] <= 28.0, op_id
    assert all(heads[o] + tails[o] == 28.0 for o in critical)


@pytest.mark.parametrize("seed", range(20))
def test_tails_match_brute_force_dfs_for_the_makespan_and_every_job_sink(seed: int) -> None:
    request, choice, sequences = random_job_shop(seed)
    graph, arcs, ops = _arcs(seed)
    problem = compile_request(request).problem
    run = {o.id: o.options[0].fixed_seconds for job in problem.jobs for o in job.operations}

    assert graph.tails() == _brute_tails(arcs, run, None)
    for job_id in sorted({_job_of(o) for o in ops}):
        assert graph.tails(job_id) == _brute_tails(arcs, run, job_id), job_id


def test_tails_after_incremental_swaps_equal_a_fresh_build() -> None:
    checked = 0
    for seed in range(40):
        request, choice, sequences = random_job_shop(seed, setups=True)
        index = ProblemIndex.build(compile_request(request).problem)
        graph = DisjunctiveGraph.build(index, choice, _copy(sequences))
        rng = np.random.default_rng((seed, 141))
        for _ in range(5):
            busy = sorted(m for m, seq in sequences.items() if len(seq) >= 2)
            m = busy[int(rng.integers(len(busy)))]
            k = int(rng.integers(len(sequences[m]) - 1))
            u, v = sequences[m][k], sequences[m][k + 1]
            swapped = {**sequences, m: [*sequences[m][:k], v, u, *sequences[m][k + 2 :]]}
            try:
                full = DisjunctiveGraph.build(index, choice, _copy(swapped))
            except GraphCycle:
                continue
            graph.apply_swap(u, v)
            sequences = swapped
            assert graph.tails() == pytest.approx(full.tails()), f"seed {seed}: {u}<->{v}"
            job = _job_of(u)
            assert graph.tails(job) == pytest.approx(full.tails(job))
            checked += 1
    assert checked >= 100


def test_tails_of_a_job_sink_never_include_ops_that_cannot_reach_it() -> None:
    graph = _graph_7_1_1({})

    assert set(graph.tails("1")) == {"1-1", "1-2", "1-3"}
    with pytest.raises(ValueError):
        graph.tails("no-such-job")


# --- path and tail matrices (T-143: shifting bottleneck at 600 x 8, SDD Risks) ---------------


@pytest.mark.parametrize("seed", range(20))
def test_path_lengths_matrix_equals_longest_path_for_every_pair(seed: int) -> None:
    """`path_lengths(op_ids)[i, j]` = longest_path(op_ids[i], op_ids[j]), -inf when none
    (the diagonal included): one pass for all pairs instead of one search per pair."""
    request, choice, sequences = random_job_shop(seed, setups=True)
    index = ProblemIndex.build(compile_request(request).problem)
    graph = DisjunctiveGraph.build(index, choice, sequences)
    ops = sorted(choice)[::2]

    lengths = graph.path_lengths(ops)

    assert lengths.shape == (len(ops), len(ops))
    for i, a in enumerate(ops):
        for j, b in enumerate(ops):
            expected = graph.longest_path(a, b) if a != b else None
            assert lengths[i, j] == (-np.inf if expected is None else expected), (a, b)


@pytest.mark.parametrize("seed", range(20))
def test_sink_tails_matrix_equals_tails_of_every_job_sink(seed: int) -> None:
    """`sink_tails(op_ids)[i, k]` = tails(job k)[op_ids[i]] for job k = index.jobs[k], -inf
    when the op does not reach V_k."""
    request, choice, sequences = random_job_shop(seed, setups=True)
    index = ProblemIndex.build(compile_request(request).problem)
    graph = DisjunctiveGraph.build(index, choice, sequences)
    ops = sorted(choice)

    tails = graph.sink_tails(ops)

    assert tails.shape == (len(ops), len(index.jobs))
    for k, job in enumerate(index.jobs):
        expected = graph.tails(job.id)
        for i, op_id in enumerate(ops):
            assert tails[i, k] == pytest.approx(expected.get(op_id, -np.inf)), (op_id, job.id)


def test_path_and_tail_matrices_on_7_1_1_job_arcs_only() -> None:
    graph = _graph_7_1_1({})

    lengths = graph.path_lengths(["1-1", "1-3", "2-1"])
    tails = graph.sink_tails(["1-1", "3-3"])

    assert lengths.tolist() == [
        [-np.inf, 18.0, -np.inf],
        [-np.inf, -np.inf, -np.inf],
        [-np.inf, -np.inf, -np.inf],
    ]  # 1-1 runs 10, 1-2 runs 8 (Pinedo Example 7.1.1)
    assert tails.tolist() == [[22.0, -np.inf, -np.inf], [-np.inf, -np.inf, 3.0]]


# --- node moves between machines (T-146, reassign moves) ---------------------------------------


def _moved(
    sequences: Mapping[str, Sequence[str]], op_id: str, target: str, position: int
) -> dict[str, list[str]]:
    moved = {m: [o for o in seq if o != op_id] for m, seq in sequences.items()}
    moved.setdefault(target, []).insert(position, op_id)
    return moved


def _random_node_move(
    rng: np.random.Generator,
    index: ProblemIndex,
    choice: Mapping[str, str],
    sequences: Mapping[str, Sequence[str]],
) -> tuple[str, str, int] | None:
    op_id = sorted(choice)[int(rng.integers(len(choice)))]
    others = sorted(o.machine_id for o in index.op(op_id).options if o.machine_id != choice[op_id])
    if not others:
        return None
    target = others[int(rng.integers(len(others)))]
    return op_id, target, int(rng.integers(len(sequences.get(target, ())) + 1))


def test_move_node_equals_full_recompute_on_500_moves() -> None:
    applied = cycles = 0
    for seed in range(200):
        request, choice, sequences = random_job_shop(seed, setups=True)
        index = ProblemIndex.build(compile_request(request).problem)
        graph = DisjunctiveGraph.build(index, choice, _copy(sequences))
        rng = np.random.default_rng((seed, 146))
        for _ in range(20):
            picked = _random_node_move(rng, index, choice, sequences)
            if picked is None:
                continue
            op_id, target, position = picked
            moved = _moved(sequences, op_id, target, position)
            moved_choice = {**choice, op_id: target}
            try:
                full = DisjunctiveGraph.build(index, moved_choice, _copy(moved))
            except GraphCycle:
                before = graph.heads()
                with pytest.raises(GraphCycle):
                    graph.move_node(op_id, target, position)
                assert graph.heads() == before, f"seed {seed}: failed move changed heads"
                assert graph.sequences() == {m: s for m, s in sequences.items() if s}
                cycles += 1
                continue
            graph.move_node(op_id, target, position)
            choice, sequences = moved_choice, moved
            assert graph.sequences() == {m: s for m, s in sequences.items() if s}
            assert graph.heads() == pytest.approx(full.heads()), f"seed {seed}: {op_id}"
            assert graph.completions() == pytest.approx(full.completions())
            assert graph.tails() == pytest.approx(full.tails())
            applied += 1
        if applied >= SWAPS:
            break
    print(f"\nnode moves applied {applied}, cycle moves refused {cycles}")
    assert applied >= SWAPS
    assert cycles >= 1


def test_move_node_back_restores_the_heads_exactly() -> None:
    request, choice, sequences = random_job_shop(3, setups=True)
    index = ProblemIndex.build(compile_request(request).problem)
    graph = DisjunctiveGraph.build(index, choice, _copy(sequences))
    heads = graph.heads()
    rng = np.random.default_rng(7)
    restored = 0
    for _ in range(50):
        picked = _random_node_move(rng, index, choice, sequences)
        if picked is None:
            continue
        op_id, target, position = picked
        home = choice[op_id]
        back = sequences[home].index(op_id)
        try:
            graph.move_node(op_id, target, position)
        except GraphCycle:
            continue
        graph.move_node(op_id, home, back)
        assert graph.heads() == heads
        assert graph.sequences() == {m: s for m, s in sequences.items() if s}
        restored += 1
    assert restored >= 10


def test_move_node_refuses_bad_input_and_leaves_the_graph_unchanged() -> None:
    request, choice, sequences = random_job_shop(0, setups=True)
    index = ProblemIndex.build(compile_request(request).problem)
    graph = DisjunctiveGraph.build(index, choice, _copy(sequences))
    op_id = sorted(choice)[0]
    other = next(o.machine_id for o in index.op(op_id).options if o.machine_id != choice[op_id])
    ineligible = ({"M0", "M1", "M2"} - {o.machine_id for o in index.op(op_id).options}).pop()
    heads = graph.heads()

    with pytest.raises(ValueError):
        graph.move_node(op_id, ineligible, 0)
    with pytest.raises(ValueError):
        graph.move_node(op_id, choice[op_id], 0)
    with pytest.raises(ValueError):
        graph.move_node(op_id, other, len(sequences[other]) + 1)
    with pytest.raises(ValueError):
        graph.move_node("no-such-op", other, 0)
    assert graph.heads() == heads


def test_sequences_lists_every_machine_chain_in_order() -> None:
    graph = _graph_7_1_1(OPTIMAL_7_1_1)

    assert graph.sequences() == {m: list(seq) for m, seq in OPTIMAL_7_1_1.items()}
    assert _graph_7_1_1({}).sequences() == {}


def test_as_decoded_equals_the_decoder_on_7_1_1() -> None:
    request = load_request("pinedo_7_1_1")
    index = ProblemIndex.build(compile_request(request).problem)
    decoded = decode(index, only_choice(request), OPTIMAL_7_1_1)
    assert isinstance(decoded, Decoded)
    graph = DisjunctiveGraph.build(index, only_choice(request), OPTIMAL_7_1_1)

    estimate = graph.as_decoded()

    assert estimate.options == decoded.options
    assert estimate.starts == decoded.starts
    assert estimate.setup_ends == decoded.setup_ends
    assert estimate.ends == decoded.ends
    assert estimate.batch_ids == (None,) * len(index.ops)


def test_as_decoded_needs_every_op_as_a_node() -> None:
    request = load_request("pinedo_7_1_1")
    index = ProblemIndex.build(compile_request(request).problem)
    choice = only_choice(request)
    del choice["1-1"]
    graph = DisjunctiveGraph.build(index, choice, {})

    with pytest.raises(ValueError):
        graph.as_decoded()


def test_placement_is_the_machine_and_index_in_its_sequence() -> None:
    graph = _graph_7_1_1(OPTIMAL_7_1_1)

    for machine_id, sequence in OPTIMAL_7_1_1.items():
        for k, op_id in enumerate(sequence):
            assert graph.placement(op_id) == (machine_id, k)
