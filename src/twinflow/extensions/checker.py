"""COMP-156 / COMP-161: run every extension on sample inputs and report problems in plain
words (REQ-203, REQ-213).

The samples (`step_samples`) are up to `samples` states from one seeded episode of the
lowest-cover rule plus three edge states (empty stock, full storage, last day), the moves
of that episode, the model facts for demand files, and a few dispatch decisions. Each kind
is called on the inputs it takes:

- policy       `decide(state)` on every state (dispatch files on the dispatch states), then
               once per grid point of `SETTINGS` on a few states
- reward       `reward(move)` on every move
- view         `label(state)`, feature `features(state)`, observation `observe(state)`
- demand       `demand(period, rng, model)` for every period of one seeded run
- engine       see `check_engine` (T-263)

Problem codes: `raised` (with the line inside the user's file), `bad_action`,
`bad_value`, `bad_settings`, `slow` (mean over `time_limit_ms`, D-147), `bad_engine`.
One problem per file: the first one found. The loader's problems come first.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import product
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import numpy as np

from twinflow.engines.contract import run_episode
from twinflow.engines.step.engine import StepEngine
from twinflow.engines.step.model import StepModel
from twinflow.engines.step.rules import make_decider
from twinflow.extensions.kinds import KINDS, check_return
from twinflow.extensions.loader import Extension, Extensions, Problem, line_in
from twinflow.extensions.view import (
    DispatchState,
    ModelFacts,
    PolicyState,
    dispatch_state,
    policy_state,
)
from twinflow.policy import DISPATCH_NAMES
from twinflow.state.records import Move, Snapshot

if TYPE_CHECKING:
    from twinflow.extensions.project import Project

# The clock the slow check reads. Tests swap it for a fake so they never depend on machine speed.
CLOCK: Callable[[], float] = time.perf_counter
GRID_STATES = 3  # states tried at each grid point of a policy's SETTINGS


@dataclass(frozen=True)
class CheckLimits:
    """How hard the checker looks (`experiment.checker`, REQ-221)."""

    time_limit_ms: float = 20.0  # D-147
    samples: int = 200


def _dispatch_samples() -> tuple[DispatchState, ...]:
    jobs = [
        {"order_id": "WO-1", "thing": "part", "qty": 5, "due_date": 7200.0, "priority": 0,
         "arrival_time": 0.0},
        {"order_id": "WO-2", "thing": "part", "qty": 1, "due_date": None, "priority": 1,
         "arrival_time": 60.0},
    ]  # fmt: skip
    return (
        dispatch_state("cell", [], time=0.0, current="fifo"),
        dispatch_state("cell", jobs[:1], time=30.0, current="fifo"),
        dispatch_state("cell", jobs, time=120.0, current="edd"),
    )


@dataclass(frozen=True)
class Samples:
    """The inputs every kind is tried on."""

    states: tuple[PolicyState, ...]
    moves: tuple[Move, ...]
    facts: ModelFacts
    periods: int
    seed: int = 0
    dispatch: tuple[DispatchState, ...] = field(default_factory=_dispatch_samples)
    engine_model: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


def _edge_states(model: StepModel) -> list[Snapshot]:
    first = model.product_ids[0]
    setup = {model.line_id: first}
    empty = {p.id: 0.0 for p in model.products}
    full = {p.id: float(p.max_storage) for p in model.products}
    half = {p.id: float(p.max_storage // 2) for p in model.products}
    last = model.horizon - 1
    return [
        Snapshot(t=0.0, period=0, stock=empty, setup=setup),
        Snapshot(t=0.0, period=0, stock=full, setup=setup),
        Snapshot(t=float(last), period=last, stock=half, setup=setup),
    ]


def step_samples(
    model: StepModel,
    *,
    count: int = 200,
    seed: int = 0,
    features: Callable[[PolicyState], Mapping[str, float]] | None = None,
) -> Samples:
    """Samples for a make-to-stock model: one seeded lowest-cover episode plus edge states.

    `features` adds feature values to each state, so policies see what they will see in a
    race (REQ-219).
    """
    engine = StepEngine(model)
    moves = run_episode(engine, make_decider("lowest_cover", model), seed)
    snapshots = [m.before for m in moves][: max(0, count - 3)] + _edge_states(model)
    facts = ModelFacts.from_step_model(model)
    actions = engine.actions(snapshots[0])
    states = []
    for snapshot in snapshots:
        state = policy_state(snapshot, facts, actions)
        if features is not None:
            state = state.replace(features=features(state))
        states.append(state)
    return Samples(tuple(states), tuple(moves), facts, model.horizon, seed)


# --- running one extension ---------------------------------------------------------------------


class ExtensionFailed(Exception):
    """An extension raised or returned a bad value; `problem` says where and how to fix it."""

    def __init__(self, problem: Problem) -> None:
        super().__init__(str(problem))
        self.problem = problem


def _problem(ext: Extension, code: str, message: str, fix: str, line: int | None) -> Problem:
    return Problem(ext.kind, code, message, fix, ext.file, line, ext.name)


def _where(args: Sequence[Any]) -> str:
    """A few plain words about the sample that broke the extension."""
    first = args[0] if args else None
    if isinstance(first, PolicyState):
        return f" on the state for day {first.period}"
    if isinstance(first, Move):
        return f" on the move for day {first.index}"
    if isinstance(first, DispatchState):
        return f" on a dispatch decision with {len(first.queue)} waiting job(s)"
    if isinstance(first, int):
        return f" for period {first}"
    return ""


def _call(ext: Extension, args: Sequence[Any], settings: Mapping[str, Any], code: str) -> Any:
    try:
        return ext(*args, **settings)
    except Exception as exc:  # user code: any error becomes a problem
        line = line_in(ext.file, exc)
        shown = ", ".join(f"{k}={v!r}" for k, v in settings.items())
        with_settings = f" with {shown}" if shown else ""
        raise ExtensionFailed(
            _problem(
                ext,
                code,
                f"{KINDS[ext.kind].function} raised {type(exc).__name__}: {exc}"
                f"{_where(args)}{with_settings}.",
                f"Fix line {line} so it works for every input, for example empty stock."
                if line is not None
                else "Make it work for every input, for example empty stock.",
                line,
            )
        ) from exc


Validate = Callable[[Any, Sequence[Any]], str | None]


def call_checked(
    ext: Extension,
    args: Sequence[Any],
    settings: Mapping[str, Any] | None = None,
    *,
    validate: Validate | None = None,
) -> Any:
    """Call an extension the way the race does: a raise or a bad return value becomes an
    `ExtensionFailed` carrying a plain-word `Problem` (REQ-213)."""
    value = _call(ext, args, settings or {}, "raised")
    if validate is not None:
        _validate(ext, value, args, validate)
    return value


def _run(
    ext: Extension,
    inputs: Sequence[Sequence[Any]],
    validate: Validate,
    limits: CheckLimits,
) -> list[Problem]:
    """Call `ext` on every input; the first problem found, or a slow problem, or none."""
    try:
        spent = 0.0
        for args in inputs:
            started = CLOCK()
            value = _call(ext, args, {}, "raised")
            spent += CLOCK() - started
            _validate(ext, value, args, validate)
        _grid(ext, inputs[:GRID_STATES], validate)
    except ExtensionFailed as stop:
        return [stop.problem]
    mean_ms = 1000.0 * spent / max(1, len(inputs))
    if mean_ms > limits.time_limit_ms:
        function = KINDS[ext.kind].function
        return [
            _problem(
                ext,
                "slow",
                f"{function} took {mean_ms:.1f} ms on average; the limit is "
                f"{limits.time_limit_ms:g} ms.",
                f"Make {function} do less work each time, or raise "
                "experiment.checker.time_limit_ms in the capsule.",
                ext.line,
            )
        ]
    return []


def _grid(ext: Extension, inputs: Sequence[Sequence[Any]], validate: Validate) -> None:
    keys = list(ext.grid)
    for values in product(*(ext.grid[k] for k in keys)):
        settings = dict(zip(keys, values, strict=True))
        for args in inputs:
            value = _call(ext, args, settings, "bad_settings")
            wrong = validate(value, args)
            if wrong is not None:
                shown = ", ".join(f"{k}={v!r}" for k, v in settings.items())
                raise ExtensionFailed(
                    _problem(
                        ext,
                        "bad_settings",
                        f"With {shown}: {wrong}",
                        "Remove that value from SETTINGS, or make decide work with it.",
                        ext.line,
                    )
                )


def _validate(ext: Extension, value: Any, args: Sequence[Any], validate: Validate) -> None:
    """Raise `ExtensionFailed` when `value` is not a good return for `ext`."""
    wrong = validate(value, args)
    if wrong is not None:
        code = "bad_action" if ext.kind == "policy" else "bad_value"
        raise ExtensionFailed(_problem(ext, code, wrong, _fix(ext), ext.line))


def _fix(ext: Extension) -> str:
    kind = KINDS[ext.kind]
    return f"Make {kind.function} return {kind.returns}."


def _policy_inputs(ext: Extension, samples: Samples) -> tuple[list[tuple[Any, ...]], Validate]:
    if ext.decision == "dispatch":
        return [(s,) for s in samples.dispatch], lambda value, args: check_return(
            "policy", value, allowed=args[0].actions or DISPATCH_NAMES
        )
    return [(s,) for s in samples.states], lambda value, args: check_return(
        "policy", value, allowed=args[0].actions
    )


def observation_validator(value: Any, args: Sequence[Any]) -> str | None:
    if isinstance(value, PolicyState):
        return None
    return (
        f"observe returned {type(value).__name__}; it must return a policy state, "
        "for example state.replace(setup=None)."
    )


def check_extension(
    ext: Extension, samples: Samples, limits: CheckLimits | None = None
) -> list[Problem]:
    """The problems of one loaded extension on the samples."""
    limits = limits or CheckLimits()
    if ext.kind == "engine":
        return check_engine(ext, samples.engine_model, samples.seed, limits)
    if ext.kind == "policy":
        inputs, validate = _policy_inputs(ext, samples)
        return _run(ext, inputs, validate, limits)
    if ext.kind == "reward":
        return _run(ext, [(m,) for m in samples.moves], _plain("reward"), limits)
    if ext.kind in ("view", "feature"):
        return _run(ext, [(s,) for s in samples.states], _plain(ext.kind), limits)
    if ext.kind == "observation":
        return _run(ext, [(s,) for s in samples.states], observation_validator, limits)
    if ext.kind == "demand":
        rng = np.random.default_rng(samples.seed)
        products = samples.facts.products
        calls = [(period, rng, samples.facts) for period in range(samples.periods)]
        return _run(ext, calls, demand_validator(products), limits)
    return []


def demand_validator(products: Sequence[str]) -> Validate:
    return lambda value, args: check_return("demand", value, products=products)


def _plain(kind: str) -> Validate:
    return lambda value, args: check_return(kind, value)


def check_extensions(
    extensions: Extensions, samples: Samples, limits: CheckLimits | None = None
) -> list[Problem]:
    """The loader's problems, then every extension's problems (REQ-203)."""
    found = list(extensions.problems)
    for ext in extensions.all():
        if ext.kind == "engine" and not samples.engine_model:
            continue  # an engine is checked by the project that uses it (T-263)
        found.extend(check_extension(ext, samples, limits))
    return found


# --- engines (T-263) ---------------------------------------------------------------------------

ENGINE_METHODS = ("start", "actions", "step", "done")
_ENGINE_FIX = {
    "start": "Make start(seed, from_state=None) return a Snapshot.",
    "actions": "Make actions(state) return a list of action names, like ('run', 'idle').",
    "step": "Make step(action) return a Move.",
    "done": "Make done() return True or False.",
}


def _engine_problem(ext: Extension, message: str, fix: str, line: int | None = None) -> Problem:
    return _problem(ext, "bad_engine", message, fix, line if line is not None else ext.line)


def _engine_call(ext: Extension, method: str, call: Callable[..., Any], *args: Any) -> Any:
    """Call one engine method; a raise becomes a problem at the user's line."""
    try:
        return call(*args)
    except Exception as exc:  # user code: any error becomes a problem
        line = line_in(ext.file, exc)
        raise ExtensionFailed(
            _problem(
                ext,
                "raised",
                f"Engine.{method} raised {type(exc).__name__}: {exc}.",
                f"Fix line {line} of the engine file." if line else "Fix the engine file.",
                line or ext.line,
            )
        ) from exc


def _engine_value(ext: Extension, method: str, value: Any, ok: bool) -> None:
    if not ok:
        raise ExtensionFailed(
            _engine_problem(ext, f"Engine.{method} returned {value!r}.", _ENGINE_FIX[method])
        )


def run_engine_check(ext: Extension, model: Mapping[str, Any], seed: int, steps: int) -> None:
    """Build the engine from the capsule model and run one short random episode; raises
    `ExtensionFailed` with the first problem."""
    engine = _engine_call(ext, "__init__", ext, dict(model))
    rng = np.random.default_rng(seed)
    state = _engine_call(ext, "start", engine.start, seed)
    _engine_value(ext, "start", state, isinstance(state, Snapshot))
    for _ in range(steps):
        finished = _engine_call(ext, "done", engine.done)
        _engine_value(ext, "done", finished, isinstance(finished, bool | np.bool_))
        if finished:
            return
        actions = _engine_call(ext, "actions", engine.actions, state)
        good = isinstance(actions, list | tuple) and bool(actions)
        _engine_value(ext, "actions", actions, good and all(isinstance(a, str) for a in actions))
        action = actions[int(rng.integers(len(actions)))]
        move = _engine_call(ext, "step", engine.step, action)
        _engine_value(ext, "step", move, isinstance(move, Move))
        state = move.after


def check_engine(
    ext: Extension, model: Mapping[str, Any], seed: int = 0, limits: CheckLimits | None = None
) -> list[Problem]:
    """An engine class must have the contract methods (REQ-194), build from the capsule's
    model and run a short episode with random actions (REQ-223)."""
    limits = limits or CheckLimits()
    missing = [m for m in ENGINE_METHODS if not callable(getattr(ext.target, m, None))]
    if missing:
        return [
            _engine_problem(
                ext,
                f"The Engine class has no {missing[0]} method.",
                f"Add `def {missing[0]}(self, ...)` to the class; an engine needs "
                f"{', '.join(ENGINE_METHODS)}.",
            )
        ]
    try:
        run_engine_check(ext, model, seed, limits.samples)
    except ExtensionFailed as failed:
        return [failed.problem]
    return []


def engine_samples(project: Project, count: int, seed: int) -> Samples:
    """Samples for a custom engine: one episode with random actions (T-263)."""
    engine = project.engine()
    rng = np.random.default_rng(seed)
    snapshot = engine.start(seed)
    states: list[PolicyState] = []
    moves: list[Move] = []
    while not engine.done() and len(moves) < count:
        actions = engine.actions(snapshot)
        states.append(project.policy_state(snapshot, actions))
        move = engine.step(actions[int(rng.integers(len(actions)))])
        moves.append(move)
        snapshot = move.after
    periods = project.facts.horizon or len(moves)
    return Samples(
        tuple(states), tuple(moves), project.facts, periods, seed,
        engine_model=MappingProxyType(dict(project.capsule.model)),
    )  # fmt: skip


def check_project(project: Project) -> list[Problem]:
    """Everything wrong with a project, in plain words (REQ-203, REQ-222): its setting
    problems, then the extension files, each run on samples with the chosen features and the
    capsule's `experiment.checker` limits."""
    config = project.config
    limits = config.checker
    if project.model is None:
        # A custom engine was checked when the project opened. Built-ins are written for
        # make-to-stock twins, so only the project's own files are run on its states.
        samples = engine_samples(project, limits.samples, config.race.seed)
        extensions = project.extensions
        checked = [
            p
            for ext in extensions.all()
            if ext.kind != "engine" and ext.source == "project"
            for p in check_extension(ext, samples, limits)
        ]
        return [*project.problems, *extensions.problems, *checked]
    samples = step_samples(
        project.model,
        count=limits.samples,
        seed=config.race.seed,
        features=project.features if config.features else None,
    )
    return [*project.problems, *check_extensions(project.extensions, samples, limits)]
