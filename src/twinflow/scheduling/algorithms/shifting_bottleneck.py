"""Shifting bottleneck for makespan and weighted tardiness (COMP-013, REQ-099..REQ-101, D-077).

SDD pseudo-code "Shifting bottleneck, makespan" (Pinedo Alg 7.2.1, p.189-195) and
"Shifting bottleneck, weighted tardiness" (Sec 7.3, p.196-202), on work centers (p.212):

1. Provisional machines: each op starts on its fastest option (run times the machine's
   calendar slowdown, see below; ties: machine id); in-progress ops stay first on their
   machines. Each multi-machine center then takes the machines its own subproblem picks on
   the job-arc graph (eligibility, speed and setups: `solve_center_parallel`).
2. While a center is open: build the disjunctive graph G of the fixed centers, and for each
   open center solve its subproblem with r(o) = head(o), delayed precedence a -> b with
   delay L(a, b) - dur(a) for the graph paths between two of its ops (p.194-196), and
   - makespan: d(o) = Cmax(G) - q(o) + dur(o), q the tail to V; Lmax subproblem;
   - weighted tardiness: d^k(o) = max(d_k, C_k) - q_k(o) + dur(o) for each job sink V_k
     that o reaches (p.198: a job already late in G is charged only for added delay);
     multi-due ATC subproblem with the setup factor exp(-s / (K2 sbar_c)) (SDD).
   One-machine centers use the single-machine solvers, multi-machine centers the
   parallel-machine version, which also picks each op's machine (`sb_subproblems`).
3. Criticality: the subproblem value for makespan and for `subproblem_value`; for
   `completion_delta` (default) sum_k w_k (C'_k - C_k) exp(-(d_k - C'_k)^+ / Kc) with C'
   the job completions after inserting the center (p.199-200), Kc default pbar of the
   center. The most critical center (ties: center id) is fixed.
4. Re-optimization: `reopt_rounds` passes over the other fixed centers in insertion order;
   each is removed, re-solved and kept when the objective does not rise: Cmax (weighted
   tardiness for `sb_wt`) of G while a center is open, the `evaluate` key of the complete
   plan once every center is fixed. A final round then re-optimizes every center (the
   last one fixed included) the same way.
5. The machine choice and sequences go through `kernel.decode.decode` (calendars,
   operators and batches are the decoder's job).

Readings [mine]:
- Delayed precedence: `DisjunctiveGraph.path_lengths` gives L for every pair of the
  center's ops in one pass; only the pairs of the transitive reduction are kept (a -> c is
  dropped when a -> b -> c within the center), so every path still orders the sequence and
  the subproblem stays near-linear in size.
- Calendar slowdown: in the center subproblem a machine's run is divided by the working
  share of its calendar span (1 without a calendar), so a machine that works half the time
  counts as half as fast; the graph keeps the plain runs.
- Cross-center options: work centers whose ops share an option machine (a substitute
  center's machine, `ProcessingOption.work_center_id`) form one subproblem keyed by the
  smallest center id, so every machine is sequenced by exactly one subproblem.
- `evaluate` (the solver passes its `Scorer` key over `fast_values` of the decoded plan)
  judges re-optimization by the solver's real objective, calendars included.

Params (`SolveContext.params`): `K` (ATC look-ahead, default 2.0), `K2` (setup factor,
default 1.0 for every center, tuned on the training bench), `Kc` (default pbar of the
center), `criticality`, `reopt_rounds` (default 2), `subproblem` (the exact
branch-and-bound limit in ops, default 10). A bad value raises ValueError.

Deadline: past `context.deadline` before every center is fixed, the open centers are each
fixed by their subproblem on the current graph in center-id order, with no criticality
comparison and no re-optimization (an anytime answer); a deadline hit during
re-optimization ends it and keeps the plan built so far.

The subproblem solvers live in the helper module `sb_subproblems`, imported at call time
(D-069 leaf rule): when it is absent the solvers answer UNKNOWN `subsolver_unavailable`.
"""

from __future__ import annotations

import functools
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal

import numpy as np
import numpy.typing as npt

from twinflow.scheduling.kernel.decode import decode
from twinflow.scheduling.kernel.graph import DisjunctiveGraph
from twinflow.scheduling.kernel.index import Calendar, OpData, ProblemIndex
from twinflow.scheduling.kernel.score import Scorer, fast_values
from twinflow.scheduling.kernel.stats import InstanceStats
from twinflow.scheduling.model import ObjectiveSpec, Problem, Schedule, ScheduleIssue
from twinflow.scheduling.objectives import primary_kind
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature, ParamValue, SolveContext

if TYPE_CHECKING:
    from twinflow.scheduling.algorithms.sb_subproblems import CenterResult, DelayedPrecedence

Mode = Literal["makespan", "weighted_tardiness"]
Criticality = Literal["completion_delta", "subproblem_value"]
Plan = Mapping[str, tuple[str, ...]]
Evaluate = Callable[[Mapping[str, str], Plan], tuple[float, ...]]
"""(op -> machine, machine -> op ids) of a complete plan -> sort key, smaller is better."""

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
    sequences: Plan
    trace: tuple[SbIteration, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Settings:
    k: float
    k2: float
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
            k2=_positive(params.get("K2", 1.0), "K2"),
            kc=None if kc is None else _positive(kc, "Kc"),
            criticality=criticality,
            reopt_rounds=_count(params.get("reopt_rounds", 2), "reopt_rounds"),
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
    evaluate: Evaluate | None = None,
) -> SbOutcome:
    """Run shifting bottleneck on `index` (see the module). `deadline` is a monotonic
    instant; `evaluate` scores complete plans for re-optimization (graph objective when
    None)."""
    return _ShiftingBottleneck(index, mode, _Settings.parse(params), deadline, evaluate).run()


@dataclass(frozen=True, slots=True)
class _GraphView:
    """What the subproblems read from one graph: heads, tails, completions, sink tails."""

    graph: DisjunctiveGraph
    heads: Mapping[str, float]
    tails: Mapping[str, float]
    completions: Mapping[str, float]
    sink_tails: npt.NDArray[np.float64]
    """Row per op position, column per job: q_k(o), -inf off-path (weighted tardiness)."""
    due_base: npt.NDArray[np.float64]
    """Per job max(d_k, C_k); nan for a job without a due date (weighted tardiness)."""

    @property
    def cmax(self) -> float:
        return max(self.completions.values(), default=0.0)


class _ShiftingBottleneck:
    """The main loop and re-optimization over mutable choice and sequences."""

    def __init__(
        self,
        index: ProblemIndex,
        mode: Mode,
        settings: _Settings,
        deadline: float,
        evaluate: Evaluate | None,
    ) -> None:
        self._index = index
        self._mode: Mode = mode
        self._settings = settings
        self._deadline = deadline
        self._evaluate = evaluate
        self._slowdown = {m.id: _calendar_slowdown(m.calendar) for m in index.machines}
        self._choice = {op.id: self._fastest_option(op.id) for op in index.ops}
        self._prefix: dict[str, tuple[str, ...]] = {}
        self._center_ops: dict[str, list[str]] = {}
        open_ops = []
        for op in index.ops:
            if op.in_progress is not None:
                machine_id = self._choice[op.id]
                self._prefix[machine_id] = (*self._prefix.get(machine_id, ()), op.id)
            else:
                open_ops.append(op)
        group = _center_groups(open_ops)
        for op in open_ops:
            self._center_ops.setdefault(group[op.work_center_id], []).append(op.id)
        self._center_machines = {
            center: tuple(sorted({o.machine_id for op_id in ops for o in index.op(op_id).options}))
            for center, ops in self._center_ops.items()
        }
        self._sequences: dict[str, tuple[str, ...]] = dict(self._prefix)
        self._open = set(self._center_ops)
        self._setup_scale = {c: self._center_setup_scale(c) for c in self._center_ops}

    def _fastest_option(self, op_id: str) -> str:
        """Shortest run times calendar slowdown, then machine id (options are id-ordered)."""
        options = self._index.op(op_id).options
        return min(options, key=lambda o: o.run * self._slowdown[o.machine_id]).machine_id

    def _center_setup_scale(self, center: str) -> float:
        """K2 sbar_c of the setup factor; inf (factor 1) for makespan or without setups."""
        if self._mode == "makespan":
            return math.inf
        sbar = InstanceStats.for_center(self._index, center, self._index.now).sbar
        return self._settings.k2 * sbar if sbar > 0.0 else math.inf

    def run(self) -> SbOutcome:
        self._choose_center_machines()
        fixed: list[str] = []
        trace: list[SbIteration] = []
        while self._open:
            if time.monotonic() >= self._deadline:
                trace += self._complete_open_centers()
                break
            view = self._view()
            subproblems = {c: self._subproblem(c, view) for c in sorted(self._open)}
            chosen = min(subproblems, key=lambda c: (-subproblems[c].criticality, c))
            self._fix(chosen, subproblems[chosen].sequences)
            trace.append(SbIteration(chosen, subproblems))
            self._reoptimize(fixed)
            fixed.append(chosen)
        self._reoptimize(fixed)
        return SbOutcome(dict(self._choice), dict(self._sequences), tuple(trace))

    def _choose_center_machines(self) -> None:
        """Each multi-machine center takes the machines of its subproblem on the job-arc
        graph (its arcs are not inserted)."""
        multi = [c for c in sorted(self._center_ops) if len(self._center_machines[c]) > 1]
        if not multi:
            return
        view = self._view()
        for center in multi:
            for machine_id, sequence in self._solve(center, view).sequences.items():
                self._choice.update(dict.fromkeys(sequence, machine_id))

    def _complete_open_centers(self) -> list[SbIteration]:
        """Past the deadline: fix each open center by its subproblem, in center-id order."""
        iterations: list[SbIteration] = []
        for center in sorted(self._open):
            result = self._solve(center, self._view())
            sub = SbSubproblem(dict(result.sequences), result.value, result.value)
            self._fix(center, sub.sequences)
            iterations.append(SbIteration(center, {center: sub}))
        return iterations

    def _fix(self, center: str, sequences: Plan) -> None:
        self._insert(center, sequences)
        self._open.discard(center)

    # --- one center ------------------------------------------------------------------------

    def _subproblem(self, center: str, view: _GraphView) -> SbSubproblem:
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
        makespan = self._mode == "makespan"
        ops = [
            CenterOp(
                id=o,
                release=view.heads[o],
                runs={
                    opt.machine_id: opt.run * self._slowdown[opt.machine_id]
                    for opt in self._index.op(o).options
                },
                due=view.cmax - view.tails[o] + duration[o] if makespan else math.inf,
                dues=() if makespan else self._local_dues(o, duration[o], view),
            )
            for o in op_ids
        ]
        return solve_center_parallel(
            ops,
            self._center_machines[center],
            _delayed_precedence(op_ids, duration, view.graph),
            setup=self._index.setups.seconds,
            objective="lmax" if makespan else "weighted_tardiness",
            K=self._settings.k,
            exact_limit=self._settings.exact_limit,
            setup_scale=self._setup_scale[center],
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
        local = view.due_base - view.sink_tails[self._index.pos[op_id]] + duration
        jobs = self._index.jobs
        return tuple((jobs[k].weight, float(local[k])) for k in np.flatnonzero(np.isfinite(local)))

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
        completions = graph.completions()
        heads, tails = graph.heads(), graph.tails()
        if self._mode == "makespan":
            empty = np.empty((0, 0))
            return _GraphView(graph, heads, tails, completions, empty, empty)
        due_base = np.array(
            [
                math.nan if job.due is None else max(job.due, completions.get(job.id, job.due))
                for job in self._index.jobs
            ]
        )
        sink_tails = graph.sink_tails(self._index.op_ids)
        return _GraphView(graph, heads, tails, completions, sink_tails, due_base)

    def _objective(self) -> tuple[float, ...]:
        """The `evaluate` key once every center is fixed, else Cmax of G (its weighted
        tardiness for `sb_wt`)."""
        if self._evaluate is not None and not self._open:
            return self._evaluate(self._choice, self._sequences)
        completions = self._graph().completions()
        if self._mode == "makespan":
            return (max(completions.values(), default=0.0),)
        return (
            sum(
                job.weight * max(completions[job.id] - job.due, 0.0)
                for job in self._index.jobs
                if job.due is not None and job.id in completions
            ),
        )

    def _insert(self, center: str, sequences: Plan) -> None:
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
        if _rises(self._objective(), before):
            self._restore(center, saved)


def _center_groups(ops: Sequence[OpData]) -> dict[str, str]:
    """Each op center's subproblem key: centers that share an option machine are one group.

    A cross-center option (`ProcessingOption.work_center_id`) puts a machine of another
    center among an op's options; both centers then sequence that machine, so they are
    solved as one subproblem keyed by the smallest center id. Without such options every
    center is its own group (Pinedo p.212 as before).
    """
    parent: dict[str, str] = {}

    def find(center: str) -> str:
        while parent.setdefault(center, center) != center:
            center = parent[center]
        return center

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    owner: dict[str, str] = {}
    for op in ops:
        find(op.work_center_id)
        for option in op.options:
            union(owner.setdefault(option.machine_id, op.work_center_id), op.work_center_id)
    return {center: find(center) for center in parent}


def _rises(after: tuple[float, ...], before: tuple[float, ...]) -> bool:
    """True when `after` sorts after `before` beyond float noise (a tie keeps the new arcs)."""
    for a, b in zip(after, before, strict=True):
        if abs(a - b) > _EPS:
            return a > b
    return False


def _delayed_precedence(
    op_ids: Sequence[str], duration: Mapping[str, float], graph: DisjunctiveGraph
) -> list[DelayedPrecedence]:
    """a -> b with delay L(a, b) - dur(a) for the graph paths between center ops, kept only
    on the transitive reduction among them (see the module)."""
    from twinflow.scheduling.algorithms.sb_subproblems import (  # noqa: PLC0415  # leaf rule
        DelayedPrecedence,
    )

    lengths = graph.path_lengths(op_ids)
    reach = np.isfinite(lengths)
    hops = reach.astype(np.float32)
    direct = reach & ((hops @ hops) == 0.0)
    return [
        DelayedPrecedence(op_ids[i], op_ids[j], float(lengths[i, j]) - duration[op_ids[i]])
        for i, j in zip(*np.nonzero(direct), strict=True)
    ]


def _calendar_slowdown(calendar: Calendar) -> float:
    """Span over working seconds of a calendar (first start to last end); 1 without one."""
    if not calendar:
        return 1.0
    working = math.fsum(end - start for start, end in calendar)
    span = max(end for _, end in calendar) - min(start for start, _ in calendar)
    return span / working if working > 0.0 else 1.0


# --- solver ------------------------------------------------------------------------------------


def _issue(code: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path="solver", message=message)


def _decoded_key(index: ProblemIndex, objective: ObjectiveSpec, context: SolveContext) -> Evaluate:
    """The solver's real objective of a plan: `Scorer` key over `fast_values` of its decode
    (infinity when it does not decode)."""
    scorer = Scorer(objective, None)

    def key(choice: Mapping[str, str], sequences: Plan) -> tuple[float, ...]:
        decoded = decode(index, choice, sequences)
        if isinstance(decoded, ScheduleIssue):
            return (math.inf,)
        values = fast_values(index, decoded, context.previous_plan, context.churn_tolerance)
        return scorer.key(values)

    return key


class ShiftingBottleneckSolver:
    """`sb_makespan` / `sb_wt` behind the `Solver` seam: run, decode, return FEASIBLE."""

    enforces_budget: ClassVar[bool] = True

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
                index,
                self._mode,
                context.params,
                deadline=context.deadline,
                evaluate=_decoded_key(index, objective, context),
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
