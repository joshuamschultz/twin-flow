"""Shifting bottleneck subproblem solvers (COMP-013, REQ-099, REQ-100, REQ-101, D-077).

Pure functions over plain data (no Problem, no index), shared by shifting bottleneck,
five-phase and the tests. A work center's subproblem is a set of ops with release dates,
due dates and delayed precedence (Pinedo p.194-196: `after` starts no earlier than `delay`
after `before` completes; the caller derives `delay` from a graph path).

Timing of a sequence is semi-active: start = max(release, the machine's previous
completion, completion(before) + delay for every dp into the op); completion = start +
setup(previous op or None for the machine state, op) + run (the setup is attached).

- `lmax_exact`: depth-first branch and bound for 1|r_j,s_jk,dp|Lmax; the bound is
  preemptive EDD over the open ops (setups and open dp ignored, both only delay).
- `lmax_schrage`: non-delay EDD over released, dp-ready ops; `lmax_schrage_swaps` adds
  first-improvement adjacent swaps; `solve_lmax` is exact up to `exact_limit` ops (SDD
  COMP-013: 10).
- `wt_atc_sequence`: 1|r_j,s_jk,dp|sum w (C - d^k)^+ dispatched by the multi-due ATC index
  (Pinedo p.199) whenever the machine frees, times the SDD setup factor exp(-s /
  setup_scale) with s the setup after the machine's previous op (`setup_scale` = K2 sbar;
  the default inf gives factor 1, the book's index).
- `solve_center_parallel`: the Pm|r_j,s_jk,M_j,dp| version of a multi-machine center
  (p.212), non-delay at t = the earliest possible start: for Lmax the ready op with the
  earliest due goes to the eligible machine that completes it first; for weighted
  tardiness the (op, machine) pair with the largest index (ATC with that machine's run,
  times the setup factor) among released ops and machines free at t (ties: earlier
  finish, op id, machine id). One machine reduces to the single-machine solvers.

Ties always break by op id, then machine id, so every result is deterministic.

A helper module of `algorithms/` (D-069): it registers no solver (`SOLVERS = ()`);
`shifting_bottleneck.py` imports it at call time, so deleting it degrades that solver to
`subsolver_unavailable` instead of breaking discovery.
"""

from __future__ import annotations

import functools
import heapq
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from twinflow.scheduling.registry import SolverSpec

Setup = Callable[[str | None, str], float]
"""Setup seconds on one machine: (previous op id or None for the machine state, op id)."""

CenterSetup = Callable[[str, str | None, str], float]
"""Setup seconds on a center: (machine id, previous op id or None, op id)."""

Dues = tuple[tuple[float, float], ...]
"""(weight, local due) pairs of the weighted-tardiness subproblem, one per reachable sink."""

CenterObjective = Literal["lmax", "weighted_tardiness"]

DEFAULT_EXACT_LIMIT = 10
"""Largest one-machine subproblem solved by branch and bound (SDD COMP-013)."""

SOLVERS: tuple[SolverSpec, ...] = ()
"""A helper module: no registry entries (D-069)."""


class DelayedPrecedenceViolation(ValueError):
    """A sequence puts an op ahead of an op it must follow (it would close a cycle)."""


@dataclass(frozen=True, slots=True)
class SubOp:
    """One op of a one-machine subproblem: `due` for Lmax, `dues` for weighted tardiness."""

    id: str
    release: float
    run: float
    due: float = math.inf
    dues: Dues = ()


@dataclass(frozen=True, slots=True)
class CenterOp:
    """One op of a multi-machine center: `runs` maps each eligible machine to its run."""

    id: str
    release: float
    runs: Mapping[str, float]
    due: float = math.inf
    dues: Dues = ()


@dataclass(frozen=True, slots=True)
class DelayedPrecedence:
    """`after` starts no earlier than `delay` after `before` completes."""

    before: str
    after: str
    delay: float


@dataclass(frozen=True, slots=True)
class SubResult:
    """A one-machine sequence and its value (Lmax or weighted tardiness)."""

    sequence: tuple[str, ...]
    value: float


@dataclass(frozen=True, slots=True)
class CenterResult:
    """Machine id -> op sequence (machines without ops left out) and the value."""

    sequences: Mapping[str, tuple[str, ...]]
    value: float


_Gates = Mapping[str, tuple[tuple[str, float], ...]]
"""Op id -> (before, delay) of every delayed precedence into it."""


# --- timing ------------------------------------------------------------------------------------


def _gates(
    dp: Sequence[DelayedPrecedence], ids: Mapping[str, object]
) -> dict[str, tuple[tuple[str, float], ...]]:
    incoming: dict[str, list[tuple[str, float]]] = {}
    for c in dp:
        if c.before not in ids or c.after not in ids:
            raise ValueError(f"delayed precedence {c.before} -> {c.after} names an unknown op")
        incoming.setdefault(c.after, []).append((c.before, c.delay))
    return {op_id: tuple(pairs) for op_id, pairs in incoming.items()}


def _earliest(op_id: str, release: float, done: Mapping[str, float], gates: _Gates) -> float:
    """Release and dp gates of an op whose dp predecessors are all done."""
    return max([release, *(done[before] + delay for before, delay in gates.get(op_id, ()))])


def _single_timeline(
    by_id: Mapping[str, SubOp], sequence: Sequence[str], gates: _Gates, setup: Setup | None
) -> dict[str, float]:
    """Completions of `sequence`; `DelayedPrecedenceViolation` when it breaks a dp."""
    done: dict[str, float] = {}
    free, previous = -math.inf, None
    for op_id in sequence:
        for before, _ in gates.get(op_id, ()):
            if before not in done:
                raise DelayedPrecedenceViolation(f"{op_id} is sequenced before {before}")
        op = by_id[op_id]
        start = max(free, _earliest(op_id, op.release, done, gates))
        free = start + (setup(previous, op_id) if setup else 0.0) + op.run
        done[op_id], previous = free, op_id
    return done


def _lmax(ops: Sequence[SubOp] | Sequence[CenterOp], done: Mapping[str, float]) -> float:
    return max((done[o.id] - o.due for o in ops), default=-math.inf)


def _weighted_tardiness(
    ops: Sequence[SubOp] | Sequence[CenterOp], done: Mapping[str, float]
) -> float:
    return sum(w * max(done[o.id] - d, 0.0) for o in ops for w, d in o.dues)


def sequence_lmax(
    ops: Sequence[SubOp],
    sequence: Sequence[str],
    dp: Sequence[DelayedPrecedence],
    *,
    setup: Setup | None = None,
) -> float:
    """Lmax of `sequence`; `DelayedPrecedenceViolation` when it breaks a dp, ValueError when
    it is not a permutation of `ops`."""
    by_id = {o.id: o for o in ops}
    if sorted(sequence) != sorted(by_id):
        raise ValueError("the sequence is not a permutation of the subproblem's ops")
    return _lmax(ops, _single_timeline(by_id, sequence, _gates(dp, by_id), setup))


class _Ready:
    """Which ops have every dp predecessor placed, and their completions so far."""

    def __init__(self, ids: Sequence[str], gates: _Gates) -> None:
        self.done: dict[str, float] = {}
        self._waiting = {op_id: len(gates.get(op_id, ())) for op_id in ids}
        self._followers: dict[str, list[str]] = {}
        for after, pairs in gates.items():
            for before, _ in pairs:
                self._followers.setdefault(before, []).append(after)
        self._ready = sorted(op_id for op_id, n in self._waiting.items() if n == 0)

    def ops(self) -> list[str]:
        """Ready ops in id order."""
        return self._ready

    def remaining(self) -> bool:
        return bool(self._ready)

    def place(self, op_id: str, completion: float) -> None:
        self.done[op_id] = completion
        self._ready.remove(op_id)
        for after in self._followers.get(op_id, ()):
            self._waiting[after] -= 1
            if self._waiting[after] == 0:
                self._ready.append(after)
        self._ready.sort()


# --- 1|r_j,dp|Lmax -----------------------------------------------------------------------------


def _preemptive_edd_lmax(open_ops: Sequence[tuple[float, float, float]], start: float) -> float:
    """Lmax of preemptive EDD over (release, run, due) from `start`: a lower bound."""
    pending = sorted(open_ops)
    heap: list[tuple[float, float]] = []
    t, k, worst = start, 0, -math.inf
    while k < len(pending) or heap:
        if not heap:
            t = max(t, pending[k][0])
        while k < len(pending) and pending[k][0] <= t:
            heapq.heappush(heap, (pending[k][2], pending[k][1]))
            k += 1
        due, left = heapq.heappop(heap)
        next_release = pending[k][0] if k < len(pending) else math.inf
        if t + left <= next_release:
            t += left
            worst = max(worst, t - due)
        else:
            heapq.heappush(heap, (due, left - (next_release - t)))
            t = next_release
    return worst


class _LmaxSearch:
    """Depth-first branch and bound over sequences, EDD-first, strict improvement only."""

    def __init__(self, ops: Sequence[SubOp], gates: _Gates, setup: Setup | None) -> None:
        self._order = sorted(ops, key=lambda o: (o.due, o.release, o.id))
        self._gates = gates
        self._setup = setup
        self._sequence: list[str] = []
        self._done: dict[str, float] = {}
        self.best: SubResult | None = None

    def run(self) -> SubResult:
        self._branch(-math.inf, None, -math.inf)
        if self.best is None:
            raise DelayedPrecedenceViolation("the delayed precedences close a cycle")
        return self.best

    def _bound_value(self) -> float:
        return math.inf if self.best is None else self.best.value

    def _ready(self, op: SubOp) -> bool:
        return op.id not in self._done and all(
            before in self._done for before, _ in self._gates.get(op.id, ())
        )

    def _branch(self, free: float, previous: str | None, lateness: float) -> None:
        if len(self._sequence) == len(self._order):
            if lateness < self._bound_value():
                self.best = SubResult(tuple(self._sequence), lateness)
            return
        for op in self._order:
            if not self._ready(op):
                continue
            start = max(free, _earliest(op.id, op.release, self._done, self._gates))
            finish = start + (self._setup(previous, op.id) if self._setup else 0.0) + op.run
            late = max(lateness, finish - op.due)
            if late >= self._bound_value():
                continue
            self._done[op.id] = finish
            self._sequence.append(op.id)
            if max(late, self._lower_bound(finish)) < self._bound_value():
                self._branch(finish, op.id, late)
            self._sequence.pop()
            del self._done[op.id]

    def _lower_bound(self, free: float) -> float:
        open_ops = [
            (self._open_release(op), op.run, op.due)
            for op in self._order
            if op.id not in self._done
        ]
        return _preemptive_edd_lmax(open_ops, free) if open_ops else -math.inf

    def _open_release(self, op: SubOp) -> float:
        """Release raised by the dp gates of already placed predecessors."""
        gates = [
            self._done[before] + delay
            for before, delay in self._gates.get(op.id, ())
            if before in self._done
        ]
        return max([op.release, *gates])


def lmax_exact(
    ops: Sequence[SubOp], dp: Sequence[DelayedPrecedence], *, setup: Setup | None = None
) -> SubResult:
    """An optimal sequence for 1|r_j,s_jk,dp|Lmax (branch and bound, see the module)."""
    by_id = {o.id: o for o in ops}
    if not ops:
        return SubResult((), -math.inf)
    return _LmaxSearch(ops, _gates(dp, by_id), setup).run()


def lmax_schrage(
    ops: Sequence[SubOp], dp: Sequence[DelayedPrecedence], *, setup: Setup | None = None
) -> SubResult:
    """Schrage: whenever the machine frees, the released dp-ready op with the earliest due."""
    by_id = {o.id: o for o in ops}
    gates = _gates(dp, by_id)
    ready = _Ready(list(by_id), gates)
    sequence: list[str] = []
    free, previous = -math.inf, None
    while ready.remaining():
        earliest = {o: _earliest(o, by_id[o].release, ready.done, gates) for o in ready.ops()}
        t = max(free, min(earliest.values()))
        op_id = min((o for o in earliest if earliest[o] <= t), key=lambda o: (by_id[o].due, o))
        free = max(t, earliest[op_id]) + (setup(previous, op_id) if setup else 0.0)
        free += by_id[op_id].run
        ready.place(op_id, free)
        sequence.append(op_id)
        previous = op_id
    return SubResult(tuple(sequence), _lmax(ops, ready.done))


def lmax_schrage_swaps(
    ops: Sequence[SubOp], dp: Sequence[DelayedPrecedence], *, setup: Setup | None = None
) -> SubResult:
    """Schrage, then adjacent swaps kept while they strictly lower Lmax."""
    by_id = {o.id: o for o in ops}
    gates = _gates(dp, by_id)
    forced = {(c.before, c.after) for c in dp}
    start = lmax_schrage(ops, dp, setup=setup)
    sequence, best = list(start.sequence), start.value
    improved = True
    while improved:
        improved = False
        for k in range(len(sequence) - 1):
            if (sequence[k], sequence[k + 1]) in forced:
                continue
            sequence[k], sequence[k + 1] = sequence[k + 1], sequence[k]
            value = _lmax(ops, _single_timeline(by_id, sequence, gates, setup))
            if value < best:
                best, improved = value, True
            else:
                sequence[k], sequence[k + 1] = sequence[k + 1], sequence[k]
    return SubResult(tuple(sequence), best)


def solve_lmax(
    ops: Sequence[SubOp],
    dp: Sequence[DelayedPrecedence],
    *,
    setup: Setup | None = None,
    exact_limit: int = DEFAULT_EXACT_LIMIT,
) -> SubResult:
    """Exact up to `exact_limit` ops, Schrage plus adjacent swaps beyond."""
    if len(ops) <= exact_limit:
        return lmax_exact(ops, dp, setup=setup)
    return lmax_schrage_swaps(ops, dp, setup=setup)


# --- 1|r_j,dp|sum w (C - d^k)^+ ----------------------------------------------------------------


def _default_pbar(runs: Sequence[float]) -> float:
    """Integer part of the mean run (p.199), at least 1 so the index scale is never 0."""
    return max(float(int(sum(runs) / len(runs))), 1.0) if runs else 1.0


def _atc(run: float, release: float, dues: Dues, t: float, scale: float) -> float:
    if not dues:
        return 0.0
    if run <= 0.0:
        return math.inf
    wait = max(release - t, 0.0)
    return sum((w / run) * math.exp(-max(d - run + wait, 0.0) / scale) for w, d in dues)


def multi_due_atc_index(op: SubOp, t: float, *, pbar: float, K: float) -> float:  # noqa: N803
    """sum over (w, d) in `op.dues` of (w / p) exp(-(d - p + (r - t)^+)^+ / (K pbar)), p.199."""
    if K <= 0.0 or pbar <= 0.0:
        raise ValueError("K and pbar must be positive")
    return _atc(op.run, op.release, op.dues, t, K * pbar)


def wt_atc_sequence(
    ops: Sequence[SubOp],
    dp: Sequence[DelayedPrecedence],
    *,
    K: float,  # noqa: N803  # Pinedo's name for the look-ahead parameter
    pbar: float | None = None,
    setup: Setup | None = None,
    setup_scale: float = math.inf,
) -> SubResult:
    """Dispatch by the multi-due ATC index at t = the earliest time the machine is usable.

    Every dp-ready op competes (an unreleased one through its (r - t)^+ term); its release
    includes its dp gates. Ties go to the smaller op id.
    """
    if K <= 0.0:
        raise ValueError("K must be positive")
    by_id = {o.id: o for o in ops}
    gates = _gates(dp, by_id)
    scale = K * (_default_pbar([o.run for o in ops]) if pbar is None else pbar)
    ready = _Ready(list(by_id), gates)
    sequence: list[str] = []
    free, previous = -math.inf, None
    while ready.remaining():
        earliest = {o: _earliest(o, by_id[o].release, ready.done, gates) for o in ready.ops()}
        t = max(free, min(earliest.values()))
        op_id = _most_urgent(
            {
                o: _atc(by_id[o].run, earliest[o], by_id[o].dues, t, scale)
                * _setup_factor(setup(previous, o) if setup else 0.0, setup_scale)
                for o in earliest
            }
        )
        free = max(t, earliest[op_id]) + (setup(previous, op_id) if setup else 0.0)
        free += by_id[op_id].run
        ready.place(op_id, free)
        sequence.append(op_id)
        previous = op_id
    return SubResult(tuple(sequence), _weighted_tardiness(ops, ready.done))


def _setup_factor(seconds: float, setup_scale: float) -> float:
    """exp(-s / (K2 sbar)), the ATCS setup factor; 1 for an infinite scale."""
    return math.exp(-seconds / setup_scale) if setup_scale < math.inf else 1.0


def _most_urgent(index: Mapping[str, float]) -> str:
    """The op with the largest index; the smaller id on a tie."""
    return min(index, key=lambda o: (-index[o], o))


# --- parallel-machine center (p.212) -----------------------------------------------------------


def solve_center_parallel(
    ops: Sequence[CenterOp],
    machines: Sequence[str],
    dp: Sequence[DelayedPrecedence],
    *,
    setup: CenterSetup | None = None,
    objective: CenterObjective = "lmax",
    K: float = 2.0,  # noqa: N803  # Pinedo's name for the look-ahead parameter
    exact_limit: int = DEFAULT_EXACT_LIMIT,
    setup_scale: float = math.inf,
) -> CenterResult:
    """Sequence a center's ops on its machines (see the module); one machine reduces to
    `solve_lmax` or `wt_atc_sequence`. ValueError for an op with no machine in `machines`."""
    for op in ops:
        if not any(m in op.runs for m in machines):
            raise ValueError(f"{op.id} has no eligible machine in {list(machines)}")
    if len(machines) == 1:
        return _one_machine(ops, machines[0], dp, setup, objective, K, exact_limit, setup_scale)
    if K <= 0.0:
        raise ValueError("K must be positive")
    return _CenterDispatch(ops, machines, dp, setup, objective, K, setup_scale).run()


def _one_machine(
    ops: Sequence[CenterOp],
    machine_id: str,
    dp: Sequence[DelayedPrecedence],
    setup: CenterSetup | None,
    objective: CenterObjective,
    K: float,  # noqa: N803
    exact_limit: int,
    setup_scale: float,
) -> CenterResult:
    single = [SubOp(o.id, o.release, o.runs[machine_id], o.due, o.dues) for o in ops]

    on_machine = None if setup is None else functools.partial(setup, machine_id)
    if objective == "lmax":
        result = solve_lmax(single, dp, setup=on_machine, exact_limit=exact_limit)
    else:
        result = wt_atc_sequence(single, dp, K=K, setup=on_machine, setup_scale=setup_scale)
    sequences = {machine_id: result.sequence} if result.sequence else {}
    return CenterResult(sequences, result.value)


class _CenterDispatch:
    """Non-delay list scheduling of a center over its machines (see the module).

    An op's dp gate is fixed once its last dp predecessor is placed, so it is computed
    then; each machine keeps a heap of its ready ops by gate and the set of those already
    released, so a decision looks only at the released ops of the machines free at t. A
    released op's urgency does not change with t (its (r - t)^+ term is 0), so it is
    computed once per (op, machine); only the setup factor follows the machine's last op.
    """

    def __init__(
        self,
        ops: Sequence[CenterOp],
        machines: Sequence[str],
        dp: Sequence[DelayedPrecedence],
        setup: CenterSetup | None,
        objective: CenterObjective,
        K: float,  # noqa: N803
        setup_scale: float,
    ) -> None:
        self._ops = ops
        self._by_id = {o.id: o for o in ops}
        self._eligible = {o.id: [m for m in machines if m in o.runs] for o in ops}
        self._gates = _gates(dp, self._by_id)
        self._setup = setup
        self._objective = objective
        shortest = [min(o.runs[m] for m in self._eligible[o.id]) for o in ops]
        self._scale = K * _default_pbar(shortest)
        self._setup_scale = setup_scale
        self._free = dict.fromkeys(machines, -math.inf)
        self._last: dict[str, str | None] = dict.fromkeys(machines, None)
        self._sequences: dict[str, list[str]] = {m: [] for m in machines}
        self._done: dict[str, float] = {}
        self._gate: dict[str, float] = {}
        self._pending: dict[str, list[tuple[float, str]]] = {m: [] for m in machines}
        self._released: dict[str, set[str]] = {m: set() for m in machines}
        self._released_by_gate: dict[str, list[tuple[float, str]]] = {m: [] for m in machines}
        self._urgency: dict[tuple[str, str], float] = {}
        self._waiting = {o.id: len(self._gates.get(o.id, ())) for o in ops}
        self._followers: dict[str, list[str]] = {}
        for after, pairs in self._gates.items():
            for before, _ in pairs:
                self._followers.setdefault(before, []).append(after)

    def run(self) -> CenterResult:
        for op in self._ops:
            if self._waiting[op.id] == 0:
                self._make_ready(op.id)
        for _ in self._ops:
            t = self._next_time()
            op_id, machine_id = self._pick(t)
            finish = self._finish(op_id, machine_id)
            self._free[machine_id], self._last[machine_id] = finish, op_id
            self._sequences[machine_id].append(op_id)
            self._place(op_id, finish)
        sequences = {m: tuple(seq) for m, seq in self._sequences.items() if seq}
        if self._objective == "lmax":
            return CenterResult(sequences, _lmax(self._ops, self._done))
        return CenterResult(sequences, _weighted_tardiness(self._ops, self._done))

    # --- readiness -----------------------------------------------------------------------

    def _make_ready(self, op_id: str) -> None:
        gate = _earliest(op_id, self._by_id[op_id].release, self._done, self._gates)
        self._gate[op_id] = gate
        for m in self._eligible[op_id]:
            heapq.heappush(self._pending[m], (gate, op_id))

    def _place(self, op_id: str, completion: float) -> None:
        self._done[op_id] = completion
        for m in self._eligible[op_id]:
            self._released[m].discard(op_id)
        for after in self._followers.get(op_id, ()):
            self._waiting[after] -= 1
            if self._waiting[after] == 0:
                self._make_ready(after)

    def _smallest_gate(self, heap: list[tuple[float, str]]) -> float:
        """The smallest gate in a machine heap, placed ops dropped first."""
        while heap and heap[0][1] in self._done:
            heapq.heappop(heap)
        return heap[0][0] if heap else math.inf

    def _next_time(self) -> float:
        """t = the earliest start of any ready op; releases every op gated by t.

        No ready op left while some op is unplaced means the dp close a cycle.
        """
        t = math.inf
        for m, free in self._free.items():
            gate = min(
                self._smallest_gate(self._released_by_gate[m]),
                self._smallest_gate(self._pending[m]),
            )
            t = min(t, max(free, gate))
        for m, pending in self._pending.items():
            while pending and pending[0][0] <= t:
                entry = heapq.heappop(pending)
                if entry[1] not in self._done:
                    self._released[m].add(entry[1])
                    heapq.heappush(self._released_by_gate[m], entry)
        if t == math.inf:
            raise DelayedPrecedenceViolation("the delayed precedences close a cycle")
        return t

    # --- the decision --------------------------------------------------------------------

    def _pick(self, t: float) -> tuple[str, str]:
        """(op, machine): EDD then the fastest machine for Lmax; for weighted tardiness the
        released (op, machine) pair on a machine free by t with the largest ATCS index."""
        pairs = [
            (o, m)
            for m, released in self._released.items()
            if self._free[m] <= t
            for o in released
            if self._gate[o] <= t
        ]
        if self._objective == "lmax":
            op_id = min((o for o, _ in pairs), key=lambda o: (self._by_id[o].due, o))
            return op_id, self._fastest_machine(op_id)
        index = {pair: self._pair_index(*pair) for pair in pairs}
        finish = {pair: self._finish(*pair) for pair in index}
        return min(index, key=lambda pair: (-index[pair], finish[pair], pair))

    def _pair_index(self, op_id: str, machine_id: str) -> float:
        key = (op_id, machine_id)
        urgency = self._urgency.get(key)
        if urgency is None:
            op = self._by_id[op_id]
            urgency = _atc(op.runs[machine_id], -math.inf, op.dues, 0.0, self._scale)
            self._urgency[key] = urgency
        return urgency * _setup_factor(self._setup_seconds(op_id, machine_id), self._setup_scale)

    def _setup_seconds(self, op_id: str, machine_id: str) -> float:
        return self._setup(machine_id, self._last[machine_id], op_id) if self._setup else 0.0

    def _finish(self, op_id: str, machine_id: str) -> float:
        start = max(self._gate[op_id], self._free[machine_id])
        return start + self._setup_seconds(op_id, machine_id) + self._by_id[op_id].runs[machine_id]

    def _fastest_machine(self, op_id: str) -> str:
        """The eligible machine that completes the op first (smaller id on a tie)."""
        finish = {m: self._finish(op_id, m) for m in self._eligible[op_id]}
        return min(finish, key=lambda m: (finish[m], m))
