"""Five-phase bottleneck procedure for flexible lines (COMP-014, REQ-102; Pinedo Sec 16.4).

SDD pseudo-code "Five-phase flexible line" (p.443-447):

1. Bottleneck (`bottleneck_center`): the work center with the largest
   (sum of shortest runs + n * sbar) / (available seconds of its machines up to the horizon)
   over its pending ops (`kernel.stats.InstanceStats`: the numerator is m * Cmax_hat).
   Available seconds count each machine's calendar from max(now, available_from) to
   `horizon_end`, or without a horizon to now + the sum of every op's shortest run
   [mine: the serial bound, so every center is measured over the same window]. Ties: the
   smallest center id.
2. Windows (`bottleneck_windows`): per op of the center, r_b = max(op release, job release
   + phi * shortest runs of the job's earlier ops) and d_b = d_j - phi * shortest runs of its
   later ops (`OpData.local_due`, None without a due); phi is `index.phi` (default 2).
3. Buckets by eligible machine set (p.447) [mine]: realised by the flexibility factor of
   phase 4, so an op with fewer eligible machines of the center ranks first.
4. The bottleneck by ATCS on the windows, as a parallel-machine list schedule on plain
   numbers: the machine with the smallest t = max(free, smallest r_b of its pending ops)
   decides (ties: machine id) and takes the op with r_b <= t of highest
   (w / p_i) exp(-max(d_b - p_i - t, 0) / (K1 pbar)) exp(-s_i(l, o) / (K2 sbar)) flex(o),
   K1 and K2 by Pinedo p.376 from the center's statistics (clamped as in `atcs`), then K2
   times `k2_scale` (default 6, tuned on the flow-line training seeds 0-19: a weaker setup
   factor than the single-stage formula), flex = 1 + 0.5 (1 - eligible / m); an op without
   d_b ranks after every op with one (as ATCS); ties by op id. Faster machines get larger
   ops through p_i(o). In-progress ops stay first on their machines. The times are
   estimates that order the bottleneck and anchor phase 5.
5. Every op gets a key from its job's bottleneck op: before it the bottleneck start
   (upstream), the bottleneck op its own start, after it the bottleneck completion
   (downstream); in a job with no bottleneck op the key is the op's own window release
   [mine]. The dispatch engine (non-delay, `kernel.dispatch`) places every op with the
   earliest key first: a bottleneck op only on its phase-4 machine, every other op by the
   engine's preferred-unless-late policy (D-072) [mine: the engine keeps the key order
   among ready ops without forcing idle time on a machine]. Then `setup_swaps`: adjacent
   swaps on non-bottleneck machines that lower the machine's total setup and do not raise
   the plan's weighted tardiness (decoded), until no swap is taken or the deadline passes.

The plan goes through `kernel.decode.decode`.

Phase 4 does not call `atcs` through the registry: ATCS dispatches a whole problem, so the
windows would need a compiled sub-problem (jobs, calendars, pools, in-progress state copied
and remapped); a list schedule on (r_b, d_b, p_i, s_i) is a few plain loops, and the
engine and the decoder place the final plan under every rule anyway.

Params (`SolveContext.params`): `phi` (non-negative), `k2_scale` (positive). An invalid or
unknown param gives UNKNOWN with an `invalid_method_param` issue.

Deadline: phases 1-4 are one polynomial pass; the engine returns `time_limit` past
`context.deadline`; the swap pass stops at half the budget left after the engine, so the
final decode and verification still fit (a swap pass cut short leaves fewer swaps, so on a
large instance the output can depend on the machine's speed). A leaf of `algorithms/`
(D-069): it registers through `SOLVERS` only.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from twinflow.scheduling.calendar_math import work_seconds
from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.dispatch import (
    Dispatched,
    DispatchEngine,
    DispatchState,
    PreferredUnlessLate,
    Priority,
)
from twinflow.scheduling.kernel.index import DEFAULT_PHI, ProblemIndex
from twinflow.scheduling.kernel.score import fast_values
from twinflow.scheduling.kernel.stats import InstanceStats
from twinflow.scheduling.model import ObjectiveKind, ObjectiveSpec, Problem, Schedule, ScheduleIssue
from twinflow.scheduling.objectives import JobOutcome, job_terms, primary_kind
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature, ParamValue, SolveContext

NAME = "five_phase"
K1_RANGE = (0.5, 10.0)
K2_RANGE = (0.05, 10.0)
FLEXIBILITY_BETA = 0.5
K2_SCALE = 6.0
"""Default `k2_scale` (tuned on the flow-line training seeds 0-19, D-083)."""
SWAP_BUDGET_SHARE = 0.5
"""Share of the budget left after phases 1-4 that the swap pass may use."""

_EPS = 1e-9
Window = tuple[float, float | None]


# --- phase 1: bottleneck -----------------------------------------------------------------------


def bottleneck_center(index: ProblemIndex) -> str:
    """The center with the largest load over available machine seconds (module docstring)."""
    horizon = _horizon(index)
    best: tuple[float, str] | None = None
    for center_id, machines in sorted(index.center_machines.items()):
        if not machines:
            continue
        stats = InstanceStats.for_center(index, center_id, index.now)
        ratio = _ratio(stats.cmax_hat * stats.m, _available(index, machines, horizon))
        if best is None or ratio > best[0]:
            best = (ratio, center_id)
    if best is None:
        raise ValueError("no work center has a machine")
    return best[1]


def _horizon(index: ProblemIndex) -> float:
    if index.horizon_end is not None:
        return index.horizon_end
    return index.now + math.fsum(min(o.run for o in op.options) for op in index.ops)


def _available(index: ProblemIndex, machines: Sequence[str], horizon: float) -> float:
    total = 0.0
    for machine_id in machines:
        machine = index.machines[index.machine_pos[machine_id]]
        start = max(index.now, machine.available_from)
        if horizon > start:
            total += work_seconds(machine.calendar, start, horizon)
    return total


def _ratio(load: float, capacity: float) -> float:
    if capacity > 0:
        return load / capacity
    return math.inf if load > 0 else 0.0


# --- phase 2: windows --------------------------------------------------------------------------


def bottleneck_windows(index: ProblemIndex, center_id: str) -> dict[str, Window]:
    """(r_b, d_b) per op with an option on a machine of the center (module docstring)."""
    machines = set(index.machines_of(center_id))
    releases = _window_releases(index)
    return {
        op.id: (releases[p], op.local_due)
        for p, op in enumerate(index.ops)
        if any(o.machine_id in machines for o in op.options)
    }


def _window_releases(index: ProblemIndex) -> list[float]:
    """Per op position: max(op release, job release + phi * shortest runs before it)."""
    releases = [0.0] * len(index.ops)
    for job in index.jobs:
        job_release = index.ops[job.ops[0]].release
        upstream = 0.0
        for p in job.ops:
            op = index.ops[p]
            releases[p] = max(op.release, job_release + index.phi * upstream)
            upstream += min(o.run for o in op.options)
    return releases


# --- phase 4: the bottleneck by ATCS on the windows --------------------------------------------


@dataclass(frozen=True, slots=True)
class _Bottleneck:
    """Phase 4's machine choice and estimated start / completion per op."""

    choice: dict[str, str]
    starts: dict[str, float]
    ends: dict[str, float]


@dataclass(frozen=True, slots=True)
class _Factors:
    """The center's ATCS divisors as reciprocals (0 = factor 1) and its machine count."""

    m: int
    inv_urgency: float
    inv_setup: float


def _atcs_factors(index: ProblemIndex, center_id: str, k2_scale: float) -> _Factors:
    """K1, K2 by Pinedo p.376 (clamped as `atcs` does), K2 times `k2_scale`; setup factor 1
    without setups."""
    stats = InstanceStats.for_center(index, center_id, index.now)
    k1 = 4.5 + stats.R if stats.R <= 0.5 else 6.0 - 2.0 * stats.R
    k2 = stats.tau / (2.0 * math.sqrt(stats.eta)) if stats.eta > 0 else K2_RANGE[1]
    k1, k2 = _clamp(k1, K1_RANGE), _clamp(k2, K2_RANGE)
    setup_on = stats.sbar > 0 and stats.eta > 0
    return _Factors(
        m=stats.m,
        inv_urgency=_reciprocal(k1 * stats.pbar),
        inv_setup=_reciprocal(k2_scale * k2 * stats.sbar) if setup_on else 0.0,
    )


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(max(value, bounds[0]), bounds[1])


def _reciprocal(value: float) -> float:
    return 1.0 / value if value > 0 else 0.0


class _BottleneckScheduler:
    """The phase-4 list schedule over the bottleneck machines (module docstring)."""

    def __init__(
        self,
        index: ProblemIndex,
        center_id: str,
        windows: Mapping[str, Window],
        k2_scale: float = K2_SCALE,
    ) -> None:
        self._index = index
        self._windows = windows
        self._machines = set(index.machines_of(center_id))
        self._factors = _atcs_factors(index, center_id, k2_scale)
        self._free = {
            m: max(index.now, index.machines[index.machine_pos[m]].available_from)
            for m in self._machines
        }
        self._last: dict[str, str | None] = dict.fromkeys(self._machines)
        self._choice: dict[str, str] = {}
        self._starts: dict[str, float] = {}
        self._ends: dict[str, float] = {}

    def run(self) -> _Bottleneck:
        pending = self._start_in_progress()
        queues = {
            m: sorted((self._windows[o][0], o) for o in pending if self._eligible(o, m))
            for m in self._machines
        }
        while any(queues.values()):
            machine_id, t = self._next_machine(queues)
            op_id = self._pick(queues[machine_id], machine_id, t)
            self._place(op_id, machine_id, t)
            for option in self._index.op(op_id).options:
                queue = queues.get(option.machine_id)
                if queue is not None:
                    queue.remove((self._windows[op_id][0], op_id))
        return _Bottleneck(
            choice=self._choice,
            starts=self._starts,
            ends=self._ends,
        )

    def _eligible(self, op_id: str, machine_id: str) -> bool:
        return any(o.machine_id == machine_id for o in self._index.op(op_id).options)

    def _start_in_progress(self) -> list[str]:
        """In-progress ops by id at now on their machine; returns the other ops by id."""
        pending = []
        for op_id in sorted(self._windows):
            op = self._index.op(op_id)
            if op.in_progress is None:
                pending.append(op_id)
                continue
            machine_id = op.options[0].machine_id
            remaining = op.in_progress.remaining_seconds or 0.0
            self._record(op_id, machine_id, self._index.now, self._index.now + remaining)
        return pending

    def _next_machine(self, queues: Mapping[str, list[tuple[float, str]]]) -> tuple[str, float]:
        """The machine with the smallest decision time t (ties: machine id), and t."""
        times = [
            (max(self._free[m], queue[0][0]), m) for m, queue in sorted(queues.items()) if queue
        ]
        t, machine_id = min(times)
        return machine_id, t

    def _pick(self, queue: list[tuple[float, str]], machine_id: str, t: float) -> str:
        best: tuple[tuple[float, float], str] | None = None
        for release, op_id in sorted(queue, key=lambda item: item[1]):
            if release > t + _EPS:
                continue
            priority = self._priority(op_id, machine_id, t)
            if best is None or priority > best[0]:
                best = (priority, op_id)
        if best is None:  # the queue head defines t, so it is always a candidate
            raise AssertionError("no bottleneck op ready at its decision time")
        return best[1]

    def _priority(self, op_id: str, machine_id: str, t: float) -> tuple[float, float]:
        index, factors = self._index, self._factors
        op = index.op(op_id)
        run = next(o.run for o in op.options if o.machine_id == machine_id)
        value = math.log(index.jobs[op.job_pos].weight) - (math.log(run) if run > 0 else -math.inf)
        value -= index.setups.seconds(machine_id, self._last[machine_id], op_id) * factors.inv_setup
        eligible = sum(1 for o in op.options if o.machine_id in self._machines)
        value += math.log(1.0 + FLEXIBILITY_BETA * (1.0 - eligible / factors.m))
        due = self._windows[op_id][1]
        if due is None:
            return (0.0, value)
        return (1.0, value - max(due - run - t, 0.0) * factors.inv_urgency)

    def _place(self, op_id: str, machine_id: str, t: float) -> None:
        op = self._index.op(op_id)
        run = next(o.run for o in op.options if o.machine_id == machine_id)
        setup = self._index.setups.seconds(machine_id, self._last[machine_id], op_id)
        self._record(op_id, machine_id, t, t + setup + run)

    def _record(self, op_id: str, machine_id: str, start: float, end: float) -> None:
        self._choice[op_id] = machine_id
        self._starts[op_id], self._ends[op_id] = start, end
        self._free[machine_id] = max(self._free[machine_id], end)
        self._last[machine_id] = op_id


# --- phase 5: propagation up and down stream ---------------------------------------------------


def _keys(index: ProblemIndex, bottleneck: _Bottleneck) -> list[float]:
    """Per op position: the phase-5 key (module docstring); in-progress ops -inf."""
    releases = _window_releases(index)
    keys = [0.0] * len(index.ops)
    for job in index.jobs:
        first = next(
            (index.op_ids[p] for p in job.ops if index.op_ids[p] in bottleneck.starts), None
        )
        anchor = None if first is None else bottleneck.starts[first]
        for p in job.ops:
            op_id = index.op_ids[p]
            if op_id in bottleneck.starts:
                keys[p] = bottleneck.starts[op_id]
                anchor = bottleneck.ends[op_id]
            else:
                keys[p] = releases[p] if anchor is None else anchor
            if index.ops[p].in_progress is not None:
                keys[p] = -math.inf
    return keys


class _KeyRule:
    """Earliest phase-5 key first."""

    def __init__(self, keys: Sequence[float]) -> None:
        self._keys = keys

    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        return -self._keys[state.index.pos[op_id]]


@dataclass(frozen=True, slots=True)
class _PhaseFourMachines:
    """Bottleneck ops on their phase-4 machine; every other op by `PreferredUnlessLate`."""

    machine_of: Mapping[int, int]
    default: PreferredUnlessLate

    def accepted(self, state: DispatchState, p: int) -> tuple[int, ...]:
        machine = self.machine_of.get(p)
        return self.default.accepted(state, p) if machine is None else (machine,)


def _dispatch_by_keys(
    index: ProblemIndex, bottleneck: _Bottleneck, keys: Sequence[float], deadline: float
) -> Dispatched | ScheduleIssue:
    """Phase 5's placement: the engine with the keys as priority (module docstring)."""
    policy = _PhaseFourMachines(
        machine_of={index.pos[o]: index.machine_pos[m] for o, m in bottleneck.choice.items()},
        default=PreferredUnlessLate(),
    )
    return DispatchEngine(index, _KeyRule(keys), policy=policy).run(deadline=deadline)


# --- phase 5: setup swaps ----------------------------------------------------------------------


def setup_swaps(
    index: ProblemIndex,
    choice: Mapping[str, str],
    sequences: Mapping[str, Sequence[str]],
    bottleneck: str,
    *,
    deadline: float | None = None,
) -> dict[str, tuple[str, ...]]:
    """Adjacent swaps on non-bottleneck machines that cut setup without raising weighted
    tardiness (module docstring); stops at `deadline` (monotonic). Input not decoding is
    returned unchanged."""
    plan = {m: tuple(seq) for m, seq in sequences.items()}
    decoded = decode(index, choice, plan)
    if isinstance(decoded, ScheduleIssue):
        return plan
    best = weighted_tardiness(index, decoded)
    frozen = set(index.machines_of(bottleneck))
    for machine_id in sorted(plan):
        if machine_id in frozen:
            continue
        improved = True
        while improved:
            improved = False
            for k in range(len(plan[machine_id]) - 1):
                if deadline is not None and time.monotonic() > deadline:
                    return plan
                value = _try_swap(index, choice, plan, machine_id, k, best)
                if value is not None:
                    best, improved = value, True
    return plan


def _try_swap(
    index: ProblemIndex,
    choice: Mapping[str, str],
    plan: dict[str, tuple[str, ...]],
    machine_id: str,
    k: int,
    best: float,
) -> float | None:
    """Apply swap k in place when it cuts setup and keeps weighted tardiness; its new value."""
    sequence = plan[machine_id]
    if index.op(sequence[k]).in_progress is not None:
        return None
    swapped = (*sequence[:k], sequence[k + 1], sequence[k], *sequence[k + 2 :])
    if (
        _local_setup(index, machine_id, swapped, k)
        >= _local_setup(index, machine_id, sequence, k) - _EPS
    ):
        return None
    trial = plan | {machine_id: swapped}
    decoded = decode(index, choice, trial)
    if isinstance(decoded, ScheduleIssue):
        return None
    value = weighted_tardiness(index, decoded)
    if value > best:
        return None
    plan[machine_id] = swapped
    return value


def _local_setup(index: ProblemIndex, machine_id: str, sequence: Sequence[str], k: int) -> float:
    """Setup seconds into positions k, k + 1 and k + 2: the ones swap k can change."""
    total = 0.0
    for q in range(k, min(k + 3, len(sequence))):
        previous = sequence[q - 1] if q > 0 else None
        total += index.setups.seconds(machine_id, previous, sequence[q])
    return total


def weighted_tardiness(index: ProblemIndex, decoded: Decoded) -> float:
    """Weighted tardiness of a decoded plan (the `objectives` formula)."""
    ends = decoded.ends
    outcomes = [
        JobOutcome(weight=job.weight, completion=max(ends[p] for p in job.ops), due=job.due)
        for job in index.jobs
    ]
    return job_terms(outcomes, index.now)[ObjectiveKind.WEIGHTED_TARDINESS]


# --- the solver --------------------------------------------------------------------------------


class InvalidParamError(ValueError):
    """A method param with an unknown name or an invalid value; the message names it."""


@dataclass(frozen=True, slots=True)
class _Settings:
    """Validated `SolveContext.params` of one solve."""

    phi: float
    k2_scale: float

    @classmethod
    def from_params(cls, params: Mapping[str, ParamValue]) -> _Settings:
        unknown = sorted(set(params) - {"phi", "k2_scale"})
        if unknown:
            raise InvalidParamError(f"unknown param {unknown[0]}")
        phi = _number(params, "phi", DEFAULT_PHI)
        k2_scale = _number(params, "k2_scale", K2_SCALE)
        if phi < 0:
            raise InvalidParamError("phi must not be negative")
        if k2_scale <= 0:
            raise InvalidParamError("k2_scale must be positive")
        return cls(phi=phi, k2_scale=k2_scale)


def _number(params: Mapping[str, ParamValue], name: str, default: float) -> float:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise InvalidParamError(f"{name} must be a finite number")
    return float(value)


class FivePhaseSolver:
    """The five-phase procedure behind the `Solver` seam (COMP-014)."""

    __slots__ = ()

    @property
    def name(self) -> str:
        return NAME

    @property
    def capabilities(self) -> frozenset[Feature]:
        """Every feature: placement goes through the dispatch engine and the decoder."""
        return frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        """Phases 1-5, then decode; FEASIBLE with the primary value, else UNKNOWN."""
        started = time.perf_counter()
        try:
            settings = _Settings.from_params(context.params)
        except InvalidParamError as error:
            issue = ScheduleIssue(
                code="invalid_method_param",
                path=f"profile.method_params.{NAME}",
                message=str(error),
            )
            return _unknown(issue, started)
        index = ProblemIndex.build(problem, settings.phi)
        result = _five_phase(index, settings.k2_scale, context.deadline)
        if isinstance(result, ScheduleIssue):
            return _unknown(result, started)
        values = fast_values(index, result, context.previous_plan, context.churn_tolerance)
        return result.schedule(NAME).model_copy(
            update={
                "primary_value": values[primary_kind(objective)],
                "elapsed_seconds": time.perf_counter() - started,
            }
        )


def _five_phase(index: ProblemIndex, k2_scale: float, deadline: float) -> Decoded | ScheduleIssue:
    if not index.ops:
        return decode(index, {}, {m.id: () for m in index.machines})
    center_id = bottleneck_center(index)
    windows = bottleneck_windows(index, center_id)
    bottleneck = _BottleneckScheduler(index, center_id, windows, k2_scale).run()
    dispatched = _dispatch_by_keys(index, bottleneck, _keys(index, bottleneck), deadline)
    if isinstance(dispatched, ScheduleIssue):
        return dispatched
    choice = dispatched.choice
    swap_deadline = time.monotonic() + SWAP_BUDGET_SHARE * max(deadline - time.monotonic(), 0.0)
    sequences = setup_swaps(index, choice, dispatched.sequences, center_id, deadline=swap_deadline)
    return decode(index, choice, sequences)


def _unknown(issue: ScheduleIssue, started: float) -> Schedule:
    return Schedule(
        status="UNKNOWN",
        assignments=(),
        solver=NAME,
        elapsed_seconds=time.perf_counter() - started,
        issues=(issue,),
    )


SOLVERS = (
    SolverSpec(
        name=NAME,
        family="decomposition",
        factory=FivePhaseSolver,
        deterministic=True,
        capabilities=frozenset(Feature),
    ),
)
"""Registry entry (D-069)."""
