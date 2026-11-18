"""Moves, neighborhoods and incremental evaluation (COMP-017, REQ-108, REQ-111, D-076).

SDD pseudo-code "Tabu search (Alg 14.3.3) and its neighborhood (p.378-380)".

- `moves(graph, spec, index)`: the neighborhood of the graph's solution, sorted by
  `Move.key`. Critical paths: a due-date primary objective (lateness, tardiness, tardy
  counts, earliness-tardiness) uses the paths to V_k of the up to 5 tardy jobs (C_k > d_k)
  with the largest w_k T_k (ties: job id); every other primary objective uses the one
  makespan path (`graph.critical_path()`), a makespan proxy for objectives such as
  weighted completion, setup or cost. Moves on those paths:
  - swap(u, v): consecutive path entries, adjacent on one machine, of different jobs
    (p.379); an in-progress op is never swapped (it must stay first);
  - look-back(u, v, a, b): swap(u, v) plus swap(a, b) for each job predecessor b of v that
    has a machine predecessor a of another job, a not in progress (p.379-380, Fig 14.1);
  - reassign(op, old, machine, position): a path op moves to another eligible machine of
    its work center, at the least-added-setup position (added = s(prev, op) + s(op, next)
    - s(prev, next), prev None = the machine state; ties: lowest position; never before an
    in-progress op). Positions ignore cycles: `evaluate` and the decoder catch them.
- `apply(move, choice, sequences)`: the move on plain choice/sequences (pure).
- `evaluate(graph, move)`: the graph's schedule after the move as a `Decoded`, by the
  graph's incremental swap / node move and the reverse step afterwards (no rebuild); the
  graph is left as it was, also when the move closes a cycle (`GraphCycle`).
- `confirm(...)`: decodes the moved solution and scores the *decoded* values (D-076): the
  graph estimate screens moves, it never decides; exact only on `graph_exact` problems.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.graph import DisjunctiveGraph
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.score import Scorer, Values, fast_values
from twinflow.scheduling.model import ObjectiveKind, ObjectiveSpec, PreviousPlan, ScheduleIssue
from twinflow.scheduling.objectives import primary_kind

MoveKind = Literal["swap", "lookback", "reassign"]
MoveKey = tuple[str, tuple[str, ...], str, str, int]
Attribute = frozenset[str] | tuple[str, str]
Placement = Mapping[str, tuple[str, int]]

MAX_DUE_PATHS = 5
"""Due-date objectives use the paths of at most this many tardy jobs (SDD pseudo-code)."""

DUE_DATE_KINDS = frozenset(
    {
        ObjectiveKind.MAX_LATENESS,
        ObjectiveKind.TARDY_COUNT,
        ObjectiveKind.WEIGHTED_TARDY_COUNT,
        ObjectiveKind.TOTAL_TARDINESS,
        ObjectiveKind.WEIGHTED_TARDINESS,
        ObjectiveKind.EARLINESS_TARDINESS,
    }
)
"""Primary objectives whose neighborhood follows the tardy jobs' sink paths."""


@dataclass(frozen=True, slots=True)
class Move:
    """One neighborhood move; build it with `swap`, `lookback` or `reassign`.

    `ops` is (u, v), (u, v, a, b) or (op,). `old_machine`, `machine` and `position` belong
    to reassign moves ("", "" and -1 otherwise).
    """

    kind: MoveKind
    ops: tuple[str, ...]
    old_machine: str = ""
    machine: str = ""
    position: int = -1

    @classmethod
    def swap(cls, u: str, v: str) -> Move:
        """Swap `u` and its machine follower `v`."""
        return cls("swap", (u, v))

    @classmethod
    def lookback(cls, u: str, v: str, a: str, b: str) -> Move:
        """swap(u, v) plus swap(a, b): b is v's job predecessor, a its machine predecessor."""
        return cls("lookback", (u, v, a, b))

    @classmethod
    def reassign(cls, op_id: str, old_machine: str, machine: str, position: int) -> Move:
        """Move `op_id` from `old_machine` to index `position` of `machine`'s sequence."""
        return cls("reassign", (op_id,), old_machine, machine, position)

    @property
    def key(self) -> MoveKey:
        """A total order over distinct moves of every kind."""
        return (self.kind, self.ops, self.old_machine, self.machine, self.position)

    @property
    def attribute(self) -> Attribute:
        """Tabu attribute: the unordered primary pair, or (op, old machine) for reassign."""
        if self.kind == "reassign":
            return (self.ops[0], self.old_machine)
        return frozenset(self.ops[:2])


@dataclass(frozen=True, slots=True)
class Confirmed:
    """A decoded move: the schedule, its values and whether they beat the best."""

    decoded: Decoded
    values: dict[ObjectiveKind, float]
    improved: bool


# --- neighborhood ------------------------------------------------------------------------------


def moves(graph: DisjunctiveGraph, spec: ObjectiveSpec, index: ProblemIndex) -> list[Move]:
    """The neighborhood of the graph's solution, sorted by key (see the module docstring)."""
    sequences = graph.sequences()
    placement = {o: (m, k) for m, seq in sequences.items() for k, o in enumerate(seq)}
    if not placement:
        return []
    found: dict[MoveKey, Move] = {}
    reassigned: set[str] = set()
    for path in _critical_paths(graph, spec, index):
        for u, v in pairwise(path):
            for move in _swaps(index, sequences, placement, u, v):
                found[move.key] = move
        for op_id in path:
            if op_id not in reassigned:
                reassigned.add(op_id)
                for move in _reassigns(index, sequences, placement, op_id):
                    found[move.key] = move
    return sorted(found.values(), key=lambda m: m.key)


def _critical_paths(
    graph: DisjunctiveGraph, spec: ObjectiveSpec, index: ProblemIndex
) -> list[list[str]]:
    if primary_kind(spec) not in DUE_DATE_KINDS:
        return [graph.critical_path()]
    completions = graph.completions()
    tardy = [
        (-job.weight * (completions[job.id] - job.due), job.id)
        for job in index.jobs
        if job.due is not None and job.id in completions and completions[job.id] > job.due
    ]
    worst = sorted(tardy)[:MAX_DUE_PATHS]
    return [graph.critical_path(job_id) for _, job_id in worst]


def _in_progress(index: ProblemIndex, op_id: str) -> bool:
    return index.op(op_id).in_progress is not None


def _swaps(
    index: ProblemIndex,
    sequences: Mapping[str, Sequence[str]],
    placement: Placement,
    u: str,
    v: str,
) -> Iterator[Move]:
    """swap(u, v) and its look-backs, when u, v are a swappable machine pair."""
    if u not in placement or v not in placement:
        return
    (mu, ku), (mv, kv) = placement[u], placement[v]
    if mu != mv or kv != ku + 1 or index.job_of(u) == index.job_of(v) or _in_progress(index, u):
        return
    yield Move.swap(u, v)
    for link in index.op(v).predecessors:
        b = link.pred_id
        # A cross-job link (D-084) can make u itself v's predecessor: the first swap then
        # moves b, so no second swap (a, b) exists.
        if b == u or b not in placement or placement[b][1] == 0:
            continue
        mb, kb = placement[b]
        a = sequences[mb][kb - 1]
        if index.job_of(a) != index.job_of(b) and not _in_progress(index, a):
            yield Move.lookback(u, v, a, b)


def _reassigns(
    index: ProblemIndex,
    sequences: Mapping[str, Sequence[str]],
    placement: Placement,
    op_id: str,
) -> Iterator[Move]:
    """One reassign per other eligible machine of the op's center, least added setup."""
    op = index.op(op_id)
    if op_id not in placement or op.in_progress is not None:
        return
    current = placement[op_id][0]
    center = index.machines_of(op.work_center_id)
    for option in op.options:
        machine_id = option.machine_id
        if machine_id == current or machine_id not in center:
            continue
        target = sequences.get(machine_id, ())
        yield Move.reassign(
            op_id, current, machine_id, _least_added_setup(index, machine_id, target, op_id)
        )


def _least_added_setup(
    index: ProblemIndex, machine_id: str, target: Sequence[str], op_id: str
) -> int:
    """Insertion index into `target` with the least added setup (lowest index on ties)."""
    seconds = index.setups.seconds
    first = 1 if target and _in_progress(index, target[0]) else 0
    best, best_added = first, float("inf")
    for k in range(first, len(target) + 1):
        prev = target[k - 1] if k > 0 else None
        added = seconds(machine_id, prev, op_id)
        if k < len(target):
            added += seconds(machine_id, op_id, target[k]) - seconds(machine_id, prev, target[k])
        if added < best_added:
            best, best_added = k, added
    return best


# --- apply and evaluate ------------------------------------------------------------------------


def apply(
    move: Move, choice: Mapping[str, str], sequences: Mapping[str, Sequence[str]]
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """The choice and sequences after `move` (inputs untouched); ValueError when it misfits."""
    new_choice = dict(choice)
    new_sequences = {m: list(seq) for m, seq in sequences.items()}
    if move.kind == "reassign":
        _reassign_in(move, new_choice, new_sequences)
        return new_choice, new_sequences
    for u, v in zip(move.ops[::2], move.ops[1::2], strict=True):
        _swap_in(u, v, choice, new_sequences)
    return new_choice, new_sequences


def _swap_in(u: str, v: str, choice: Mapping[str, str], sequences: dict[str, list[str]]) -> None:
    sequence = sequences.get(choice.get(u, ""), [])
    k = sequence.index(u) if u in sequence else -1
    if k < 0 or k + 1 >= len(sequence) or sequence[k + 1] != v:
        raise ValueError(f"{v} does not directly follow {u} on a machine")
    sequence[k], sequence[k + 1] = v, u


def _reassign_in(move: Move, choice: dict[str, str], sequences: dict[str, list[str]]) -> None:
    (op_id,) = move.ops
    source = sequences.get(move.old_machine, [])
    if choice.get(op_id) != move.old_machine or op_id not in source:
        raise ValueError(f"{op_id} is not sequenced on {move.old_machine}")
    target = sequences.setdefault(move.machine, [])
    if not 0 <= move.position <= len(target):
        raise ValueError(f"position {move.position} is outside 0..{len(target)}")
    source.remove(op_id)
    target.insert(move.position, op_id)
    choice[op_id] = move.machine


def evaluate(graph: DisjunctiveGraph, move: Move) -> Decoded:
    """The graph's schedule after `move`; the graph is restored (also on `GraphCycle`)."""
    if move.kind == "reassign":
        return _evaluate_reassign(graph, move)
    applied: list[tuple[str, str]] = []
    try:
        for u, v in zip(move.ops[::2], move.ops[1::2], strict=True):
            graph.apply_swap(u, v)
            applied.append((u, v))
        return graph.as_decoded()
    finally:
        for u, v in reversed(applied):
            graph.apply_swap(v, u)


def _evaluate_reassign(graph: DisjunctiveGraph, move: Move) -> Decoded:
    (op_id,) = move.ops
    home, k = graph.placement(op_id)
    if home != move.old_machine:
        raise ValueError(f"{op_id} is on {home}, not {move.old_machine}")
    graph.move_node(op_id, move.machine, move.position)
    try:
        return graph.as_decoded()
    finally:
        graph.move_node(op_id, home, k)


# --- decode confirmation (D-076) -------------------------------------------------------------


def confirm(
    index: ProblemIndex,
    estimate: Values,
    choice: Mapping[str, str],
    sequences: Mapping[str, Sequence[str]],
    scorer: Scorer,
    best: Values,
    *,
    previous_plan: PreviousPlan | None = None,
    churn_tolerance: float = 0.0,
) -> Confirmed | ScheduleIssue:
    """Decode the moved solution and score its decoded values against `best`.

    `estimate` (the graph's values) is what selected the move; it never decides here: the
    returned values and `improved` come from the decode alone (D-076). Churn is scored
    against `previous_plan` (0 without one).
    """
    decoded = decode(index, choice, sequences)
    if isinstance(decoded, ScheduleIssue):
        return decoded
    values = fast_values(index, decoded, previous_plan, churn_tolerance)
    return Confirmed(decoded, values, scorer.better(values, best))
