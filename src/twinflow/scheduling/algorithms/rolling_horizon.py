"""Rolling horizon: time-based decomposition into windows (COMP-015, REQ-103, REQ-125, D-069).

SDD pseudo-code "Rolling horizon" (Pinedo Sec 15.2, p.398-403). Registers `rolling_horizon`,
family `decomposition`, deterministic (its default window solver `atcs->tabu` is).

1. Cuts (`cuts`, a pure function on the `ProblemIndex`): jobs sorted by (due, id), jobs
   without a due last. A cut after position a is
   - `due_gap` when d_next - d_a >= gap * pbar_job, pbar_job the mean over jobs of their
     total shortest run (due-date clusters, Ex 15.2.2);
   - `load` when V(t) <= eps * capacity, t the first release later than job a's release,
     V(t) the released bottleneck work not yet processed just before t, capacity = the
     bottleneck's machine count times its mean shortest op run (Ex 15.2.1).
   A window shorter than `min_jobs` merges into the next (the last into the previous); a
   window longer than `max_jobs` splits into equal parts (reason `max_jobs`). A cut that
   would put a job before a cross-job predecessor is dropped.
2. Windows in order: window w's sub-problem holds its jobs plus every earlier window's ops
   as exact pins (machine, earliest = latest = the start); later windows' jobs are absent.
   Jobs with an in-progress op join the first window (they hold their machine at now).
   Each free op is ready no earlier than the last pinned end on every machine it may use
   (Pinedo's single-machine reading: a window follows the earlier ones): the dispatch
   engine places ops in time order and cannot leave room for a pinned op it has not
   placed yet, so without this a dispatch-based window solver would take pinned slots.
   The `window_solver` pipeline (`a->b`, registry names looked up at call time) runs on it
   through the `Solver` seam, starting from a floor (`_append_plan`, the warm start of
   the first stage); each stage's verified schedule warm-starts the next, the last
   verified one is kept and its assignments become the next window's pins.
3. Seams (Fig 15.3): for each pair of adjacent windows, the last `seam` jobs of the first
   and the first `seam` of the second are re-solved by `seam_solver` with every other op
   pinned; the result is kept when it verifies and its decode scores better.
4. The final plan is the decode of the per-machine sequences (D-071); should the decode
   fail, the last verified sub-problem schedule (valid for the whole problem: pins only
   restrict) is returned.
5. Single pass (DEV-25): with more than one window (and `whole_problem_floor`), one `atcs`
   pass over the whole problem (registry, when registered) runs first on one window's
   share of the time. Windows queue behind the earlier windows on every machine, so their
   plan can be far worse; when the single pass scores better it is returned with a
   `windows_lost_to_single_pass` issue, never silently.

Params (`SolveContext.params`; an unknown or invalid one gives UNKNOWN
`invalid_method_param`): `gap` (>= 0, default 1.0), `eps` (>= 0, default 0.25),
`min_jobs` (>= 1, default 20), `max_jobs` (>= min_jobs, default 150), `seam` (>= 0,
default 5; 0 = seams off), `window_solver` (default `atcs->tabu`), `seam_solver` (also a
pipeline; default `ortools` when registered and the seam sub-problem has at most
`SEAM_EXACT_OPS` ops, else `tabu`), `whole_problem_floor` (bool, default true: step 5;
false shows the decomposition alone). An unregistered window or seam solver gives UNKNOWN
`subsolver_unavailable`. CP-SAT refuses times with more than six decimals, so as a window
or seam solver it helps only when the pins come from CP-SAT (tick-aligned) plans.

Budget: 5 percent of the budget is kept for the final decode and verification; with seams
the windows share 80 percent of the rest and the seams 20 percent. Each window gets an
equal share of the time left for the windows (unused time flows to later windows).
"""

from __future__ import annotations

import bisect
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from twinflow.scheduling import registry
from twinflow.scheduling.kernel.decode import decode
from twinflow.scheduling.kernel.dispatch import Dispatched, dispatched_schedule
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.score import Scorer, Values, fast_values
from twinflow.scheduling.model import (
    Assignment,
    CompiledProblem,
    Job,
    ObjectiveSpec,
    Pin,
    Problem,
    Schedule,
    ScheduleIssue,
    SchedulingProfile,
)
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import (
    Feature,
    OptionalDependencyError,
    ParamValue,
    SolveContext,
    Solver,
)
from twinflow.scheduling.verifier import verify

log = logging.getLogger(__name__)

NAME = "rolling_horizon"
DEFAULT_GAP = 1.0
"""Due-gap threshold in mean job lengths [mine]; Ex 15.2.3 needs <= 8 / (57 / 9) = 1.26."""
DEFAULT_EPS = 0.25
DEFAULT_MIN_JOBS = 20
DEFAULT_MAX_JOBS = 150
DEFAULT_SEAM = 5
DEFAULT_WINDOW_SOLVER = "atcs->tabu"
EXACT_SEAM_SOLVER = "ortools"
FALLBACK_SEAM_SOLVER = "tabu"
WHOLE_PROBLEM_SOLVER = "atcs"
"""With more than one window, the plan is compared with one pass of this solver (DEV-25)."""
SEAM_EXACT_OPS = 60
"""A seam sub-problem with at most this many ops goes to CP-SAT by default [mine]."""
FINAL_SHARE = 0.05
"""Budget share kept for the final decode and verification."""
WINDOW_SHARE = 0.8
"""Share of the rest the windows get when seams run; the seams get the remainder."""
SOLUTION_STATUSES = frozenset({"OPTIMAL", "FEASIBLE"})
_KNOWN_PARAMS = frozenset(
    {
        "gap",
        "eps",
        "min_jobs",
        "max_jobs",
        "seam",
        "window_solver",
        "seam_solver",
        "whole_problem_floor",
    }
)

CutReason = Literal["due_gap", "load", "max_jobs"]


class InvalidParamError(ValueError):
    """A method param with an unknown name or an invalid value; the message names it."""


@dataclass(frozen=True, slots=True)
class Cut:
    """A window boundary after position `after` of the (due, id) job order."""

    after: int
    reason: CutReason


@dataclass(frozen=True, slots=True, kw_only=True)
class _Settings:
    """Validated `SolveContext.params` of one solve."""

    gap: float
    eps: float
    min_jobs: int
    max_jobs: int
    seam: int
    window_solver: tuple[str, ...]
    seam_solver: tuple[str, ...] | None
    whole_problem_floor: bool

    @classmethod
    def parse(cls, params: Mapping[str, ParamValue]) -> _Settings:
        unknown = sorted(set(params) - _KNOWN_PARAMS)
        if unknown:
            raise InvalidParamError(f"unknown param {unknown[0]}")
        min_jobs = _count(params, "min_jobs", DEFAULT_MIN_JOBS, 1)
        max_jobs = _count(params, "max_jobs", DEFAULT_MAX_JOBS, 1)
        if max_jobs < min_jobs:
            raise InvalidParamError("max_jobs must not be below min_jobs")
        seam_solver = params.get("seam_solver")
        return cls(
            gap=_non_negative(params, "gap", DEFAULT_GAP),
            eps=_non_negative(params, "eps", DEFAULT_EPS),
            min_jobs=min_jobs,
            max_jobs=max_jobs,
            seam=_count(params, "seam", DEFAULT_SEAM, 0),
            window_solver=_names(params.get("window_solver", DEFAULT_WINDOW_SOLVER)),
            seam_solver=None if seam_solver is None else _names(seam_solver),
            whole_problem_floor=_flag(params, "whole_problem_floor", True),
        )


def _non_negative(params: Mapping[str, ParamValue], name: str, default: float) -> float:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidParamError(f"{name} must be a number")
    if not (math.isfinite(value) and value >= 0):
        raise InvalidParamError(f"{name} must be finite and not negative")
    return float(value)


def _flag(params: Mapping[str, ParamValue], name: str, default: bool) -> bool:
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise InvalidParamError(f"{name} must be true or false")
    return value


def _count(params: Mapping[str, ParamValue], name: str, default: int, least: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < least:
        raise InvalidParamError(f"{name} must be an integer >= {least}")
    return value


def _names(value: ParamValue) -> tuple[str, ...]:
    """Registry names joined by `->`; an empty name is refused."""
    if not isinstance(value, str):
        raise InvalidParamError("a solver pipeline must be a string of names joined by ->")
    names = tuple(part.strip() for part in value.split("->"))
    if not all(names):
        raise InvalidParamError(f"solver pipeline {value!r} has an empty name")
    return names


# --- cuts (pure) -------------------------------------------------------------------------------


def cuts(index: ProblemIndex, params: Mapping[str, ParamValue]) -> tuple[Cut, ...]:
    """The window cuts of `index` under `params`, ascending (see the module docstring).

    Raises `InvalidParamError` for an invalid param.
    """
    return _cuts(index, _Settings.parse(params))


def _cuts(index: ProblemIndex, settings: _Settings) -> tuple[Cut, ...]:
    order = due_order(index)
    blocked = _blocked_positions(index, order)
    raw = [c for c in _raw_cuts(index, order, settings) if c.after not in blocked]
    merged = _merge_short(raw, len(order), settings.min_jobs)
    return _split_long(merged, len(order), settings.max_jobs, blocked)


def due_order(index: ProblemIndex) -> tuple[int, ...]:
    """Job positions sorted by (due, id); jobs without a due date last."""
    jobs = index.jobs
    return tuple(
        sorted(
            range(len(jobs)),
            key=lambda k: (math.inf if jobs[k].due is None else jobs[k].due, jobs[k].id),
        )
    )


def _shortest_run(index: ProblemIndex, p: int) -> float:
    return min((o.run for o in index.ops[p].options), default=0.0)


def _job_release(index: ProblemIndex, k: int) -> float:
    return min((index.ops[p].release for p in index.jobs[k].ops), default=index.now)


def _raw_cuts(index: ProblemIndex, order: Sequence[int], settings: _Settings) -> list[Cut]:
    jobs = index.jobs
    lengths = [sum(_shortest_run(index, p) for p in job.ops) for job in jobs]
    threshold = settings.gap * (sum(lengths) / len(lengths)) if lengths else 0.0
    load = _BottleneckLoad.build(index)
    found: list[Cut] = []
    for a in range(len(order) - 1):
        due_a, due_next = jobs[order[a]].due, jobs[order[a + 1]].due
        if due_a is not None and (due_next is None or due_next - due_a >= threshold):
            found.append(Cut(a, "due_gap"))
        elif load.cut_after(_job_release(index, order[a]), settings.eps):
            found.append(Cut(a, "load"))
    return found


@dataclass(frozen=True, slots=True)
class _BottleneckLoad:
    """A fluid queue of released bottleneck work served at the bottleneck's machine count."""

    releases: tuple[float, ...]
    """Distinct job releases, ascending."""
    backlog: tuple[float, ...]
    """Unprocessed work just after the arrivals at each release."""
    machines: int
    capacity: float

    @classmethod
    def build(cls, index: ProblemIndex) -> _BottleneckLoad:
        center = _bottleneck(index)
        if center is None:
            return cls((), (), 1, 0.0)
        machines = max(len(index.machines_of(center)), 1)
        arrivals: dict[float, float] = {}
        runs: list[float] = []
        for k, job in enumerate(index.jobs):
            work = [
                _shortest_run(index, p) for p in job.ops if index.ops[p].work_center_id == center
            ]
            runs += work
            release = _job_release(index, k)
            arrivals[release] = arrivals.get(release, 0.0) + sum(work)
        releases = tuple(sorted(arrivals))
        backlog: list[float] = []
        level, clock = 0.0, -math.inf
        for release in releases:
            level = max(0.0, level - machines * (release - clock)) if backlog else 0.0
            level += arrivals[release]
            backlog.append(level)
            clock = release
        capacity = machines * (sum(runs) / len(runs)) if runs else 0.0
        return cls(releases, tuple(backlog), machines, capacity)

    def cut_after(self, release: float, eps: float) -> bool:
        """V(t) <= eps * capacity at t, the first release later than `release`."""
        k = bisect.bisect_right(self.releases, release)
        if k == 0 or k == len(self.releases):
            return False
        t = self.releases[k]
        remaining = self.backlog[k - 1] - self.machines * (t - self.releases[k - 1])
        return max(0.0, remaining) <= eps * self.capacity


def _bottleneck(index: ProblemIndex) -> str | None:
    """The center with the most shortest-run work per machine (ties: center id)."""
    work: dict[str, float] = {}
    for p, op in enumerate(index.ops):
        work[op.work_center_id] = work.get(op.work_center_id, 0.0) + _shortest_run(index, p)
    if not work:
        return None
    return min(work, key=lambda c: (-work[c] / max(len(index.machines_of(c)), 1), c))


def _blocked_positions(index: ProblemIndex, order: Sequence[int]) -> frozenset[int]:
    """Cut positions that would put a job's cross-job predecessor in a later window."""
    rank = {k: r for r, k in enumerate(order)}
    blocked: set[int] = set()
    for op in index.ops:
        for link in op.predecessors:
            pred, succ = rank[index.ops[link.pred_pos].job_pos], rank[op.job_pos]
            blocked.update(range(succ, pred))
    return frozenset(blocked)


def _merge_short(raw: Sequence[Cut], jobs: int, min_jobs: int) -> list[Cut]:
    """Drop cuts so every window has `min_jobs` (the last window joins the previous)."""
    kept: list[Cut] = []
    start = 0
    for cut in raw:
        if cut.after - start + 1 >= min_jobs:
            kept.append(cut)
            start = cut.after + 1
    if kept and jobs - start < min_jobs:
        kept.pop()
    return kept


def _split_long(
    merged: Sequence[Cut], jobs: int, max_jobs: int, blocked: frozenset[int]
) -> tuple[Cut, ...]:
    """Split each window longer than `max_jobs` into equal parts."""
    result: list[Cut] = []
    start = 0
    for cut in (*merged, None):
        end = jobs - 1 if cut is None else cut.after
        size = end - start + 1
        parts = math.ceil(size / max_jobs)
        for k in range(1, parts):
            position = start + round(k * size / parts) - 1
            if position not in blocked:
                result.append(Cut(position, "max_jobs"))
        if cut is not None:
            result.append(cut)
        start = end + 1
    return tuple(result)


# --- sub-problems ------------------------------------------------------------------------------


def _windows(index: ProblemIndex, settings: _Settings) -> list[tuple[int, ...]]:
    """Job positions per window in (due, id) order; in-progress jobs join the first."""
    order = due_order(index)
    bounds = [-1, *(c.after for c in _cuts(index, settings)), len(order) - 1]
    windows = [order[a + 1 : b + 1] for a, b in zip(bounds, bounds[1:], strict=False)]
    windows = [w for w in windows if w]
    if len(windows) < 2:
        return windows
    running = {index.ops[p].job_pos for p in range(len(index.ops)) if index.ops[p].in_progress}
    moved = [k for w in windows[1:] for k in w if k in running]
    first = (*windows[0], *moved)
    rest = [tuple(k for k in w if k not in running) for w in windows[1:]]
    return [first, *(w for w in rest if w)]


def _sub_problem(
    problem: Problem,
    free: frozenset[str],
    pinned: Mapping[str, Assignment],
    scheduled: frozenset[str],
    *,
    append: bool,
) -> Problem:
    """Free jobs, jobs with pinned ops (exact pins) and every unscheduled job, in order.

    `append` (windows): each free op becomes ready no earlier than the last pinned end on
    every machine it may use, so it follows the pinned ops wherever it goes. The dispatch
    engine places ops in time order and cannot leave room for a pinned op it has not
    placed yet; this way no free op can take a pinned op's slot.
    """
    last_end: dict[str, float] = {}
    if append:
        for a in pinned.values():
            last_end[a.machine_id] = max(last_end.get(a.machine_id, -math.inf), a.end)
    jobs: list[Job] = []
    for job in problem.jobs:
        if job.id in free:
            jobs.append(_appended_job(job, last_end) if last_end else job)
        elif job.id not in scheduled:
            jobs.append(job)
        elif any(op.id in pinned for op in job.operations):
            jobs.append(_pinned_job(job, pinned))
    return problem.model_copy(update={"jobs": tuple(jobs)})


def _pinned_job(job: Job, pinned: Mapping[str, Assignment]) -> Job:
    operations = tuple(
        op.model_copy(update={"pin": _exact_pin(pinned[op.id])})
        if op.id in pinned and op.status.state == "pending"
        else op
        for op in job.operations
    )
    return job.model_copy(update={"operations": operations})


def _appended_job(job: Job, last_end: Mapping[str, float]) -> Job:
    operations = []
    for op in job.operations:
        ready = max((last_end.get(o.machine_id, -math.inf) for o in op.options), default=-math.inf)
        if op.status.state == "pending" and ready > op.ready_time:
            op = op.model_copy(update={"ready_time": ready})
        operations.append(op)
    return job.model_copy(update={"operations": tuple(operations)})


def _exact_pin(assignment: Assignment) -> Pin:
    return Pin(
        machine_id=assignment.machine_id,
        earliest_start=assignment.start,
        latest_start=assignment.start,
    )


def _append_plan(
    sub: Problem, pinned: Mapping[str, Assignment], rank: Mapping[str, int]
) -> Schedule | None:
    """The window's floor (Pinedo's time-based decomposition: a window follows the earlier
    ones): the pinned assignments as they are, then the free ops decoded on machines that
    start where their last pinned op ends, in-progress ops first, then in (due rank of their
    job, route position) order, each on its shortest option. None when it does not decode
    or verify.

    Dispatch-based window solvers place ops in time order and can miss a pinned op's slot,
    and a CP-SAT start (rounded to its tick) can sit inside the verifier's tolerance but
    outside the decoder's slack, so neither can be relied on to reproduce the pins; the
    floor keeps every window solvable.
    """
    index = ProblemIndex.build(_tail_problem(sub, pinned))
    choice: dict[str, str] = {}
    keyed: dict[str, list[tuple[bool, int, int, str]]] = {}
    for op in index.ops:
        option = min(op.options, key=lambda o: (o.run, o.machine_id))
        choice[op.id] = option.machine_id
        key = (op.in_progress is None, rank[index.jobs[op.job_pos].id], op.route_pos, op.id)
        keyed.setdefault(option.machine_id, []).append(key)
    sequences = {m: tuple(op_id for *_, op_id in sorted(ops)) for m, ops in keyed.items()}
    decoded = decode(index, choice, sequences)
    if isinstance(decoded, ScheduleIssue):
        log.debug("%s: the append plan does not decode (%s)", NAME, decoded.code)
        return None
    kept = tuple(pinned[op.id] for job in sub.jobs for op in job.operations if op.id in pinned)
    tail = decoded.schedule(NAME)
    schedule = tail.model_copy(update={"assignments": (*kept, *tail.assignments)})
    if not _verifies(sub, schedule):
        log.debug("%s: the append plan does not verify", NAME)
        return None
    return schedule


def _tail_problem(sub: Problem, pinned: Mapping[str, Assignment]) -> Problem:
    """`sub` without the pinned jobs, each machine available from its last pinned end and
    set up as that op left it."""
    operations = {op.id: op for job in sub.jobs for op in job.operations}
    last: dict[str, Assignment] = {}
    for a in pinned.values():
        previous = last.get(a.machine_id)
        if previous is None or (a.end, a.start) > (previous.end, previous.start):
            last[a.machine_id] = a
    machines = []
    for machine in sub.machines:
        final = last.get(machine.id)
        if final is not None:
            op = operations[final.operation_id]
            state = machine.state.model_copy(
                update={
                    "available_from": max(machine.state.available_from, final.end),
                    "last_setup_params": op.setup_params,
                    "last_setup_family": op.setup_family,
                }
            )
            machine = machine.model_copy(update={"state": state})
        machines.append(machine)
    jobs = tuple(j for j in sub.jobs if not any(op.id in pinned for op in j.operations))
    return sub.model_copy(update={"jobs": jobs, "machines": tuple(machines)})


def _verifies(problem: Problem, schedule: Schedule) -> bool:
    compiled = CompiledProblem(problem=problem, profile=SchedulingProfile())
    unvalued = schedule.model_copy(update={"primary_value": None})
    return verify(compiled, (), unvalued).valid


def _plan(index: ProblemIndex, schedule: Schedule) -> Dispatched | ScheduleIssue:
    """The schedule's machine choice and start-ordered sequences, decoded on `index`."""
    choice: dict[str, str] = {}
    keyed: dict[str, list[tuple[float, int, str]]] = {}
    for a in schedule.assignments:
        p = index.pos.get(a.operation_id)
        if p is None:
            continue
        choice[a.operation_id] = a.machine_id
        keyed.setdefault(a.machine_id, []).append((a.start, p, a.operation_id))
    sequences = {m: tuple(op_id for *_, op_id in sorted(ops)) for m, ops in keyed.items()}
    decoded = decode(index, choice, sequences)
    if isinstance(decoded, ScheduleIssue):
        return decoded
    return Dispatched(choice=choice, sequences=sequences, decoded=decoded)


# --- solver lookup -----------------------------------------------------------------------------


def _issue(code: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path=f"profile.method_params.{NAME}", message=message)


def _lookup(name: str) -> Solver | ScheduleIssue:
    """The registered solver `name` (D-069), else `subsolver_unavailable`."""
    if name == NAME:
        return _issue("invalid_method_param", f"{NAME} cannot be its own sub-solver")
    try:
        return registry.get(name)
    except (registry.UnknownSolverError, OptionalDependencyError) as error:
        return _issue("subsolver_unavailable", f"sub-solver {name!r}: {error}")


def _lookup_all(names: Sequence[str]) -> tuple[Solver, ...] | ScheduleIssue:
    solvers: list[Solver] = []
    for name in names:
        solver = _lookup(name)
        if isinstance(solver, ScheduleIssue):
            return solver
        solvers.append(solver)
    return tuple(solvers)


# --- the run -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Current:
    """The verified plan so far and its decode on the whole problem (None: no decode)."""

    schedule: Schedule
    plan: Dispatched | None
    values: Values | None


class _Run:
    """One rolling-horizon solve: windows, seams, final plan."""

    def __init__(
        self,
        problem: Problem,
        objective: ObjectiveSpec,
        context: SolveContext,
        settings: _Settings,
        budget_seconds: float,
    ) -> None:
        self._problem = problem
        self._objective = objective
        self._context = context
        self._settings = settings
        self._index = ProblemIndex.build(problem)
        self._scheduled = frozenset(job.id for job in self._index.jobs)
        self._order_rank = {self._index.jobs[k].id: r for r, k in enumerate(due_order(self._index))}
        self._end = context.deadline - FINAL_SHARE * budget_seconds
        self._scorer: Scorer | None = None

    def run(self, stages: Sequence[Solver], seam_stages: Sequence[Solver] | None) -> Schedule:
        windows = _windows(self._index, self._settings)
        seams = self._settings.seam > 0 and len(windows) > 1
        now = time.monotonic()
        windows_end = now + (self._end - now) * (WINDOW_SHARE if seams else 1.0)
        single = None
        if len(windows) > 1 and self._settings.whole_problem_floor:
            single = self._single_pass(now + (windows_end - now) / (len(windows) + 1))
        current: Schedule | None = None
        for k, window in enumerate(windows):
            share = (windows_end - time.monotonic()) / (len(windows) - k)
            solved = self._solve_window(window, current, stages, time.monotonic() + share)
            if isinstance(solved, ScheduleIssue):
                return _unknown(solved)
            current = solved
        if current is None:
            return self._final(None)
        best = self._evaluated(current)
        if seams:
            best = self._seams(windows, best, seam_stages)
        if (
            single is not None
            and single.values is not None
            and self._better(single.values, best.values)
        ):
            return self._lost_to_single_pass(single, len(windows))
        return self._final(best)

    # --- single pass (DEV-25) ------------------------------------------------------------------

    def _single_pass(self, deadline: float) -> _Current | None:
        """One `atcs` pass over the whole problem, the floor a multi-window plan must beat.

        Windows queue behind the earlier windows on every machine (the dispatch engine
        cannot fill free ops around exact pins), so with more than one window the plan can
        be far worse than one pass; None when `atcs` is not registered or gives no
        verified plan."""
        if not registry.is_registered(WHOLE_PROBLEM_SOLVER):
            return None
        context = SolveContext(
            previous_plan=self._context.previous_plan,
            churn_tolerance=self._context.churn_tolerance,
            seed=self._context.seed,
            deadline=deadline,
            warm_start=None,
            params={},
        )
        schedule = registry.get(WHOLE_PROBLEM_SOLVER).solve(
            self._problem,
            objective=self._objective,
            budget_seconds=max(deadline - time.monotonic(), 1e-3),
            context=context,
        )
        if schedule.status not in SOLUTION_STATUSES or not _verifies(self._problem, schedule):
            return None
        return self._evaluated(schedule)

    def _lost_to_single_pass(self, single: _Current, windows: int) -> Schedule:
        schedule = self._final(single)
        issue = _issue(
            "windows_lost_to_single_pass",
            f"the plan from {windows} windows scored worse than one {WHOLE_PROBLEM_SOLVER} pass "
            "over the whole problem; the single pass is returned (DEV-25)",
        )
        return schedule.model_copy(update={"issues": (*schedule.issues, issue)})

    def _solve_window(
        self,
        window: Sequence[int],
        current: Schedule | None,
        stages: Sequence[Solver],
        deadline: float,
    ) -> Schedule | ScheduleIssue:
        pinned = {} if current is None else {a.operation_id: a for a in current.assignments}
        free = frozenset(self._index.jobs[k].id for k in window)
        sub = _sub_problem(self._problem, free, pinned, self._scheduled, append=True)
        floor = _append_plan(sub, pinned, self._order_rank)
        return self._pipeline(sub, stages, floor, deadline)

    def _pipeline(
        self, sub: Problem, stages: Sequence[Solver], floor: Schedule | None, deadline: float
    ) -> Schedule | ScheduleIssue:
        """Run the stages in series from the verified `floor` (None: none); each verified
        schedule warm-starts the next and the last one is kept (the floor when none
        verifies); `window_unsolved` when there is neither."""
        best = warm = floor
        for stage in stages:
            context = SolveContext(
                previous_plan=self._context.previous_plan,
                churn_tolerance=self._context.churn_tolerance,
                seed=self._context.seed,
                deadline=deadline,
                warm_start=warm,
                params={},
            )
            schedule = stage.solve(
                sub,
                objective=self._objective,
                budget_seconds=max(deadline - time.monotonic(), 1e-3),
                context=context,
            )
            if schedule.status in SOLUTION_STATUSES and _verifies(sub, schedule):
                best = warm = schedule
            else:
                log.debug("%s: %s gave no verified plan (%s)", NAME, stage.name, schedule.status)
        if best is None:
            names = "->".join(stage.name for stage in stages)
            return _issue("window_unsolved", f"{names} gave no verified plan for a window")
        return best

    def _evaluated(self, schedule: Schedule) -> _Current:
        plan = _plan(self._index, schedule)
        if isinstance(plan, ScheduleIssue):
            return _Current(schedule, None, None)
        values = fast_values(
            self._index, plan.decoded, self._context.previous_plan, self._context.churn_tolerance
        )
        if self._scorer is None:
            self._scorer = Scorer(self._objective, values)
        return _Current(schedule, plan, values)

    # --- seams (Fig 15.3) ----------------------------------------------------------------------

    def _seams(
        self,
        windows: Sequence[Sequence[int]],
        best: _Current,
        explicit: Sequence[Solver] | None,
    ) -> _Current:
        for k in range(len(windows) - 1):
            remaining = len(windows) - 1 - k
            deadline = time.monotonic() + (self._end - time.monotonic()) / remaining
            if deadline <= time.monotonic():
                break
            seam = (*windows[k][-self._settings.seam :], *windows[k + 1][: self._settings.seam])
            best = self._reoptimize_seam(seam, best, explicit, deadline)
        return best

    def _reoptimize_seam(
        self,
        seam: Sequence[int],
        best: _Current,
        explicit: Sequence[Solver] | None,
        deadline: float,
    ) -> _Current:
        free = frozenset(self._index.jobs[k].id for k in seam)
        pinned = {
            a.operation_id: a
            for a in best.schedule.assignments
            if self._index.job_of(a.operation_id) not in free
        }
        sub = _sub_problem(self._problem, free, pinned, self._scheduled, append=False)
        stages = explicit if explicit is not None else _default_seam_solver(sub)
        if isinstance(stages, ScheduleIssue):
            return best
        solved = self._pipeline(sub, stages, best.schedule, deadline)
        if isinstance(solved, ScheduleIssue) or solved is best.schedule:
            return best
        candidate = self._evaluated(solved)
        if candidate.values is None or not self._better(candidate.values, best.values):
            return best
        return candidate

    def _better(self, values: Values, than: Values | None) -> bool:
        if than is None or self._scorer is None:
            return True  # the scorer is set with the first decoded plan
        return self._scorer.better(values, than)

    # --- final plan ----------------------------------------------------------------------------

    def _final(self, best: _Current | None) -> Schedule:
        """The decode of the sequences (D-071); the verified schedule when it does not decode."""
        if best is None:
            empty = decode(self._index, {}, {})
            if isinstance(empty, ScheduleIssue):
                return _unknown(empty)
            plan: Dispatched | ScheduleIssue = Dispatched(choice={}, sequences={}, decoded=empty)
        elif best.plan is None:
            return best.schedule.model_copy(update={"solver": NAME, "primary_value": None})
        else:
            plan = best.plan
        return dispatched_schedule(
            NAME, plan, self._index, self._objective, self._context, time.perf_counter()
        )


def _default_seam_solver(sub: Problem) -> tuple[Solver, ...] | ScheduleIssue:
    """CP-SAT for a small seam sub-problem when it is available, else tabu."""
    ops = sum(len(job.operations) for job in sub.jobs)
    if ops <= SEAM_EXACT_OPS and registry.is_registered(EXACT_SEAM_SOLVER):
        exact = _lookup_all((EXACT_SEAM_SOLVER,))
        if not isinstance(exact, ScheduleIssue):
            return exact
    return _lookup_all((FALLBACK_SEAM_SOLVER,))


def _unknown(issue: ScheduleIssue) -> Schedule:
    return Schedule(
        status="UNKNOWN", assignments=(), solver=NAME, elapsed_seconds=0.0, issues=(issue,)
    )


class RollingHorizonSolver:
    """`rolling_horizon` behind the `Solver` seam (see the module docstring)."""

    __slots__ = ()

    @property
    def name(self) -> str:
        return NAME

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
        """Cuts, windows, seams and the decoded plan; UNKNOWN with a named issue otherwise."""
        started = time.perf_counter()
        try:
            settings = _Settings.parse(context.params)
        except InvalidParamError as error:
            return _unknown(_issue("invalid_method_param", str(error)))
        stages = _lookup_all(settings.window_solver)
        if isinstance(stages, ScheduleIssue):
            return _unknown(stages)
        seam_stages: tuple[Solver, ...] | None = None
        if settings.seam > 0 and settings.seam_solver is not None:
            found = _lookup_all(settings.seam_solver)
            if isinstance(found, ScheduleIssue):
                return _unknown(found)
            seam_stages = found
        run = _Run(problem, objective, context, settings, budget_seconds)
        schedule = run.run(stages, seam_stages)
        return schedule.model_copy(update={"elapsed_seconds": time.perf_counter() - started})


SOLVERS = (
    SolverSpec(
        name=NAME,
        family="decomposition",
        factory=RollingHorizonSolver,
        deterministic=True,
        capabilities=frozenset(Feature),
    ),
)
"""Registry entries (D-069)."""
