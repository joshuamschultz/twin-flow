"""Disjunctive graph over a fixed machine choice and sequences (COMP-005, REQ-099, REQ-111, D-076).

SDD Data Model "Disjunctive graph" (Pinedo p.180-182, p.191, extended to the v2 model).
Nodes are the scheduled ops that have a chosen machine; each has a dense int position from
the `ProblemIndex`, and every hot loop works on those ints and flat lists.

- `dur(v) = setup(v) + run(v)`, `setup(v) = s(machine predecessor of v, v)` (machine state
  when first); `det(v) = setup(v)` when `setup_detached`, else 0. An in-progress op has
  head = now, setup 0 and dur = remaining seconds.
- Head lower bound `h0(v) = max(now, available_from, pin.earliest_start,
  release(v) - det(v))`, where `release` is the index's max(job release, ready time).
- Job arc u -> v without overlap: `dur(u) + min_lag - det(v)`. With overlap q:
  `setup(u) + fixed_u + q * unit_u + min_lag - det(v)` and a second arc
  `dur(u) + q * unit_v - dur(v)`; paths take the larger of the two.
- Machine arc u -> v (consecutive on a machine): `dur(u)`. A machine absent from the
  sequences adds no arcs. Sink of job k: `C_k = max over job k ops (head + dur)`.

Heads come from one topological DP (Kahn order, O(n + arcs)). `apply_swap(u, v)` swaps
adjacent ops on one machine: it refuses a swap that closes a cycle (another u -> v path)
before touching anything, reorders only the topological window between u and v (the ops
reachable from u move after the rest), recomputes the setups of v, u and the follower,
and recomputes heads from the first changed node, only where a head can change.

`graph_exact(features)`: the graph's values equal the decoder's for these features
(D-076). Pins count as exact: a pin's earliest start is a head bound, and a latest start
only rejects in the decoder, never moves an op.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence, Set
from dataclasses import dataclass

from twinflow.scheduling.kernel.index import OpData, OpOption, ProblemIndex
from twinflow.scheduling.seam import Feature

GRAPH_EXACT_FEATURES = frozenset(
    {
        Feature.ALTERNATIVES,
        Feature.SETUPS,
        Feature.DETACHED_SETUP,
        Feature.LAGS,
        Feature.OVERLAP,
        Feature.MACHINE_STATE,
        Feature.IN_PROGRESS,
        Feature.PINS,
    }
)
"""Features for which graph heads and completions equal the decoder's (D-076)."""

_NONE = -1
"""Position sentinel: no machine predecessor or follower, or not a node."""


class GraphCycle(ValueError):
    """The machine sequences and job links close a cycle; no schedule exists for them."""


def graph_exact(features: Set[Feature]) -> bool:
    """True when every feature keeps graph values equal to the decoder's (D-076)."""
    return features <= GRAPH_EXACT_FEATURES


@dataclass(frozen=True, slots=True)
class _JobArc:
    """A job link u -> v; `lot` is q * unit_v with overlap, else None.

    `const` is `min_lag` without overlap and `fixed_u + q * unit_u + min_lag` with it.
    """

    u: int
    v: int
    const: float
    lot: float | None


class DisjunctiveGraph:
    """Heads, completions and paths of one machine choice and sequence set (see module)."""

    __slots__ = (
        "_arcs_in",
        "_arcs_out",
        "_det",
        "_detached",
        "_dur",
        "_floor",
        "_head",
        "_in_progress",
        "_index",
        "_machine",
        "_mnext",
        "_mprev",
        "_nodes",
        "_order",
        "_rank",
        "_release",
        "_run",
        "_setup",
    )

    def __init__(self, index: ProblemIndex, choice: Mapping[str, str]) -> None:
        n = len(index.ops)
        self._index = index
        self._nodes = tuple(sorted(_node_position(index, op_id) for op_id in choice))
        self._machine = [""] * n
        self._run = [0.0] * n
        self._floor = [0.0] * n
        self._release = [0.0] * n
        self._detached = [False] * n
        self._in_progress = [False] * n
        self._setup = [0.0] * n
        self._det = [0.0] * n
        self._dur = [0.0] * n
        self._mprev = [_NONE] * n
        self._mnext = [_NONE] * n
        self._head = [0.0] * n
        self._order: list[int] = []
        self._rank = [_NONE] * n
        options: dict[int, OpOption] = {}
        for op_id, machine_id in choice.items():
            p = index.pos[op_id]
            options[p] = _chosen_option(index.ops[p], machine_id)
            self._set_node(p, machine_id, options[p])
        self._arcs_in, self._arcs_out = _job_arcs(index, options, n)

    @classmethod
    def build(
        cls,
        index: ProblemIndex,
        choice: Mapping[str, str],
        sequences: Mapping[str, Sequence[str]],
    ) -> DisjunctiveGraph:
        """The graph of `choice` (op -> machine) and `sequences` (machine -> op ids).

        Raises `GraphCycle` when the arcs close a cycle and ValueError when an op is not
        scheduled, has no option on its chosen machine, or is sequenced elsewhere.
        """
        graph = cls(index, choice)
        for machine_id, sequence in sequences.items():
            graph._link_sequence(machine_id, sequence, choice)
        for p in graph._nodes:
            graph._refresh_duration(p)
        graph._order = graph._topological_order()
        for rank, p in enumerate(graph._order):
            graph._rank[p] = rank
        for p in graph._order:
            graph._head[p] = graph._head_of(p)
        return graph

    # --- queries ---------------------------------------------------------------------------

    def heads(self) -> dict[str, float]:
        """Op id -> earliest start (longest path from the source)."""
        op_ids = self._index.op_ids
        return {op_ids[p]: self._head[p] for p in self._nodes}

    def completions(self) -> dict[str, float]:
        """Job id -> C_k, the max of head + dur over the job's nodes (jobs with nodes only)."""
        completions: dict[str, float] = {}
        for job in self._index.jobs:
            ends = [self._head[p] + self._dur[p] for p in job.ops if self._rank[p] != _NONE]
            if ends:
                completions[job.id] = max(ends)
        return completions

    def critical_path(self, sink: str | None = None) -> list[str]:
        """Op ids of one longest source path to the makespan sink, or to job `sink`'s V_k."""
        candidates = self._nodes if sink is None else self._job_nodes(sink)
        end = max(reversed(candidates), key=lambda p: self._head[p] + self._dur[p])
        path = [end]
        tight = self._tight_predecessor(end)
        while tight != _NONE:
            path.append(tight)
            tight = self._tight_predecessor(tight)
        op_ids = self._index.op_ids
        return [op_ids[p] for p in reversed(path)]

    def has_path(self, a: str, b: str) -> bool:
        """True when a directed path leads from op `a` to op `b`."""
        return self.longest_path(a, b) is not None

    def longest_path(self, a: str, b: str) -> float | None:
        """Length of the longest `a` -> `b` path (sum of arc weights); None when none."""
        start, target = self._node(a), self._node(b)
        if start == target:
            return None
        last = self._rank[target]
        dist: dict[int, float] = {start: 0.0}
        for rank in range(self._rank[start], last + 1):
            x = self._order[rank]
            if x not in dist:
                continue
            for y, weight in self._out_edges(x):
                length = dist[x] + weight
                if self._rank[y] <= last and length > dist.get(y, -math.inf):
                    dist[y] = length
        return dist.get(target)

    # --- incremental move ------------------------------------------------------------------

    def apply_swap(self, u: str, v: str) -> None:
        """Swap `u` and its machine follower `v`, updating heads incrementally.

        Raises `GraphCycle` (graph unchanged) when another u -> v path exists, and
        ValueError when `v` does not directly follow `u` on a machine.
        """
        pu, pv = self._node(u), self._node(v)
        if self._mnext[pu] != pv:
            raise ValueError(f"{v} does not directly follow {u} on a machine")
        reach = self._reach_without_arc(pu, pv)
        before, after = self._mprev[pu], self._mnext[pv]
        self._relink(before, pv, pu, after)
        self._reorder_window(pu, pv, reach)
        changed = [pv, pu] if after == _NONE else [pv, pu, after]
        for p in changed:
            self._refresh_duration(p)
        self._update_heads(changed)

    # --- build helpers ---------------------------------------------------------------------

    def _set_node(self, p: int, machine_id: str, option: OpOption) -> None:
        op = self._index.ops[p]
        machine = self._index.machines[self._index.machine_pos[machine_id]]
        self._machine[p] = machine_id
        if op.in_progress is not None:
            self._in_progress[p] = True
            self._run[p] = op.in_progress.remaining_seconds or 0.0
            self._floor[p] = self._index.now
            return
        earliest = op.pin.earliest_start if op.pin is not None else None
        bounds = [self._index.now, machine.available_from]
        self._floor[p] = max(bounds if earliest is None else [*bounds, earliest])
        self._release[p] = op.release
        self._run[p] = option.run
        self._detached[p] = op.setup_detached

    def _link_sequence(
        self, machine_id: str, sequence: Sequence[str], choice: Mapping[str, str]
    ) -> None:
        previous = _NONE
        for op_id in sequence:
            if choice.get(op_id) != machine_id:
                raise ValueError(f"{op_id} is sequenced on {machine_id} but not chosen there")
            p = self._index.pos[op_id]
            self._mprev[p] = previous
            if previous != _NONE:
                self._mnext[previous] = p
            previous = p

    def _topological_order(self) -> list[int]:
        """Kahn's order over job and machine arcs; `GraphCycle` when some node is left."""
        indegree = [0] * len(self._index.ops)
        for p in self._nodes:
            indegree[p] = len(self._arcs_in[p]) + (self._mprev[p] != _NONE)
        order = [p for p in self._nodes if indegree[p] == 0]
        for x in order:  # `order` grows while it is read: a FIFO queue without pops
            for y, _ in self._out_edges(x):
                indegree[y] -= 1
                if indegree[y] == 0:
                    order.append(y)
        if len(order) < len(self._nodes):
            stuck = sorted(self._index.op_ids[p] for p in self._nodes if indegree[p] > 0)
            raise GraphCycle(f"sequences close a cycle through {', '.join(stuck[:5])}")
        return order

    # --- arc arithmetic --------------------------------------------------------------------

    def _refresh_duration(self, p: int) -> None:
        """setup, det and dur of node `p` from its current machine predecessor."""
        setup = 0.0
        if not self._in_progress[p]:
            prev = self._mprev[p]
            prev_id = None if prev == _NONE else self._index.op_ids[prev]
            setup = self._index.setups.seconds(self._machine[p], prev_id, self._index.op_ids[p])
        self._setup[p] = setup
        self._det[p] = setup if self._detached[p] else 0.0
        self._dur[p] = setup + self._run[p]

    def _job_weight(self, arc: _JobArc) -> float:
        """Weight of job arc u -> v; with overlap, the larger of its two arcs."""
        u, v = arc.u, arc.v
        if arc.lot is None:
            return self._dur[u] + arc.const - self._det[v]
        start_bound = self._setup[u] + arc.const - self._det[v]
        return max(start_bound, self._dur[u] + arc.lot - self._dur[v])

    def _h0(self, p: int) -> float:
        if self._in_progress[p]:
            return self._floor[p]
        return max(self._floor[p], self._release[p] - self._det[p])

    def _head_of(self, p: int) -> float:
        """Max of h0 and every incoming arc's head + weight (predecessors done first)."""
        head = self._head
        best = self._h0(p)
        prev = self._mprev[p]
        if prev != _NONE:
            best = max(best, head[prev] + self._dur[prev])
        for arc in self._arcs_in[p]:
            best = max(best, head[arc.u] + self._job_weight(arc))
        return best

    def _out_edges(self, x: int) -> Iterator[tuple[int, float]]:
        """(successor, weight) of every arc leaving `x`: machine follower, then job arcs."""
        follower = self._mnext[x]
        if follower != _NONE:
            yield follower, self._dur[x]
        for arc in self._arcs_out[x]:
            yield arc.v, self._job_weight(arc)

    def _tight_predecessor(self, p: int) -> int:
        """A predecessor whose arc sets `p`'s head, or `_NONE` when h0 does."""
        head = self._head[p]
        prev = self._mprev[p]
        if prev != _NONE and self._head[prev] + self._dur[prev] == head:
            return prev
        for arc in self._arcs_in[p]:
            if self._head[arc.u] + self._job_weight(arc) == head:
                return arc.u
        return _NONE

    # --- swap helpers ----------------------------------------------------------------------

    def _reach_without_arc(self, u: int, v: int) -> set[int]:
        """Nodes reachable from `u` up to `v`'s rank, ignoring machine arc u -> v.

        Reaching `v` means the swap would close a cycle: raise `GraphCycle`.
        """
        last = self._rank[v]
        reach = {u}
        stack = [arc.v for arc in self._arcs_out[u]]
        while stack:
            x = stack.pop()
            if x == v:
                ids = self._index.op_ids
                raise GraphCycle(f"swapping {ids[u]} and {ids[v]} closes a cycle")
            if x in reach or self._rank[x] > last:
                continue
            reach.add(x)
            stack.extend(y for y, _ in self._out_edges(x))
        return reach

    def _relink(self, before: int, first: int, second: int, after: int) -> None:
        """Machine order becomes before -> first -> second -> after."""
        if before != _NONE:
            self._mnext[before] = first
        self._mprev[first] = before
        self._mnext[first] = second
        self._mprev[second] = first
        self._mnext[second] = after
        if after != _NONE:
            self._mprev[after] = second

    def _reorder_window(self, u: int, v: int, reach: set[int]) -> None:
        """Within ranks u..v, move the nodes reachable from u after all the others.

        No arc leads from `reach` to a window node outside it (that node would be in
        `reach`), so the order stays topological and the new arc v -> u is respected.
        """
        lo, hi = self._rank[u], self._rank[v]
        window = self._order[lo : hi + 1]
        moved = [x for x in window if x not in reach] + [x for x in window if x in reach]
        self._order[lo : hi + 1] = moved
        for rank, x in enumerate(moved, start=lo):
            self._rank[x] = rank

    def _update_heads(self, changed: list[int]) -> None:
        """Recompute heads in order from the first changed node, only where one can move."""
        dirty = set(changed)
        for p in changed:
            dirty.update(y for y, _ in self._out_edges(p))
        for rank in range(min(self._rank[p] for p in changed), len(self._order)):
            if not dirty:
                return
            x = self._order[rank]
            if x not in dirty:
                continue
            dirty.discard(x)
            head = self._head_of(x)
            if head != self._head[x]:
                self._head[x] = head
                dirty.update(y for y, _ in self._out_edges(x))

    # --- lookups ---------------------------------------------------------------------------

    def _node(self, op_id: str) -> int:
        p = self._index.pos.get(op_id, _NONE)
        if p == _NONE or self._rank[p] == _NONE:
            raise ValueError(f"{op_id} is not a node of this graph")
        return p

    def _job_nodes(self, job_id: str) -> tuple[int, ...]:
        job = self._index.jobs[self._index.job_pos[job_id]]
        nodes = tuple(p for p in job.ops if self._rank[p] != _NONE)
        if not nodes:
            raise ValueError(f"job {job_id} has no node in this graph")
        return nodes


def _node_position(index: ProblemIndex, op_id: str) -> int:
    p = index.pos.get(op_id)
    if p is None:
        raise ValueError(f"{op_id} is not a scheduled op of the problem")
    return p


def _chosen_option(op: OpData, machine_id: str) -> OpOption:
    for option in op.options:
        if option.machine_id == machine_id:
            return option
    raise ValueError(f"{op.id} has no eligible option on {machine_id}")


def _job_arcs(
    index: ProblemIndex, options: Mapping[int, OpOption], n: int
) -> tuple[list[tuple[_JobArc, ...]], list[tuple[_JobArc, ...]]]:
    """Incoming and outgoing job arcs per position, between nodes only."""
    arcs_in: list[list[_JobArc]] = [[] for _ in range(n)]
    arcs_out: list[list[_JobArc]] = [[] for _ in range(n)]
    for v, v_option in options.items():
        for link in index.ops[v].predecessors:
            u_option = options.get(link.pred_pos)
            if u_option is None:
                continue
            arc = _arc(link.pred_pos, v, u_option, v_option, link.min_lag, link.overlap)
            arcs_in[v].append(arc)
            arcs_out[link.pred_pos].append(arc)
    return [tuple(a) for a in arcs_in], [tuple(a) for a in arcs_out]


def _arc(
    u: int, v: int, u_option: OpOption, v_option: OpOption, min_lag: float, q: float | None
) -> _JobArc:
    if q is None:
        return _JobArc(u, v, min_lag, None)
    transfer = u_option.fixed_seconds + q * u_option.unit_seconds + min_lag
    return _JobArc(u, v, transfer, q * v_option.unit_seconds)
