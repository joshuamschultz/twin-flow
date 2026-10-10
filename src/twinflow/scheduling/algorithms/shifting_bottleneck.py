"""Shifting bottleneck for makespan and weighted tardiness (COMP-013, REQ-099..REQ-101, D-077).

SDD pseudo-code "Shifting bottleneck, makespan" (Pinedo Alg 7.2.1, p.189-195) and
"Shifting bottleneck, weighted tardiness" (Sec 7.3, p.196-202), on work centers (p.212):

1. Every op starts on its shortest-run machine (ties: machine id), the provisional choice
   that gives the graph its durations; in-progress ops stay first on their machines.
2. While a center is open: build the disjunctive graph G of the fixed centers, and for each
   open center solve its subproblem with r(o) = head(o), delayed precedence a -> b with
   delay L(a, b) - dur(a) for every G path between two of its ops (p.194-196), and
   - makespan: d(o) = Cmax(G) - q(o) + dur(o), q the tail to V; Lmax subproblem;
   - weighted tardiness: d^k(o) = max(d_k, C_k) - q_k(o) + dur(o) for each job sink V_k
     that o reaches (p.198: a job already late in G is charged only for added delay);
     multi-due ATC subproblem.
   One-machine centers use the single-machine solvers, multi-machine centers the
   parallel-machine version, which also picks each op's machine (`sb_subproblems`).
3. Criticality: the subproblem value for makespan and for `subproblem_value`; for
   `completion_delta` (default) sum_k w_k (C'_k - C_k) exp(-(d_k - C'_k)^+ / Kc) with C'
   the job completions after inserting the center (p.199-200), Kc default pbar of the
   center. The most critical center (ties: center id) is fixed.
4. Re-optimization: `reopt_rounds` passes over the other fixed centers in insertion order;
   each is removed, re-solved and kept when Cmax (weighted tardiness of G for `sb_wt`) does
   not increase.
5. The machine choice and sequences go through `kernel.decode.decode` (calendars,
   operators and batches are the decoder's job).

Params (`SolveContext.params`): `K` (ATC look-ahead, default 2.0), `Kc` (default pbar of
the center), `criticality`, `reopt_rounds` (default 1), `subproblem` (the exact
branch-and-bound limit in ops, default 10). A bad value raises ValueError.

Deadline: the method is one pass with no anytime plan, like the dispatch rules. Past
`context.deadline` before every center is fixed it returns UNKNOWN `time_limit`; a deadline
hit during re-optimization ends re-optimization and keeps the plan built so far.

The subproblem solvers live in the helper module `sb_subproblems`, imported at call time
(D-069 leaf rule): when it is absent the solvers answer UNKNOWN `subsolver_unavailable`.
"""

from __future__ import annotations

import functools
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from twinflow.scheduling.kernel.decode import decode
from twinflow.scheduling.kernel.graph import DisjunctiveGraph
from twinflow.scheduling.kernel.index import OpOption, ProblemIndex
from twinflow.scheduling.kernel.score import fast_values
from twinflow.scheduling.model import ObjectiveSpec, Problem, Schedule, ScheduleIssue
from twinflow.scheduling.objectives import primary_kind
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature, ParamValue, SolveContext

if TYPE_CHECKING:
    from twinflow.scheduling.algorithms.sb_subproblems import CenterResult, DelayedPrecedence

Mode = Literal["makespan", "weighted_tardiness"]
Criticality = Literal["completion_delta", "subproblem_value"]

_SUBPROBLEMS = "twinflow.scheduling.algorithms.sb_subproblems"
_CRITICALITIES: Mapping[str, Criticality] = {
    "completion_delta": "completion_delta",
    "subproblem_value": "subproblem_value",
}
_EPS = 1e-9
"""Float slack of the re-optimization test (a tie keeps the new arcs)."""


@dataclass(frozen=True, slots=True)
class SbSubproblem:
    """One open center's subproblem in one iteration: sequences, value and criticality."""

    sequences: Mapping[str, tuple[str, ...]]
    value: float
    criticality: float


@dataclass(frozen=True, slots=True)
class SbIteration:
    """The center fixed in one main iteration and every open center's subproblem."""

    chosen: str
    subproblems: Mapping[str, SbSubproblem]


@dataclass(frozen=True, slots=True)
class SbOutcome:
    """Op -> machine and machine -> op ids (the decoder's input), plus the trace."""

    choice: Mapping[str, str]
    sequences: Mapping[str, tuple[str, ...]]
    trace: tuple[SbIteration, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Settings:
    k: float
    kc: float | None
    criticality: Criticality
    reopt_rounds: int
    exact_limit: int

    @classmethod
    def parse(cls, params: Mapping[str, ParamValue]) -> _Settings:
        raw = params.get("criticality", "completion_delta")
        criticality = _CRITICALITIES.get(raw) if isinstance(raw, str) else None
        if criticality is None:
            raise ValueError(f"criticality must be one of {sorted(_CRITICALITIES)}, not {raw!r}")
        kc = params.get("Kc")
        return cls(
            k=_positive(params.get("K", 2.0), "K"),
            kc=None if kc is None else _positive(kc, "Kc"),
            criticality=criticality,
            reopt_rounds=_count(params.get("reopt_rounds", 1), "reopt_rounds"),
            exact_limit=_count(params.get("subproblem", 10), "subproblem"),
        )


def _positive(value: ParamValue, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        raise ValueError(f"{name} must be a positive number, not {value!r}")
    return float(value)


def _count(value: ParamValue, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, not {value!r}")
    return value


def shifting_bottleneck(
    index: ProblemIndex,
    mode: Mode,
    params: Mapping[str, ParamValue],
    *,
    deadline: float = math.inf,
) -> SbOutcome:
    """Run shifting bottleneck on `index` (see the module). `deadline` is a monotonic
    instant; past it before every center is fixed, TimeoutError."""
    return _ShiftingBottleneck(index, mode, _Settings.parse(params), deadline).run()


@dataclass(frozen=True, slots=True)
class _GraphView:
    """What the subproblems read from one graph: heads, tails, completions."""

    graph: DisjunctiveGraph
    heads: Mapping[str, float]
    tails: Mapping[str, float]
    job_tails: Mapping[str, Mapping[str, float]]
    completions: Mapping[str, float]

    @property
    def cmax(self) -> float:
        return max(self.completions.values(), default=0.0)


class _ShiftingBottleneck:
    """The main loop and re-optimization over mutable choice and sequences."""

    def __init__(
        self, index: ProblemIndex, mode: Mode, settings: _Settings, deadline: float
    ) -> None:
        self._index = index
        self._mode: Mode = mode
        self._settings = settings
        self._deadline = deadline
        self._choice = {op.id: _shortest_option(op.options) for op in index.ops}
        self._prefix: dict[str, tuple[str, ...]] = {}
        self._center_ops: dict[str, list[str]] = {}
        for op in index.ops:
            if op.in_progress is not None:
                machine_id = self._choice[op.id]
                self._prefix[machine_id] = (*self._prefix.get(machine_id, ()), op.id)
            else:
                self._center_ops.setdefault(op.work_center_id, []).append(op.id)
        self._center_machines = {
            center: tuple(sorted({o.machine_id for op_id in ops for o in index.op(op_id).options}))
            for center, ops in self._center_ops.items()
        }
        self._sequences: dict[str, tuple[str, ...]] = dict(self._prefix)

    def run(self) -> SbOutcome:
        open_centers = sorted(self._center_ops)
        fixed: list[str] = []
        trace: list[SbIteration] = []
        while open_centers:
            view = self._view()
            subproblems = {c: self._subproblem(c, view) for c in open_centers}
            chosen = min(open_centers, key=lambda c: (-subproblems[c].criticality, c))
            self._insert(chosen, subproblems[chosen].sequences)
            trace.append(SbIteration(chosen, subproblems))
            open_centers.remove(chosen)
            self._reoptimize(fixed)
            fixed.append(chosen)
        return SbOutcome(dict(self._choice), dict(self._sequences), tuple(trace))

    # --- one center ------------------------------------------------------------------------

    def _subproblem(self, center: str, view: _GraphView) -> SbSubproblem:
        if time.monotonic() >= self._deadline:
            raise TimeoutError("shifting bottleneck ran out of time")
        result = self._solve(center, view)
        if self._mode == "makespan" or self._settings.criticality == "subproblem_value":
            criticality = result.value
        else:
            criticality = self._completion_delta(center, result, view)
        return SbSubproblem(dict(result.sequences), result.value, criticality)

    def _solve(self, center: str, view: _GraphView) -> CenterResult:
        """The center's subproblem from the current graph (heads, due dates, dp)."""
        from twinflow.scheduling.algorithms.sb_subproblems import (  # noqa: PLC0415  # leaf rule
            CenterOp,
            solve_center_parallel,
        )

        op_ids = self._center_ops[center]
        duration = {o: self._duration(o) for o in op_ids}
        ops = [
            CenterOp(
                id=o,
                release=view.heads[o],
                runs={opt.machine_id: opt.run for opt in self._index.op(o).options},
                due=view.cmax - view.tails[o] + duration[o]
                if self._mode == "makespan"
                else math.inf,
                dues=self._local_dues(o, duration[o], view) if self._mode != "makespan" else (),
            )
            for o in op_ids
        ]
        return solve_center_parallel(
            ops,
            self._center_machines[center],
            self._delayed_precedence(op_ids, duration, view.graph),
            setup=self._index.setups.seconds,
            objective="lmax" if self._mode == "makespan" else "weighted_tardiness",
            K=self._settings.k,
            exact_limit=self._settings.exact_limit,
        )

    def _duration(self, op_id: str) -> float:
        """dur(o) in the graph while o's center is open: setup from the machine state + run."""
        machine_id = self._choice[op_id]
        run = next(o.run for o in self._index.op(op_id).options if o.machine_id == machine_id)
        return self._index.setups.seconds(machine_id, None, op_id) + run

    def _local_dues(
        self, op_id: str, duration: float, view: _GraphView
    ) -> tuple[tuple[float, float], ...]:
        """(w_k, max(d_k, C_k) - q_k(o) + dur(o)) for every job sink V_k the op reaches.

        Pinedo p.198: a job already late in G is charged only for the delay this center
        adds, so its local due is measured from C_k, not d_k.
        """
        return tuple(
            (
                job.weight,
                max(job.due, view.completions[job.id]) - view.job_tails[job.id][op_id] + duration,
            )
            for job in self._index.jobs
            if job.due is not None and op_id in view.job_tails[job.id]
        )

    @staticmethod
    def _delayed_precedence(
        op_ids: Sequence[str], duration: Mapping[str, float], graph: DisjunctiveGraph
    ) -> list[DelayedPrecedence]:
        """a -> b with delay L(a, b) - dur(a) for every graph path between center ops."""
        from twinflow.scheduling.algorithms.sb_subproblems import (  # noqa: PLC0415  # leaf rule
            DelayedPrecedence,
        )

        dp: list[DelayedPrecedence] = []
        for a in op_ids:
            for b in op_ids:
                length = None if a == b else graph.longest_path(a, b)
                if length is not None:
                    dp.append(DelayedPrecedence(a, b, length - duration[a]))
        return dp

    def _completion_delta(self, center: str, result: CenterResult, view: _GraphView) -> float:
        """sum_k w_k (C'_k - C_k) exp(-(d_k - C'_k)^+ / Kc) after inserting the center."""
        saved = self._snapshot(center)
        self._insert(center, result.sequences)
        after = self._graph().completions()
        self._restore(center, saved)
        kc = self._settings.kc if self._settings.kc is not None else self._pbar(center)
        total = 0.0
        for job in self._index.jobs:
            if job.due is None or job.id not in after:
                continue
            delta = after[job.id] - view.completions[job.id]
            total += job.weight * delta * math.exp(-max(job.due - after[job.id], 0.0) / kc)
        return total

    def _pbar(self, center: str) -> float:
        """Integer part of the mean shortest run of the center's ops, at least 1 (p.199)."""
        runs = [
            min(o.run for o in self._index.op(op_id).options) for op_id in self._center_ops[center]
        ]
        return max(float(int(sum(runs) / len(runs))), 1.0)

    # --- graph state -----------------------------------------------------------------------

    def _graph(self) -> DisjunctiveGraph:
        return DisjunctiveGraph.build(self._index, self._choice, self._sequences)

    def _view(self) -> _GraphView:
        graph = self._graph()
        job_tails = (
            {}
            if self._mode == "makespan"
            else {job.id: graph.tails(job.id) for job in self._index.jobs if job.due is not None}
        )
        return _GraphView(graph, graph.heads(), graph.tails(), job_tails, graph.completions())

    def _objective(self) -> float:
        """Cmax of G, or its weighted tardiness for `sb_wt`."""
        completions = self._graph().completions()
        if self._mode == "makespan":
            return max(completions.values(), default=0.0)
        return sum(
            job.weight * max(completions[job.id] - job.due, 0.0)
            for job in self._index.jobs
            if job.due is not None and job.id in completions
        )

    def _insert(self, center: str, sequences: Mapping[str, tuple[str, ...]]) -> None:
        """The center's machine arcs: in-progress prefix, then the subproblem's sequence."""
        for machine_id in self._center_machines[center]:
            sequence = sequences.get(machine_id, ())
            for op_id in sequence:
                self._choice[op_id] = machine_id
            self._set_sequence(machine_id, (*self._prefix.get(machine_id, ()), *sequence))

    def _remove(self, center: str) -> None:
        for machine_id in self._center_machines[center]:
            self._set_sequence(machine_id, self._prefix.get(machine_id, ()))

    def _set_sequence(self, machine_id: str, sequence: tuple[str, ...]) -> None:
        if sequence:
            self._sequences[machine_id] = sequence
        else:
            self._sequences.pop(machine_id, None)

    def _snapshot(self, center: str) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
        choice = {o: self._choice[o] for o in self._center_ops[center]}
        machines = self._center_machines[center]
        return choice, {m: self._sequences[m] for m in machines if m in self._sequences}

    def _restore(
        self, center: str, saved: tuple[dict[str, str], dict[str, tuple[str, ...]]]
    ) -> None:
        choice, sequences = saved
        self._choice.update(choice)
        for machine_id in self._center_machines[center]:
            self._set_sequence(machine_id, sequences.get(machine_id, ()))

    # --- re-optimization (p.193, p.399) ----------------------------------------------------

    def _reoptimize(self, centers: Sequence[str]) -> None:
        """Re-solve each center in `centers`; keep it when the objective does not rise."""
        for _ in range(self._settings.reopt_rounds):
            for center in centers:
                if time.monotonic() >= self._deadline:
                    return
                self._reoptimize_one(center)

    def _reoptimize_one(self, center: str) -> None:
        before = self._objective()
        saved = self._snapshot(center)
        self._remove(center)
        result = self._solve(center, self._view())
        self._insert(center, result.sequences)
        if self._objective() > before + _EPS:
            self._restore(center, saved)


def _shortest_option(options: Sequence[OpOption]) -> str:
    """The provisional machine: shortest run, then machine id (options are id-ordered)."""
    return min(options, key=lambda o: o.run).machine_id


# --- solver ------------------------------------------------------------------------------------


def _issue(code: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path="solver", message=message)


class ShiftingBottleneckSolver:
    """`sb_makespan` / `sb_wt` behind the `Solver` seam: run, decode, return FEASIBLE."""

    __slots__ = ("_mode", "_name")

    def __init__(self, name: str, mode: Mode) -> None:
        self._name = name
        self._mode: Mode = mode

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> frozenset[Feature]:
        """Every feature: the decoder places the final plan."""
        return frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        """One shifting-bottleneck pass with `context.params`, decoded and scored."""
        started = time.perf_counter()
        index = ProblemIndex.build(problem)
        try:
            outcome = shifting_bottleneck(
                index, self._mode, context.params, deadline=context.deadline
            )
        except TimeoutError:
            return self._unknown(
                _issue("time_limit", "shifting bottleneck ran out of time"), started
            )
        except ModuleNotFoundError as error:
            if error.name != _SUBPROBLEMS:
                raise
            return self._unknown(
                _issue("subsolver_unavailable", f"{_SUBPROBLEMS} is not installed"), started
            )
        decoded = decode(index, outcome.choice, outcome.sequences)
        if isinstance(decoded, ScheduleIssue):
            return self._unknown(decoded, started)
        values = fast_values(index, decoded, context.previous_plan, context.churn_tolerance)
        return decoded.schedule(self._name).model_copy(
            update={
                "primary_value": values[primary_kind(objective)],
                "elapsed_seconds": time.perf_counter() - started,
            }
        )

    def _unknown(self, issue: ScheduleIssue, started: float) -> Schedule:
        return Schedule(
            status="UNKNOWN",
            assignments=(),
            solver=self._name,
            elapsed_seconds=time.perf_counter() - started,
            issues=(issue,),
        )


_MODES: Mapping[str, Mode] = {"sb_makespan": "makespan", "sb_wt": "weighted_tardiness"}

SOLVERS = tuple(
    SolverSpec(
        name=name,
        family="decomposition",
        factory=functools.partial(ShiftingBottleneckSolver, name, mode),
        deterministic=True,
        capabilities=frozenset(Feature),
    )
    for name, mode in _MODES.items()
)
"""Registry entries (D-069)."""
