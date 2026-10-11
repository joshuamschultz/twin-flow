"""Filtered beam search (COMP-019, REQ-110; SDD "Filtered beam search", Pinedo Sec 15.1).

Registers `beam` (family `improvement`, deterministic, every `Feature`). `beam_search` per
the SDD pseudo-code (p.396-398):

- Nodes are partial dispatches (`kernel.dispatch.DispatchState`); a node's children are its
  Giffler-Thompson conflict set (`gt_branch`), one child per op committed on the conflict
  machine. A node's `key` is the op ids committed from the root, in commit order.
- Crude filter (`filter_width`, None or 0 = off): the children with the smallest
  crude(child) = weighted tardiness of the jobs fully placed + sum over the other jobs of
  w_j max(0, head_j + remaining shortest run - d_j) survive (ties: key). `head_j` [mine] is
  max(end of the job's last placed op, release of its next op, earliest free time among
  that op's machines): without the machine term every child of a single machine ties.
- Thorough evaluation: each survivor's rollout, the rest of its dispatch completed by the
  non-delay engine with the rollout rule (`atcs`, or `atc`, with `K1` when given; built by
  `registry.rule`), scored by `kernel.score.Scorer` over `fast_values` of the decode. The
  `beam_width` best rollouts (ties: key) form the next beam; finished nodes are not
  expanded again.
- Anytime incumbent: the root rollout, always computed (even past the deadline), then the
  best rollout seen. The search stops at the deadline, mid-level if need be (that level's
  rolled-out nodes are recorded with `kept` False), or when the beam is empty.

The solver stops its search 5 percent of the budget before `context.deadline` [mine], so
building and verifying the plan stays inside the budget. Params (`SolveContext.params`):
`beam_width` (int >= 1, default 4), `filter_width` (int >= 0, default 12; 0 = off), `rule`
(`atcs` | `atc`) and `K1` (positive number); anything else gives UNKNOWN with
`invalid_method_param`. A missing rollout-rule leaf gives UNKNOWN `subsolver_unavailable`.

A leaf of `algorithms/` (D-069): it registers through `SOLVERS` and composes rules by name.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from twinflow.scheduling import registry
from twinflow.scheduling.kernel.dispatch import (
    Dispatched,
    DispatchEngine,
    DispatchState,
    dispatched_schedule,
    gt_branch,
)
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.score import Scorer, Values, fast_values
from twinflow.scheduling.model import ObjectiveSpec, PreviousPlan, Problem, Schedule, ScheduleIssue
from twinflow.scheduling.objectives import primary_kind
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature, ParamValue, SolveContext

NAME = "beam"
DEFAULT_BEAM_WIDTH = 4
DEFAULT_FILTER_WIDTH = 12
DEFAULT_RULE = "atcs"
ROLLOUT_RULES = ("atc", "atcs")
SEARCH_SHARE = 0.95
"""Share of the budget the search may use; the rest builds and verifies the plan."""


@dataclass(frozen=True, slots=True)
class BeamNode:
    """One rolled-out node: committed op ids, its rollout's primary value, kept or not."""

    key: tuple[str, ...]
    value: float
    kept: bool


@dataclass(frozen=True, slots=True)
class BeamResult:
    """The incumbent, its primary value and the rolled-out nodes per depth (1, 2, ...)."""

    best: Dispatched
    best_value: float
    levels: tuple[tuple[BeamNode, ...], ...]


@dataclass(frozen=True, slots=True)
class _Child:
    key: tuple[str, ...]
    state: DispatchState


@dataclass(frozen=True, slots=True)
class _Rolled:
    child: _Child
    plan: Dispatched
    values: Values


# --- the search ----------------------------------------------------------------------------------


def beam_search(
    problem: Problem,
    objective: ObjectiveSpec,
    *,
    beam_width: int = DEFAULT_BEAM_WIDTH,
    filter_width: int | None = DEFAULT_FILTER_WIDTH,
    rule: str = DEFAULT_RULE,
    K1: float | None = None,  # noqa: N803  # Pinedo's name, as the ATCS param
    deadline: float | None = None,
    previous_plan: PreviousPlan | None = None,
    churn_tolerance: float = 0.0,
) -> BeamResult | ScheduleIssue:
    """Filtered beam search on a compiled problem (module docstring).

    `deadline` is a `time.monotonic()` instant (None: none). Returns the root rollout's
    issue when even it has no plan. `registry.UnknownRuleError` for an unknown `rule`.
    """
    index = ProblemIndex.build(problem)
    params = {} if K1 is None else {"K1": K1}
    engine = DispatchEngine(index, registry.rule(rule, **params))
    search = _Search(index, objective, engine, previous_plan, churn_tolerance)
    return search.run(beam_width, filter_width or None, deadline)


class _Search:
    """One beam search: the engine, the scorer and the incumbent."""

    def __init__(
        self,
        index: ProblemIndex,
        objective: ObjectiveSpec,
        engine: DispatchEngine,
        previous_plan: PreviousPlan | None,
        churn_tolerance: float,
    ) -> None:
        self.index = index
        self.objective = objective
        self.engine = engine
        self.previous_plan = previous_plan
        self.churn_tolerance = churn_tolerance
        self.scorer = Scorer(objective, None)
        self.best: _Rolled | None = None

    def run(
        self, beam_width: int, filter_width: int | None, deadline: float | None
    ) -> BeamResult | ScheduleIssue:
        root = _Child((), self.engine.initial_state())
        rolled = self._rollout(root, None)
        if isinstance(rolled, ScheduleIssue):
            return rolled
        self.scorer = Scorer(self.objective, rolled.values)
        self.best = rolled
        levels: list[tuple[BeamNode, ...]] = []
        beam = [root]
        while beam and not _past(deadline):
            children = self._filtered(_children(beam), filter_width)
            if not children:
                break
            scored, finished = self._roll_level(children, deadline)
            kept = {r.child.key for r in sorted(scored, key=self._rank)[:beam_width]}
            if not finished:
                kept = set()
            levels.append(tuple(self._node(r, r.child.key in kept) for r in scored))
            beam = [r.child for r in scored if r.child.key in kept and not r.child.state.done]
        best = self._best()
        return BeamResult(best=best.plan, best_value=self._primary(best), levels=tuple(levels))

    def _roll_level(
        self, children: Sequence[_Child], deadline: float | None
    ) -> tuple[list[_Rolled], bool]:
        """Rollouts of `children` until the deadline; (rolled, whether every child rolled)."""
        scored: list[_Rolled] = []
        for child in children:
            rolled = self._rollout(child, deadline)
            if isinstance(rolled, ScheduleIssue):
                if rolled.code == "time_limit":
                    return scored, False
                continue
            scored.append(rolled)
            if self.scorer.better(rolled.values, self._best().values):
                self.best = rolled
        return scored, True

    def _rollout(self, child: _Child, deadline: float | None) -> _Rolled | ScheduleIssue:
        plan = self.engine.complete(child.state.copy(), deadline)
        if isinstance(plan, ScheduleIssue):
            return plan
        values = fast_values(self.index, plan.decoded, self.previous_plan, self.churn_tolerance)
        return _Rolled(child, plan, values)

    def _filtered(self, children: list[_Child], width: int | None) -> list[_Child]:
        if width is None or len(children) <= width:
            return children
        return sorted(children, key=lambda c: (_crude(c.state), c.key))[:width]

    def _rank(self, rolled: _Rolled) -> tuple[tuple[float, ...], tuple[str, ...]]:
        return self.scorer.key(rolled.values), rolled.child.key

    def _node(self, rolled: _Rolled, kept: bool) -> BeamNode:
        return BeamNode(key=rolled.child.key, value=self._primary(rolled), kept=kept)

    def _primary(self, rolled: _Rolled) -> float:
        return rolled.values[primary_kind(self.objective)]

    def _best(self) -> _Rolled:
        if self.best is None:
            raise AssertionError("the root rollout sets the incumbent first")
        return self.best


def _past(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


def _children(beam: Sequence[_Child]) -> list[_Child]:
    """One child per op of each unfinished node's Giffler-Thompson conflict set."""
    children: list[_Child] = []
    for node in beam:
        for op_id, machine_id in gt_branch(node.state):
            state = node.state.copy()
            if state.commit(op_id, machine_id) is None:
                children.append(_Child((*node.key, op_id), state))
    return children


def _crude(state: DispatchState) -> float:
    """Weighted tardiness bound of a partial dispatch (module docstring)."""
    index = state.index
    total = 0.0
    for job in index.jobs:
        if job.due is None:
            continue
        head, remaining = _job_head(state, job.ops)
        total += job.weight * max(0.0, head + remaining - job.due)
    return total


def _job_head(state: DispatchState, positions: tuple[int, ...]) -> tuple[float, float]:
    """(head, remaining shortest run) of a job; head is its completion once fully placed."""
    index = state.index
    head = -math.inf
    for p in positions:
        op = index.ops[p]
        end = state.end_of(op.id)
        if end is not None:
            head = max(head, end)
            continue
        free = min(state.free_time(o.machine_id) for o in op.options)
        run = min(o.run for o in op.options)
        return max(head, op.release, free), run + op.remaining_shortest_run
    return head, 0.0


# --- params and the solver -----------------------------------------------------------------------


class InvalidParamError(ValueError):
    """A method param with an unknown name or an invalid value; the message names it."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _Settings:
    beam_width: int = DEFAULT_BEAM_WIDTH
    filter_width: int = DEFAULT_FILTER_WIDTH
    rule: str = DEFAULT_RULE
    k1: float | None = None

    @classmethod
    def from_params(cls, params: Mapping[str, ParamValue]) -> _Settings:
        unknown = sorted(set(params) - {"beam_width", "filter_width", "rule", "K1"})
        if unknown:
            raise InvalidParamError(f"unknown param {unknown[0]}")
        rule = params.get("rule", DEFAULT_RULE)
        if rule not in ROLLOUT_RULES:
            raise InvalidParamError(f"rule must be one of {', '.join(ROLLOUT_RULES)}")
        return cls(
            beam_width=_int(params, "beam_width", DEFAULT_BEAM_WIDTH, lowest=1),
            filter_width=_int(params, "filter_width", DEFAULT_FILTER_WIDTH, lowest=0),
            rule=str(rule),
            k1=_k1(params),
        )


def _int(params: Mapping[str, ParamValue], name: str, default: int, *, lowest: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < lowest:
        raise InvalidParamError(f"{name} must be an integer >= {lowest}")
    return value


def _k1(params: Mapping[str, ParamValue]) -> float | None:
    value = params.get("K1")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        raise InvalidParamError("K1 must be a positive number")
    if not math.isfinite(value):
        raise InvalidParamError("K1 must be a positive number")
    return float(value)


class BeamSolver:
    """Filtered beam search behind the `Solver` seam (COMP-019)."""

    __slots__ = ()

    @property
    def name(self) -> str:
        return NAME

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
        """The beam's incumbent at the search deadline; UNKNOWN with a named issue otherwise."""
        started = time.perf_counter()
        index = ProblemIndex.build(problem)
        result = self._search(problem, objective, budget_seconds, context)
        plan = result if isinstance(result, ScheduleIssue) else result.best
        return dispatched_schedule(NAME, plan, index, objective, context, started)

    def _search(
        self,
        problem: Problem,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> BeamResult | ScheduleIssue:
        path = f"profile.method_params.{NAME}"
        try:
            settings = _Settings.from_params(context.params)
        except InvalidParamError as error:
            return ScheduleIssue(code="invalid_method_param", path=path, message=str(error))
        try:
            return beam_search(
                problem,
                objective,
                beam_width=settings.beam_width,
                filter_width=settings.filter_width,
                rule=settings.rule,
                K1=settings.k1,
                deadline=context.deadline - (1.0 - SEARCH_SHARE) * budget_seconds,
                previous_plan=context.previous_plan,
                churn_tolerance=context.churn_tolerance,
            )
        except registry.UnknownRuleError as error:
            return ScheduleIssue(
                code="subsolver_unavailable", path=f"{path}.rule", message=str(error)
            )


SOLVERS = (
    SolverSpec(
        name=NAME,
        family="improvement",
        factory=BeamSolver,
        deterministic=True,
        capabilities=frozenset(Feature),
    ),
)
"""Registry entries (D-069)."""
