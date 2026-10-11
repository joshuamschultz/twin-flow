"""Tabu search and simulated annealing (COMP-018, REQ-108, REQ-109, D-076).

Registers `tabu` (Pinedo Alg 14.3.3, p.382-384; deterministic) and `sa` (Alg 14.3.2,
p.380-382; not deterministic: it draws from `random.Random(context.seed)`), family
`improvement`, every `Feature` (each kept plan is decoded). Both walk the `kernel.moves`
neighborhood of a machine choice and sequences (`Plan`) and keep the best decoded plan.

- Graph screen, decode confirm (D-076): a move is valued on the disjunctive graph
  (`moves.evaluate`, exact on graph-exact problems, an estimate otherwise); only a move that
  is taken is decoded (`moves.confirm`), and only decoded values become current or best. A
  move whose decode fails is dropped. The graph follows each taken move in place
  (`apply_swap` / `move_node`), never rebuilt.
- `tabu_search`: each iteration values every move (the deadline is checked per move) and
  takes the best (value, then move key) that is not tabu, or that is tabu but beats the best
  (aspiration, p.381, on graph values); a dropped move gives way to the next. The tabu list
  holds the reverse move's attribute (the unordered op pair; for a reassign (op, its new
  machine), so op may not leave it) for the last `tabu_length` moves.
- `simulated_annealing`: each iteration draws one move (`rng.randrange`; a move that closes
  a cycle on the graph is dropped and another drawn), delta = `Scorer(spec, start
  values).delta(candidate, current)` on graph values (see `_Walk`); accepted when delta <= 0,
  else when `rng.random() < exp(-delta / beta)` (the one acceptance draw, worse candidates
  only). `beta_1` = the delta of the start with its primary 5 percent worse (5 percent of 1
  when the primary is 0), over ln 2, so such a move is accepted with probability 1/2 at first
  [mine]; when that delta is not positive (an epsilon bound already broken) 0.05 / ln 2.
  `beta_{k+1} = sa_cooling * beta_k`. A step's `values` are the graph values the decision
  used.

Params (`SolveContext.params`; an unknown or invalid one gives UNKNOWN
`invalid_method_param`): `tabu`: `tabu_length` (int 5..9, default 7; any int >= 1 with
the test-only `allow_any_tabu_length` true), `iteration_limit` (int >= 1, default
`DEFAULT_ITERATION_LIMIT` moves); `sa`: `sa_iterations` (int >= 1, default 20,000),
`sa_cooling` (float in (0, 1), default 0.995).

Start: `context.warm_start` as choice + per-machine sequences in start order when that
decodes; else `atcs` through the registry; else the dispatch engine with the `edd` rule
(`registry.rule`); a missing `edd` leaf gives UNKNOWN `subsolver_unavailable`. The start is
built without a deadline (it is the anytime floor); the search stops 5 percent of the
budget before `context.deadline` [mine], leaving the rest to decode and verify the plan.

A leaf of `algorithms/` (D-069): it registers through `SOLVERS` and composes by name.
"""

from __future__ import annotations

import dataclasses
import functools
import math
import random
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from twinflow.scheduling import registry
from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.dispatch import Dispatched, DispatchEngine, dispatched_schedule
from twinflow.scheduling.kernel.graph import DisjunctiveGraph, GraphCycle
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.moves import Attribute, Confirmed, Move, apply, confirm, evaluate
from twinflow.scheduling.kernel.moves import moves as neighborhood
from twinflow.scheduling.kernel.score import Scorer, Values, fast_values
from twinflow.scheduling.model import (
    ObjectiveKind,
    ObjectiveSpec,
    PreviousPlan,
    Problem,
    Schedule,
    ScheduleIssue,
)
from twinflow.scheduling.objectives import primary_kind, signed
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature, ParamValue, SolveContext

TABU = "tabu"
SA = "sa"
DEFAULT_TABU_LENGTH = 7
TABU_LENGTHS = range(5, 10)
DEFAULT_ITERATION_LIMIT = 30
"""Tabu moves per run [mine]. Measured on the 40x5x4 training instances: 30 moves take
0.4 to 0.8 s (about 145 graph-valued moves each), so inside the 2 s gate budget tabu stops on
this limit, not the clock, and its output is deterministic."""
DEFAULT_SA_ITERATIONS = 20_000
DEFAULT_SA_COOLING = 0.995
WORSE_SHARE = 0.05
"""beta_1 accepts a move this share worse than the start with probability 1/2."""
SEARCH_SHARE = 0.95
"""Share of the budget the search may use; the rest decodes and verifies the plan."""

Sequences = Mapping[str, Sequence[str]]


@dataclass(frozen=True, slots=True)
class Plan:
    """A machine choice (op id -> machine id) and sequences (machine id -> op ids)."""

    choice: Mapping[str, str]
    sequences: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class Visit:
    """One tabu move: the plan reached, its decoded values, taken by aspiration or not."""

    plan: Plan
    values: Values
    aspiration: bool


@dataclass(frozen=True, slots=True)
class TabuResult:
    """The best decoded plan, its values and the visits (empty unless recorded)."""

    best: Plan
    best_values: Values
    trace: tuple[Visit, ...]


@dataclass(frozen=True, slots=True)
class AnnealStep:
    """One annealing iteration: the candidate, its graph values, delta, beta, accepted."""

    plan: Plan
    values: Values
    delta: float
    beta: float
    accepted: bool


@dataclass(frozen=True, slots=True)
class AnnealResult:
    """The best decoded plan, its values and the steps (empty unless recorded)."""

    best: Plan
    best_values: Values
    trace: tuple[AnnealStep, ...]


class Draws(Protocol):
    """The part of `random.Random` annealing uses."""

    def randrange(self, stop: int, /) -> int: ...

    def random(self) -> float: ...


class InvalidParamError(ValueError):
    """A method param with an unknown name or an invalid value; the message names it."""


# --- the walk shared by both searches -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Candidate:
    move: Move
    values: Values


class _Walk:
    """The current plan, its graph and decoded values, and the best decoded plan.

    Moves are compared on graph values only (`graph_values`, `best_graph_values`: the graph's
    values of the current and the best plan), never a graph estimate against a decoded value:
    with calendars or operators the graph ignores waiting, so that comparison would call
    nearly every move an improvement. Decoded values alone decide current and best (D-076).
    """

    def __init__(
        self,
        index: ProblemIndex,
        spec: ObjectiveSpec,
        start: Plan,
        previous_plan: PreviousPlan | None,
        churn_tolerance: float,
    ) -> None:
        decoded = decode(index, start.choice, start.sequences)
        if isinstance(decoded, ScheduleIssue):
            raise ValueError(f"the start plan does not decode: {decoded.code}")
        self.index = index
        self.spec = spec
        self.previous_plan = previous_plan
        self.churn_tolerance = churn_tolerance
        self.graph = DisjunctiveGraph.build(index, start.choice, start.sequences)
        self.current = start
        self.values: Values = self._values(decoded)
        self.scorer = Scorer(spec, self.values)
        self.best = start
        self.best_values: Values = self.values
        self.graph_values: Values = self._values(self.graph.as_decoded())
        self.best_graph_values: Values = self.graph_values

    def moves(self) -> list[Move]:
        return neighborhood(self.graph, self.spec, self.index)

    def estimate(self, move: Move) -> Values | None:
        """The graph's values after `move`; None when it closes a cycle."""
        try:
            return self._values(evaluate(self.graph, move))
        except GraphCycle:
            return None

    def take(self, move: Move, estimate: Values) -> Plan | None:
        """Decode `move` from the current plan and make it current; None when it fails."""
        choice, sequences = apply(move, self.current.choice, self.current.sequences)
        confirmed = confirm(
            self.index,
            estimate,
            choice,
            sequences,
            self.scorer,
            self.best_values,
            previous_plan=self.previous_plan,
            churn_tolerance=self.churn_tolerance,
        )
        if not isinstance(confirmed, Confirmed):
            return None
        _play(self.graph, move)
        self.current = _plan(choice, sequences)
        self.values = confirmed.values
        self.graph_values = estimate
        if confirmed.improved:
            self.best, self.best_values = self.current, confirmed.values
            self.best_graph_values = estimate
        return self.current

    def candidate_plan(self, move: Move) -> Plan:
        return _plan(*apply(move, self.current.choice, self.current.sequences))

    def _values(self, decoded: Decoded) -> dict[ObjectiveKind, float]:
        return fast_values(self.index, decoded, self.previous_plan, self.churn_tolerance)


def _plan(choice: Mapping[str, str], sequences: Sequences) -> Plan:
    return Plan(choice=dict(choice), sequences={m: tuple(s) for m, s in sequences.items()})


def _play(graph: DisjunctiveGraph, move: Move) -> None:
    """Apply `move` to the graph in place (it evaluated without a cycle just before)."""
    if move.kind == "reassign":
        graph.move_node(move.ops[0], move.machine, move.position)
        return
    for u, v in zip(move.ops[::2], move.ops[1::2], strict=True):
        graph.apply_swap(u, v)


def _past(deadline: float) -> bool:
    return time.monotonic() > deadline


# --- tabu search (Alg 14.3.3) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class _TabuSettings:
    tabu_length: int = DEFAULT_TABU_LENGTH
    iteration_limit: int = DEFAULT_ITERATION_LIMIT

    @classmethod
    def from_params(cls, params: Mapping[str, ParamValue]) -> _TabuSettings:
        _refuse_unknown(params, {"tabu_length", "iteration_limit", "allow_any_tabu_length"})
        allow_any = params.get("allow_any_tabu_length", False)
        if not isinstance(allow_any, bool):
            raise InvalidParamError("allow_any_tabu_length must be true or false")
        length = _int(params, "tabu_length", DEFAULT_TABU_LENGTH, lowest=1)
        if not allow_any and length not in TABU_LENGTHS:
            raise InvalidParamError("tabu_length must be an integer from 5 to 9")
        limit = _int(params, "iteration_limit", DEFAULT_ITERATION_LIMIT, lowest=1)
        return cls(tabu_length=length, iteration_limit=limit)


def tabu_search(
    index: ProblemIndex,
    spec: ObjectiveSpec,
    start: Plan,
    params: Mapping[str, ParamValue],
    deadline: float,
    *,
    record_trace: bool = False,
    previous_plan: PreviousPlan | None = None,
    churn_tolerance: float = 0.0,
) -> TabuResult:
    """Tabu search from `start` (module docstring) until the iteration limit or `deadline`.

    `start` must decode (ValueError otherwise); `InvalidParamError` for a bad param.
    """
    settings = _TabuSettings.from_params(params)
    walk = _Walk(index, spec, start, previous_plan, churn_tolerance)
    tabu: deque[Attribute] = deque(maxlen=settings.tabu_length)
    trace: list[Visit] = []
    for _ in range(settings.iteration_limit):
        ranked = _ranked(walk, deadline)
        visit = None if ranked is None else _tabu_step(walk, ranked, tabu)
        if visit is None:
            break
        if record_trace:
            trace.append(visit)
    return TabuResult(best=walk.best, best_values=walk.best_values, trace=tuple(trace))


def _ranked(walk: _Walk, deadline: float) -> list[_Candidate] | None:
    """Every move with its graph values, best first (ties: move key); None at the deadline."""
    candidates: list[_Candidate] = []
    for move in walk.moves():
        if _past(deadline):
            return None
        values = walk.estimate(move)
        if values is not None:
            candidates.append(_Candidate(move, values))
    delta = walk.scorer.delta

    def compare(a: _Candidate, b: _Candidate) -> int:
        d = delta(a.values, b.values)
        return -1 if d < 0 else int(d > 0)

    return sorted(candidates, key=functools.cmp_to_key(compare))  # stable: moves come by key


def _tabu_step(walk: _Walk, ranked: list[_Candidate], tabu: deque[Attribute]) -> Visit | None:
    """Take the first admissible candidate that decodes; None when none does."""
    for candidate in ranked:
        is_tabu = candidate.move.attribute in tabu
        if is_tabu and not walk.scorer.better(candidate.values, walk.best_graph_values):
            continue
        plan = walk.take(candidate.move, candidate.values)
        if plan is None:
            continue
        tabu.appendleft(_reverse_attribute(candidate.move))
        return Visit(plan=plan, values=walk.values, aspiration=is_tabu)
    return None


def _reverse_attribute(move: Move) -> Attribute:
    """The attribute of the move that undoes `move` (module docstring)."""
    if move.kind == "reassign":
        return (move.ops[0], move.machine)
    return move.attribute


# --- simulated annealing (Alg 14.3.2) -------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class _AnnealSettings:
    iterations: int = DEFAULT_SA_ITERATIONS
    cooling: float = DEFAULT_SA_COOLING

    @classmethod
    def from_params(cls, params: Mapping[str, ParamValue]) -> _AnnealSettings:
        _refuse_unknown(params, {"sa_iterations", "sa_cooling"})
        cooling = params.get("sa_cooling", DEFAULT_SA_COOLING)
        if isinstance(cooling, bool) or not isinstance(cooling, int | float):
            raise InvalidParamError("sa_cooling must be a number between 0 and 1")
        if not 0.0 < cooling < 1.0:
            raise InvalidParamError("sa_cooling must be a number between 0 and 1")
        iterations = _int(params, "sa_iterations", DEFAULT_SA_ITERATIONS, lowest=1)
        return cls(iterations=iterations, cooling=float(cooling))


def simulated_annealing(
    index: ProblemIndex,
    spec: ObjectiveSpec,
    start: Plan,
    params: Mapping[str, ParamValue],
    deadline: float,
    rng: Draws,
    *,
    record_trace: bool = False,
    previous_plan: PreviousPlan | None = None,
    churn_tolerance: float = 0.0,
) -> AnnealResult:
    """Simulated annealing from `start` (module docstring) until the iterations or `deadline`.

    `start` must decode (ValueError otherwise); `InvalidParamError` for a bad param.
    """
    settings = _AnnealSettings.from_params(params)
    walk = _Walk(index, spec, start, previous_plan, churn_tolerance)
    beta = first_beta(walk.scorer, spec, walk.values)
    trace: list[AnnealStep] = []
    for _ in range(settings.iterations):
        if _past(deadline):
            break
        drawn = _draw(walk, rng)
        if drawn is None:
            break
        delta = walk.scorer.delta(drawn.values, walk.graph_values)
        accepted = _accepts(delta, beta, rng) and walk.take(drawn.move, drawn.values) is not None
        if record_trace:
            plan = walk.current if accepted else walk.candidate_plan(drawn.move)
            trace.append(AnnealStep(plan, drawn.values, delta, beta, accepted))
        beta *= settings.cooling
    return AnnealResult(best=walk.best, best_values=walk.best_values, trace=tuple(trace))


def first_beta(scorer: Scorer, spec: ObjectiveSpec, start_values: Values) -> float:
    """beta_1: the delta of a 5 percent worse primary over ln 2 (module docstring)."""
    kind = primary_kind(spec)
    value = start_values[kind]
    worse = dict(start_values)
    worse[kind] = value + signed(kind, WORSE_SHARE * max(abs(value), 1.0))
    delta = scorer.delta(worse, start_values)
    return (delta if delta > 0 else WORSE_SHARE) / math.log(2.0)


def _draw(walk: _Walk, rng: Draws) -> _Candidate | None:
    """One random move with its graph values; None when every move closes a cycle."""
    pool = walk.moves()
    while pool:
        move = pool.pop(rng.randrange(len(pool)))
        values = walk.estimate(move)
        if values is not None:
            return _Candidate(move, values)
    return None


def _accepts(delta: float, beta: float, rng: Draws) -> bool:
    """Not worse, or a draw below exp(-delta / beta); the draw happens for worse only."""
    if delta <= 0:
        return True
    return beta > 0 and rng.random() < math.exp(-delta / beta)


# --- params ---------------------------------------------------------------------------------


def _refuse_unknown(params: Mapping[str, ParamValue], known: set[str]) -> None:
    unknown = sorted(set(params) - known)
    if unknown:
        raise InvalidParamError(f"unknown param {unknown[0]}")


def _int(params: Mapping[str, ParamValue], name: str, default: int, *, lowest: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < lowest:
        raise InvalidParamError(f"{name} must be an integer >= {lowest}")
    return value


# --- start ----------------------------------------------------------------------------------


def _plan_of(schedule: Schedule, index: ProblemIndex) -> Plan | None:
    """The schedule's choice and start-ordered sequences over `index`'s ops, if it decodes."""
    by_op = {a.operation_id: a for a in schedule.assignments}
    if any(op_id not in by_op for op_id in index.op_ids):
        return None
    ordered = sorted((by_op[o] for o in index.op_ids), key=lambda a: (a.start, a.operation_id))
    choice = {a.operation_id: a.machine_id for a in ordered}
    sequences: dict[str, list[str]] = {}
    for a in ordered:
        sequences.setdefault(a.machine_id, []).append(a.operation_id)
    try:
        decoded = decode(index, choice, sequences)
    except ValueError:  # an op on an ineligible machine or before an in-progress op
        return None
    return None if isinstance(decoded, ScheduleIssue) else _plan(choice, sequences)


def _start(
    problem: Problem, index: ProblemIndex, objective: ObjectiveSpec, context: SolveContext
) -> Plan | ScheduleIssue:
    """Warm start, else ATCS through the registry, else the engine with `edd`."""
    if context.warm_start is not None:
        warm = _plan_of(context.warm_start, index)
        if warm is not None:
            return warm
    if registry.is_registered("atcs"):
        atcs = _atcs_plan(problem, index, objective, context)
        if atcs is not None:
            return atcs
    try:
        rule = registry.rule("edd")
    except registry.UnknownRuleError as error:
        return ScheduleIssue(code="subsolver_unavailable", path="problem", message=str(error))
    dispatched = DispatchEngine(index, rule).run()
    if isinstance(dispatched, ScheduleIssue):
        return dispatched
    return _plan(dispatched.choice, dispatched.sequences)


def _atcs_plan(
    problem: Problem, index: ProblemIndex, objective: ObjectiveSpec, context: SolveContext
) -> Plan | None:
    plain = dataclasses.replace(context, deadline=math.inf, warm_start=None, params={})
    schedule = registry.get("atcs").solve(
        problem, objective=objective, budget_seconds=math.inf, context=plain
    )
    return _plan_of(schedule, index)


# --- the solvers ----------------------------------------------------------------------------

Search = Callable[[ProblemIndex, ObjectiveSpec, Plan, float, SolveContext], Plan]
"""(index, objective, start, search deadline, context) -> the best plan."""
Validate = Callable[[Mapping[str, ParamValue]], object]


def _tabu(
    index: ProblemIndex, objective: ObjectiveSpec, start: Plan, deadline: float, ctx: SolveContext
) -> Plan:
    result = tabu_search(
        index,
        objective,
        start,
        ctx.params,
        deadline,
        previous_plan=ctx.previous_plan,
        churn_tolerance=ctx.churn_tolerance,
    )
    return result.best


def _anneal(
    index: ProblemIndex, objective: ObjectiveSpec, start: Plan, deadline: float, ctx: SolveContext
) -> Plan:
    rng = random.Random(ctx.seed)  # noqa: S311 - reproducible search draws, not a secret
    result = simulated_annealing(
        index,
        objective,
        start,
        ctx.params,
        deadline,
        rng,
        previous_plan=ctx.previous_plan,
        churn_tolerance=ctx.churn_tolerance,
    )
    return result.best


class LocalSearchSolver:
    """Tabu search or simulated annealing behind the `Solver` seam (COMP-018)."""

    __slots__ = ("_name", "_search", "_validate")

    def __init__(self, name: str, search: Search, validate: Validate) -> None:
        self._name = name
        self._search = search
        self._validate = validate

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> frozenset[Feature]:
        """Every feature: every kept plan goes through the decoder."""
        return frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        """The best plan found by the search deadline; UNKNOWN with a named issue otherwise."""
        started = time.perf_counter()
        index = ProblemIndex.build(problem)
        result = self._best(problem, index, objective, budget_seconds, context)
        return dispatched_schedule(self._name, result, index, objective, context, started)

    def _best(
        self,
        problem: Problem,
        index: ProblemIndex,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Dispatched | ScheduleIssue:
        try:
            self._validate(context.params)
        except InvalidParamError as error:
            path = f"profile.method_params.{self._name}"
            return ScheduleIssue(code="invalid_method_param", path=path, message=str(error))
        start = _start(problem, index, objective, context)
        if isinstance(start, ScheduleIssue):
            return start
        deadline = context.deadline - (1.0 - SEARCH_SHARE) * budget_seconds
        best = self._search(index, objective, start, deadline, context)
        decoded = decode(index, best.choice, best.sequences)
        if isinstance(decoded, ScheduleIssue):
            raise AssertionError(f"the best plan decoded before: {decoded.code}")
        return Dispatched(choice=best.choice, sequences=best.sequences, decoded=decoded)


SOLVERS = (
    SolverSpec(
        name=TABU,
        family="improvement",
        factory=functools.partial(LocalSearchSolver, TABU, _tabu, _TabuSettings.from_params),
        deterministic=True,
        capabilities=frozenset(Feature),
    ),
    SolverSpec(
        name=SA,
        family="improvement",
        factory=functools.partial(LocalSearchSolver, SA, _anneal, _AnnealSettings.from_params),
        deterministic=False,
        capabilities=frozenset(Feature),
    ),
)
"""Registry entries (D-069)."""
