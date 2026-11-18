"""ATC and ATCS composite dispatch (COMP-010, REQ-092..REQ-095, REQ-121, D-073).

Registers `atc` and `atcs` (family `composite`, deterministic, every `Feature`) on the
dispatch engine. Per work center c (`kernel.stats.InstanceStats`: tau, R, eta, m, pbar,
sbar) the rule fixes K once (Pinedo p.376): explicit params `K1` / `K2`, else the
`k_lookup(tau, R, eta, m)` constructor argument, else `K1 = 4.5 + R` (R <= 0.5) or
`6 - 2R`, `K2 = tau / (2 sqrt(eta))`; K1 is clamped to [0.5, 10], K2 to [0.05, 10]. For op o
of job j on machine i (center c) at time t after op l:

    (w_j / p_i(o)) * exp(-max(dloc(o) - p_i(o) - t, 0) / (K1 * pbar_c))
                   * exp(-s_i(l, o) / (K2 * sbar_c)) * flex(o, c) * suit(o, i)

with `dloc = d_j - phi * R_after(o)` (`phi`, default 2). ATC fixes the setup factor at 1;
so does ATCS when sbar or eta is 0. `flex = 1 + 0.5 (1 - |M(o) in c| / m)` with param
`flexibility`, `suit = exp(-pref_i(o) / prefbar_c)` with param `suitability` (prefbar = mean
over the center's ops of their mean option preference cost; factor 1 when it is 0).

Readings [mine]: the index is computed as its logarithm (same order, no underflow to a
tie); an op without a due date has no urgency factor and ranks after every op with one
(the priority is (has due, log index)); K1 * pbar = 0 gives urgency factor 1.

**Macro jobs** (param `macro_jobs`, Pinedo p.93, p.448): before dispatch, ops with a due
date, no batch family, not in progress and the same option machines are grouped in (due, op
id) order; an op joins the open group when its due is within `merge_window` (default the
center's pbar) of the group's first due and its setup to and from every member is 0 on every
one of those machines, else it opens a new group. While no member is placed, each member's
index uses the group's summed w and summed p and its smallest dloc. Once one member is placed,
the others follow on that machine in op-id order at its next decisions while their gates are
met when the previous member finishes (the engine's followers hook); a member whose gate is
not met returns to the pool with its own index.

Params (`SolveContext.params`): `K1`, `K2` (ATC reads its single K from `K1`), `phi`,
`flexibility`, `suitability`, `macro_jobs`, `merge_window`, `mode` (`nondelay` | `active`).
An invalid or unknown param gives UNKNOWN with an `invalid_method_param` issue. Each solve
returns one `AtcsRun` per work center with machines in `Schedule.atcs_runs`.

A leaf of `algorithms/` (D-069): it registers through `SOLVERS`, and the bare rules through
`RULES` (`registry.rule("atcs", **params)`: the params above but `mode`, a fresh `AtcsRule`
per call that the engine prepares per run; `k_lookup` is not offered there).
"""

from __future__ import annotations

import functools
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal

from twinflow.scheduling.kernel.dispatch import (
    DispatchEngine,
    DispatchState,
    Mode,
    Priority,
    dispatch_schedule,
)
from twinflow.scheduling.kernel.index import DEFAULT_PHI, OpData, ProblemIndex
from twinflow.scheduling.kernel.stats import InstanceStats, KLookup, atcs_k, clamp_k
from twinflow.scheduling.model import AtcsRun, ObjectiveSpec, Problem, Schedule, ScheduleIssue
from twinflow.scheduling.registry import RuleSpec, SolverSpec
from twinflow.scheduling.seam import Feature, ParamValue, SolveContext

KSource = Literal["formula", "table", "explicit"]

FLEXIBILITY_BETA = 0.5

_BOOL_PARAMS = ("flexibility", "suitability", "macro_jobs")
_NUMBER_PARAMS = ("K1", "K2", "phi", "merge_window")
_KNOWN_PARAMS = frozenset((*_BOOL_PARAMS, *_NUMBER_PARAMS, "mode"))


# --- factors (public: pinned by the tests; the K formula `atcs_k` is in kernel.stats) -------


def flexibility_factor(eligible: int, m: int) -> float:
    """1 + 0.5 (1 - eligible / m): ops with fewer eligible machines of the center go first."""
    return 1.0 + FLEXIBILITY_BETA * (1.0 - eligible / m) if m > 0 else 1.0


def suitability_factor(preference_cost: float, prefbar: float) -> float:
    """exp(-preference_cost / prefbar) (K3 = 1); 1 when prefbar is 0."""
    return math.exp(-preference_cost / prefbar) if prefbar > 0 else 1.0


# --- settings --------------------------------------------------------------------------------


class InvalidParamError(ValueError):
    """A method param with an unknown name or an invalid value; the message names it."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _Settings:
    """Validated `SolveContext.params` of one solve."""

    k1: float | None = None
    k2: float | None = None
    phi: float = DEFAULT_PHI
    flexibility: bool = False
    suitability: bool = False
    macro_jobs: bool = False
    merge_window: float | None = None
    mode: Mode = "nondelay"

    @classmethod
    def from_params(cls, params: Mapping[str, ParamValue]) -> _Settings:
        unknown = sorted(set(params) - _KNOWN_PARAMS)
        if unknown:
            raise InvalidParamError(f"unknown param {unknown[0]}")
        mode = params.get("mode", "nondelay")
        if mode not in ("nondelay", "active"):
            raise InvalidParamError("mode must be nondelay or active")
        return cls(
            k1=_positive(params, "K1"),
            k2=_positive(params, "K2"),
            phi=_non_negative(params, "phi", DEFAULT_PHI),
            flexibility=_flag(params, "flexibility"),
            suitability=_flag(params, "suitability"),
            macro_jobs=_flag(params, "macro_jobs"),
            merge_window=_positive(params, "merge_window"),
            mode="active" if mode == "active" else "nondelay",
        )


def _number(params: Mapping[str, ParamValue], name: str) -> float | None:
    value = params.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise InvalidParamError(f"{name} must be a finite number")
    return float(value)


def _positive(params: Mapping[str, ParamValue], name: str) -> float | None:
    value = _number(params, name)
    if value is not None and value <= 0:
        raise InvalidParamError(f"{name} must be positive")
    return value


def _non_negative(params: Mapping[str, ParamValue], name: str, default: float) -> float:
    value = _number(params, name)
    if value is None:
        return default
    if value < 0:
        raise InvalidParamError(f"{name} must not be negative")
    return value


def _flag(params: Mapping[str, ParamValue], name: str) -> bool:
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise InvalidParamError(f"{name} must be true or false")
    return value


# --- per-center K ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Center:
    """One center's divisors as reciprocals (0 = factor 1) and its machine count."""

    m: int
    inv_urgency: float
    inv_setup: float
    inv_preference: float


def _choose_k(
    stats: InstanceStats, settings: _Settings, k_lookup: KLookup | None
) -> tuple[float, float, KSource]:
    """Explicit params > `k_lookup` > formula, per K; clamped (SDD ATCS pseudo-code)."""
    if settings.k1 is not None and settings.k2 is not None:
        k1, k2 = settings.k1, settings.k2
    elif k_lookup is not None:
        k1, k2 = k_lookup(stats.tau, stats.R, stats.eta, stats.m)
    else:
        k1, k2 = atcs_k(stats.tau, stats.R, stats.eta)
    explicit = settings.k1 is not None or settings.k2 is not None
    source: KSource = "explicit" if explicit else "table" if k_lookup is not None else "formula"
    k1 = settings.k1 if settings.k1 is not None else k1
    k2 = settings.k2 if settings.k2 is not None else k2
    return (*clamp_k(k1, k2), source)


def _mean_preference(index: ProblemIndex, machines: set[str]) -> float:
    """prefbar: mean over the center's pending ops of their mean option preference cost."""
    means = []
    for op_data in index.ops:
        costs = [o.preference_cost for o in op_data.options if o.machine_id in machines]
        if costs and op_data.in_progress is None:
            means.append(sum(costs) / len(costs))
    return sum(means) / len(means) if means else 0.0


def _reciprocal(value: float) -> float:
    return 1.0 / value if value > 0 else 0.0


# --- macro jobs --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Group:
    """A macro job: member op ids (op-id order), summed weight, smallest dloc, runs by machine."""

    members: tuple[str, ...]
    weight: float
    local_due: float
    runs: Mapping[str, float]


def _macro_groups(index: ProblemIndex, window_of: Callable[[OpData], float]) -> dict[str, _Group]:
    """Macro groups of two or more ops, by member op id (module docstring)."""
    buckets: dict[tuple[str, ...], list[OpData]] = {}
    for op_data in index.ops:
        due = index.jobs[op_data.job_pos].due
        if due is None or op_data.batch_family is not None or op_data.in_progress is not None:
            continue
        machines = tuple(o.machine_id for o in op_data.options)
        buckets.setdefault(machines, []).append(op_data)
    groups: dict[str, _Group] = {}
    for machines, ops in sorted(buckets.items()):
        for members in _split_bucket(index, machines, ops, window_of):
            if len(members) > 1:
                group = _group(index, members)
                groups.update(dict.fromkeys(group.members, group))
    return groups


def _split_bucket(
    index: ProblemIndex,
    machines: tuple[str, ...],
    ops: list[OpData],
    window_of: Callable[[OpData], float],
) -> list[list[OpData]]:
    """Greedy groups in (due, op id) order: join the open group or open a new one."""

    def due(op_data: OpData) -> float:
        value = index.jobs[op_data.job_pos].due
        return math.inf if value is None else value

    groups: list[list[OpData]] = []
    for op_data in sorted(ops, key=lambda o: (due(o), o.id)):
        if groups and _joins(index, machines, groups[-1], op_data, window_of, due):
            groups[-1].append(op_data)
        else:
            groups.append([op_data])
    return groups


def _joins(
    index: ProblemIndex,
    machines: tuple[str, ...],
    group: list[OpData],
    op_data: OpData,
    window_of: Callable[[OpData], float],
    due: Callable[[OpData], float],
) -> bool:
    head = group[0]
    if due(op_data) - due(head) > window_of(head):
        return False
    setups = index.setups
    return all(
        setups.seconds(machine_id, member.id, op_data.id) == 0
        and setups.seconds(machine_id, op_data.id, member.id) == 0
        for machine_id in machines
        for member in group
    )


def _group(index: ProblemIndex, members: list[OpData]) -> _Group:
    runs: dict[str, float] = {}
    for op_data in members:
        for option in op_data.options:
            runs[option.machine_id] = runs.get(option.machine_id, 0.0) + option.run
    dues = [op_data.local_due for op_data in members if op_data.local_due is not None]
    return _Group(
        members=tuple(sorted(op_data.id for op_data in members)),
        weight=sum(index.jobs[op_data.job_pos].weight for op_data in members),
        local_due=min(dues),
        runs=runs,
    )


# --- the rule ----------------------------------------------------------------------------------


class AtcsRule:
    """The ATCS (or ATC when `setups` is False) priority rule; see the module docstring."""

    def __init__(self, settings: _Settings, k_lookup: KLookup | None, *, setups: bool) -> None:
        self._settings = settings
        self._k_lookup = k_lookup
        self._setups = setups
        self._centers: dict[str, _Center] = {}
        self._center_of: dict[str, str] = {}
        self._groups: dict[str, _Group] = {}
        self.runs: tuple[AtcsRun, ...] = ()

    def prepare(self, index: ProblemIndex) -> None:
        """Statistics and K per work center with machines; macro groups when on."""
        self._center_of = {m.id: m.work_center_id for m in index.machines}
        stats_of: dict[str, InstanceStats] = {}
        runs = []
        for center_id, machines in sorted(index.center_machines.items()):
            if not machines:
                continue
            stats = InstanceStats.for_center(index, center_id, index.now)
            k1, k2, source = _choose_k(stats, self._settings, self._k_lookup)
            stats_of[center_id] = stats
            self._centers[center_id] = self._center(index, stats, k1, k2)
            runs.append(_run_record(stats, k1, k2, source))
        self.runs = tuple(runs)
        if self._settings.macro_jobs:
            self._groups = _macro_groups(index, self._window(stats_of))

    def _center(self, index: ProblemIndex, stats: InstanceStats, k1: float, k2: float) -> _Center:
        setup_on = self._setups and stats.sbar > 0 and stats.eta > 0
        prefbar = 0.0
        if self._settings.suitability:
            prefbar = _mean_preference(index, set(index.machines_of(stats.work_center_id)))
        return _Center(
            m=stats.m,
            inv_urgency=_reciprocal(k1 * stats.pbar),
            inv_setup=_reciprocal(k2 * stats.sbar) if setup_on else 0.0,
            inv_preference=_reciprocal(prefbar),
        )

    def _window(self, stats_of: Mapping[str, InstanceStats]) -> Callable[[OpData], float]:
        """merge_window, else the pbar of the center of the op's first option machine."""
        fixed = self._settings.merge_window
        if fixed is not None:
            return lambda _op: fixed

        def pbar(op_data: OpData) -> float:
            return stats_of[self._center_of[op_data.options[0].machine_id]].pbar

        return pbar

    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        index = state.index
        op_data = index.op(op_id)
        center_id = self._center_of[machine_id]
        center = self._centers[center_id]
        option = state.option(op_id, machine_id)
        weight, run, local_due = self._macro_or_own(state, op_data, machine_id)
        value = _log(weight) - _log(run)
        if center.inv_setup:
            value -= index.setups.seconds(machine_id, last_op_id, op_id) * center.inv_setup
        if self._settings.flexibility:
            eligible = sum(1 for o in op_data.options if self._center_of[o.machine_id] == center_id)
            value += math.log(flexibility_factor(eligible, center.m))
        value -= option.preference_cost * center.inv_preference
        if local_due is None:
            return (0.0, value)
        return (1.0, value - max(local_due - run - t, 0.0) * center.inv_urgency)

    def _macro_or_own(
        self, state: DispatchState, op_data: OpData, machine_id: str
    ) -> tuple[float, float, float | None]:
        """(w, p, dloc) of the op's intact macro group, else of the op itself."""
        group = self._groups.get(op_data.id)
        if group is not None and not any(state.is_placed(m) for m in group.members):
            return group.weight, group.runs[machine_id], group.local_due
        run = state.option(op_data.id, machine_id).run
        return state.index.jobs[op_data.job_pos].weight, run, op_data.local_due

    def followers(self, state: DispatchState, op_id: str, machine_id: str) -> Sequence[str]:
        """The other unplaced members of the op's macro group, in op-id order."""
        group = self._groups.get(op_id)
        if group is None:
            return ()
        return tuple(m for m in group.members if m != op_id and not state.is_placed(m))


def _log(value: float) -> float:
    """log for the index; log(0) is -inf (a zero run makes w / p infinite)."""
    return math.log(value) if value > 0 else -math.inf


def _run_record(stats: InstanceStats, k1: float, k2: float, source: KSource) -> AtcsRun:
    return AtcsRun(
        work_center_id=stats.work_center_id,
        tau=stats.tau,
        R=stats.R,
        eta=stats.eta,
        m=stats.m,
        K1=k1,
        K2=k2,
        source=source,
    )


# --- the solver --------------------------------------------------------------------------------


class AtcsSolver:
    """ATC or ATCS on the dispatch engine behind the `Solver` seam (COMP-010).

    `k_lookup` is a constructor argument (`registry.get("atcs", k_lookup=...)`); scalar
    settings come from `context.params` per solve.
    """

    enforces_budget: ClassVar[bool] = True

    __slots__ = ("_k_lookup", "_name", "_setups")

    def __init__(self, name: str, *, setups: bool, k_lookup: KLookup | None = None) -> None:
        self._name = name
        self._setups = setups
        self._k_lookup = k_lookup

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> frozenset[Feature]:
        """Every feature: placement goes through the shared timelines and the decoder."""
        return frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        """One dispatch pass; `atcs_runs` carry each center's K and the primary value."""
        started = time.perf_counter()
        try:
            settings = _Settings.from_params(context.params)
        except InvalidParamError as error:
            return self._invalid(error, started)
        index = ProblemIndex.build(problem, settings.phi)
        rule = AtcsRule(settings, self._k_lookup, setups=self._setups)
        engine = DispatchEngine(index, rule, mode=settings.mode)
        schedule = dispatch_schedule(self._name, engine, index, objective, context, started)
        value = schedule.primary_value
        runs = tuple(r.model_copy(update={"objective_value": value}) for r in rule.runs)
        return schedule.model_copy(update={"atcs_runs": runs})

    def _invalid(self, error: InvalidParamError, started: float) -> Schedule:
        issue = ScheduleIssue(
            code="invalid_method_param",
            path=f"profile.method_params.{self._name}",
            message=str(error),
        )
        return Schedule(
            status="UNKNOWN",
            assignments=(),
            solver=self._name,
            elapsed_seconds=time.perf_counter() - started,
            issues=(issue,),
        )


SOLVERS = tuple(
    SolverSpec(
        name=name,
        family="composite",
        factory=functools.partial(AtcsSolver, name, setups=setups),
        deterministic=True,
        capabilities=frozenset(Feature),
    )
    for name, setups in (("atc", False), ("atcs", True))
)
"""Registry entries (D-069)."""


def _rule(setups: bool, **params: ParamValue) -> AtcsRule:
    """A fresh ATC/ATCS rule from method params; `mode` belongs to the solver, not the rule."""
    if "mode" in params:
        raise InvalidParamError("mode is a solver param, not a rule param")
    return AtcsRule(_Settings.from_params(params), None, setups=setups)


RULES = tuple(
    RuleSpec(name=name, factory=functools.partial(_rule, setups))
    for name, setups in (("atc", False), ("atcs", True))
)
"""Rule entries (`registry.rule`)."""
