"""COMP-157: race every policy on the same demand and rank them (REQ-206, REQ-208).

`race(project, policies)` runs each policy file of a project on the same draws (common
random numbers, D-135, D-153): one `EpisodePlan` per seed, shared by every policy. For each
policy it keeps:

- the score (the capsule's `experiment.score` reward, REQ-217) and the dollars (the ledger
  total) per episode, both always (D-152), with mean, spread and a t-interval at
  `experiment.race.interval`;
- the paired difference of each episode's score against the best policy, with its interval
  (D-144 uses it to promote a candidate);
- the dollars by cause (revenue, production, setup, holding, lost sales, late) per episode;
- fill rate, changeovers and cross-family changeovers per episode;
- the score and dollars of every day, so the cumulative profit, its bands, the rank on each
  day and the first day one policy passes another can be read (RL repo notebook 1).

A broken policy (it will not load, it raises, it returns a bad action) or a slow one (one
decision over `experiment.race.decision_timeout_s`) is listed in `skipped` and `problems`
and left out of the ranking; it never stops the race (REQ-213).

The moves of the best `experiment.race.top_policies` policies are kept so `write` can save
them as `policies/<name>/moves.parquet` next to `race.json`.

Only the CLI and in-process Python may run a race: it loads local code (D-139).
"""

from __future__ import annotations

import json
import math
import signal
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from itertools import product
from pathlib import Path
from types import FrameType, MappingProxyType
from typing import Any

import numpy as np
import numpy.typing as npt
import polars as pl
import scipy.stats  # type: ignore[import-untyped]  # no stub; D-036 interval library

from twinflow.engines.contract import Decide, Engine
from twinflow.extensions.checker import ExtensionFailed, call_checked
from twinflow.extensions.kinds import check_return
from twinflow.extensions.loader import Extension, Problem
from twinflow.extensions.project import EpisodePlan, Project, ProjectError, RaceSettings
from twinflow.state.issues import did_you_mean
from twinflow.state.markov import ChainCounter, MarkovChain
from twinflow.state.recorder import StateRecorder
from twinflow.state.records import LEDGER_PARTS, Move, flatten, unflatten

RACE_FILE = "race.json"
SCENARIO_FILE = "scenario.json"
POLICY_FOLDER = "policies"  # under the race folder: policies/<name>/moves.parquet
FloatArray = npt.NDArray[np.float64]
_EMPTY: Mapping[str, Any] = MappingProxyType({})


# --- small statistics --------------------------------------------------------------------------


@dataclass(frozen=True)
class Stat:
    """Mean, spread (sample standard deviation) and a t-interval of per-episode values.
    With one episode the spread and interval are None."""

    mean: float
    std: float | None
    low: float | None
    high: float | None
    n: int

    def to_json(self) -> dict[str, Any]:
        return {"mean": self.mean, "std": self.std, "low": self.low, "high": self.high, "n": self.n}


def stat(values: Sequence[float], level: float = 0.95) -> Stat:
    """Mean, sample standard deviation and a two-sided t-interval at `level`."""
    data = np.asarray(values, dtype=float)
    n = len(data)
    if n == 0:
        raise ValueError("no values to summarize")
    mean = float(data.mean())
    if n == 1:
        return Stat(mean, None, None, None, 1)
    std = float(data.std(ddof=1))
    half = float(scipy.stats.t.ppf((1.0 + level) / 2.0, n - 1)) * std / math.sqrt(n)
    return Stat(mean, std, mean - half, mean + half, n)


@dataclass(frozen=True)
class Band:
    """Mean and sample standard deviation of the running total on each day (the bands)."""

    mean: tuple[float, ...]
    std: tuple[float, ...]

    def to_json(self) -> dict[str, Any]:
        return {"mean": list(self.mean), "std": list(self.std)}


def band(daily: FloatArray) -> Band:
    """The band of the cumulative sum of `daily` (episodes x days)."""
    cum = np.cumsum(daily, axis=1)
    std = cum.std(axis=0, ddof=1) if cum.shape[0] > 1 else np.zeros(cum.shape[1])
    return Band(tuple(float(x) for x in cum.mean(axis=0)), tuple(float(x) for x in std))


def rank_by_day(cumulative: Mapping[str, Sequence[float]]) -> dict[str, list[int]]:
    """Rank 1 = most so far, on every day; a tie shares the best rank (RL repo
    `rank_over_time`, method "min")."""
    names = list(cumulative)
    if not names:
        return {}
    table = np.array([cumulative[n] for n in names], dtype=float)
    ranks = 1 + (table[None, :, :] > table[:, None, :]).sum(axis=1)
    return {n: [int(r) for r in ranks[i]] for i, n in enumerate(names)}


def first_day_passed(
    cumulative: Mapping[str, Sequence[float]], leader: str, challenger: str
) -> int | None:
    """The first day (counting from 1) the challenger's running total is ahead of the
    leader's, or None when it never is (RL repo `first_day_passed`)."""
    ahead = np.flatnonzero(np.asarray(cumulative[challenger]) > np.asarray(cumulative[leader]))
    return int(ahead[0]) + 1 if len(ahead) else None


# --- one policy's results ----------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class PolicyResult:
    """One policy (under one observation) over every race episode."""

    policy: str
    observation: str
    settings: Mapping[str, Any]
    source: str
    file: str
    episode_score: tuple[float, ...]
    episode_dollars: tuple[float, ...]
    daily_score: FloatArray  # episodes x days
    daily_dollars: FloatArray
    parts: Mapping[str, float]  # mean dollars per episode by cause
    fill_rate: float | None
    changeovers: float  # per episode
    cross_family: float  # per episode
    idle_share: float  # share of decisions that rest the line
    score: Stat
    dollars: Stat
    cumulative_score: Band
    cumulative_dollars: Band
    rank: int = 0
    vs_best: Stat | None = None
    dollar_warning: str | None = None
    leaks: tuple[Leak, ...] = ()
    extra: Mapping[str, Any] = field(default=_EMPTY)

    def to_json(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "observation": self.observation,
            "rank": self.rank,
            "settings": dict(self.settings),
            "source": self.source,
            "file": self.file,
            "score": self.score.to_json(),
            "dollars": self.dollars.to_json(),
            "vs_best": None if self.vs_best is None else self.vs_best.to_json(),
            "dollar_warning": self.dollar_warning,
            "parts": dict(self.parts),
            "fill_rate": self.fill_rate,
            "changeovers": self.changeovers,
            "cross_family": self.cross_family,
            "idle_share": self.idle_share,
            "money_leaks": [leak.to_json() for leak in self.leaks],
            "episode_score": list(self.episode_score),
            "episode_dollars": list(self.episode_dollars),
            "cumulative_score": self.cumulative_score.to_json(),
            "cumulative_dollars": self.cumulative_dollars.to_json(),
            **{k: v for k, v in self.extra.items()},
        }


@dataclass(frozen=True)
class Trial:
    """One tuning trial: a policy at one grid point of its SETTINGS, on the tuning draws
    (D-143). `score` and `dollars` are means per episode; None when the trial broke."""

    policy: str
    settings: Mapping[str, Any]
    score: float | None
    dollars: float | None
    problem: Problem | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "settings": dict(self.settings),
            "score": self.score,
            "dollars": self.dollars,
            "problem": None if self.problem is None else self.problem.to_json(),
        }


def grid_points(grid: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    """Every combination of a SETTINGS grid, in file order (the last name changes fastest,
    like `itertools.product` and the RL repo's `grid_settings`)."""
    keys = list(grid)
    return [dict(zip(keys, values, strict=True)) for values in product(*grid.values())]


LEAK_PARTS = ("setup", "holding", "lost_sales", "late")  # the dollars a plant can win back
LEAK_WORDS = {
    "setup": "changeovers",
    "holding": "keeping stock",
    "lost_sales": "lost sales",
    "late": "late orders",
}
TOP_LEAKS = 5


@dataclass(frozen=True)
class Leak:
    """One grouped state where a policy loses money (REQ-208): dollars lost per run while
    in the state, split by cause (changeovers, holding, lost sales, late)."""

    state: str
    visits: int
    share: float  # share of all days spent in the state
    per_visit: float  # dollars lost on one day in the state
    per_run: float  # dollars lost in the state over one run
    parts: Mapping[str, float]  # per run, by cause
    cause: str  # the biggest part

    @property
    def cause_words(self) -> str:
        return LEAK_WORDS.get(self.cause, self.cause)

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "visits": self.visits,
            "share": self.share,
            "per_visit": self.per_visit,
            "per_run": self.per_run,
            "parts": dict(self.parts),
            "cause": self.cause,
            "cause_words": self.cause_words,
        }


def money_leaks(chain: MarkovChain, episodes: int, top: int = TOP_LEAKS) -> list[Leak]:
    """The `top` states that lose the most dollars per run, from a chain's money table
    (every visited state, not only the map's top states)."""
    leaks = []
    for state, money in chain.money.items():
        parts = {p: money.visits * getattr(money, p) / max(1, episodes) for p in LEAK_PARTS}
        per_run = sum(parts.values())
        leaks.append(
            Leak(
                state=state,
                visits=money.visits,
                share=chain.share.get(state, 0.0),
                per_visit=per_run * max(1, episodes) / money.visits,
                per_run=per_run,
                parts=MappingProxyType(parts),
                cause=max(parts, key=lambda p: parts[p]),
            )
        )
    leaks.sort(key=lambda leak: -leak.per_run)  # stable: ties keep the chain's order
    return leaks[:top]


# --- running one policy ------------------------------------------------------------------------


class DecisionTimeout(BaseException):
    """One decision ran past the race's limit. A BaseException, so a policy's own
    `except Exception` cannot swallow it."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"one decision took {seconds:.2f} s")
        self.seconds = seconds


def _alarm(signum: int, frame: FrameType | None) -> None:
    raise DecisionTimeout(math.inf)


@contextmanager
def _alarm_handler() -> Iterator[bool]:
    """Install the SIGALRM handler for the race when this is the main thread of a Unix
    process (so a hung decision is stopped); yields whether it did."""
    usable = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    if not usable:
        yield False
        return
    previous = signal.signal(signal.SIGALRM, _alarm)
    try:
        yield True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def timed(decide: Decide, limit_s: float, alarm: bool) -> Decide:
    """`decide` stopped after `limit_s` seconds on one decision (by an alarm when it can,
    and by the clock after it returns in any case)."""

    def run(snapshot: Any) -> str:
        started = time.perf_counter()
        if alarm:
            signal.setitimer(signal.ITIMER_REAL, limit_s)
            try:
                action = decide(snapshot)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        else:
            action = decide(snapshot)
        spent = time.perf_counter() - started
        if spent > limit_s:
            raise DecisionTimeout(spent)
        return action

    return run


def _episode(engine: Engine, decide: Decide, seed: int) -> list[Move]:
    state = engine.start(seed)
    moves: list[Move] = []
    while not engine.done():
        move = engine.step(decide(state))
        moves.append(move)
        state = move.after
    return moves


def iter_episodes(
    project: Project,
    policy: Extension,
    plan: EpisodePlan,
    *,
    settings: Mapping[str, Any] | None = None,
    observation: str | None = None,
    timeout_s: float | None = None,
) -> Iterator[list[Move]]:
    """The episodes of `plan` with one policy, one list of moves at a time, so a caller
    that only sums them never holds a whole race (the race streams, COMP-157).

    Raises `ExtensionFailed` when the policy raises or returns a bad action and
    `DecisionTimeout` when one decision takes longer than `timeout_s`."""
    decide = project.decider(policy, plan.engine, settings=settings, observation=observation)
    if timeout_s is None:
        for seed in plan.seeds:
            yield _episode(plan.engine, decide, seed)
        return
    with _alarm_handler() as alarm:
        guarded = timed(decide, timeout_s, alarm)
        for seed in plan.seeds:
            yield _episode(plan.engine, guarded, seed)


def run_policy(
    project: Project,
    policy: Extension,
    plan: EpisodePlan,
    *,
    settings: Mapping[str, Any] | None = None,
    observation: str | None = None,
    timeout_s: float | None = None,
) -> list[list[Move]]:
    """Every episode of `plan` with one policy, as lists of moves (in-process Python).
    Holds them all: for long runs use `iter_episodes`."""
    return list(
        iter_episodes(
            project,
            policy,
            plan,
            settings=settings,
            observation=observation,
            timeout_s=timeout_s,
        )
    )


class MoveTable:
    """Moves kept as polars columns (one `flatten` row per move plus `episode`), far
    smaller than `Move` objects; `moves()` builds the objects again on demand."""

    def __init__(self) -> None:
        self._parts: list[pl.DataFrame] = []
        self._frame: pl.DataFrame | None = None

    def add(self, moves: Sequence[Move], episode: int) -> None:
        if moves:
            rows = [{"episode": episode, **flatten(m)} for m in moves]
            self._parts.append(pl.from_dicts(rows, infer_schema_length=None))
            self._frame = None

    @property
    def frame(self) -> pl.DataFrame:
        if self._frame is None:
            self._frame = (
                pl.concat(self._parts, how="diagonal_relaxed")
                if self._parts
                else pl.DataFrame(schema={"episode": pl.Int64})
            )
            self._parts = [self._frame]
        return self._frame

    def __len__(self) -> int:
        return self.frame.height

    def episodes(self) -> Iterator[tuple[int, list[Move]]]:
        """(episode, moves) in order, one episode at a time."""
        frame = self.frame
        if frame.height == 0:
            return
        for (episode,), part in frame.group_by("episode", maintain_order=True):
            yield int(episode), [unflatten(row) for row in part.to_dicts()]

    def moves(self) -> tuple[Move, ...]:
        return tuple(unflatten(row) for row in self.frame.to_dicts())


def _score_check(value: Any, args: Sequence[Any]) -> str | None:
    return check_return("reward", value)


@dataclass
class _Tally:
    """Running sums over a policy's episodes."""

    project: Project
    reward: Extension
    score_rows: list[list[float]] = field(default_factory=list)
    dollar_rows: list[list[float]] = field(default_factory=list)
    parts: dict[str, float] = field(default_factory=lambda: dict.fromkeys(LEDGER_PARTS, 0.0))
    sold: float = 0.0
    asked: float = 0.0
    switches: int = 0
    cross: int = 0
    idle: int = 0
    decisions: int = 0

    def add(self, moves: Sequence[Move]) -> None:
        family = self.project.facts.family
        scores, dollars = [], []
        for move in moves:
            try:
                score = call_checked(self.reward, (move,), validate=_score_check)
            except ExtensionFailed as failed:
                raise ProjectError([failed.problem]) from None
            scores.append(float(score))
            ledger = move.ledger
            dollars.append(ledger.total)
            for part in LEDGER_PARTS:
                self.parts[part] += getattr(ledger, part)
            for key, value in move.events.items():
                if key.startswith("sold."):
                    self.sold += value
                elif key.startswith("demand."):
                    self.asked += value
            self.decisions += 1
            self.idle += move.action == "idle"
            for line, before in move.before.setup.items():
                after = move.after.setup.get(line)
                if after != before and after is not None:
                    self.switches += 1
                    if before is None or family.get(before, before) != family.get(after, after):
                        self.cross += 1
        self.score_rows.append(scores)
        self.dollar_rows.append(dollars)


def _matrix(rows: Sequence[Sequence[float]]) -> FloatArray:
    """Episodes x days; a shorter episode is padded with zero days (its total holds)."""
    days = max((len(r) for r in rows), default=0)
    out = np.zeros((len(rows), days))
    for i, row in enumerate(rows):
        out[i, : len(row)] = row
    return out


def _result(
    tally: _Tally,
    policy: Extension,
    observation: str,
    settings: Mapping[str, Any],
    level: float,
) -> PolicyResult:
    episodes = len(tally.score_rows)
    daily_score, daily_dollars = _matrix(tally.score_rows), _matrix(tally.dollar_rows)
    episode_score = tuple(float(x) for x in daily_score.sum(axis=1))
    episode_dollars = tuple(float(x) for x in daily_dollars.sum(axis=1))
    return PolicyResult(
        policy=policy.name,
        observation=observation,
        settings=MappingProxyType(dict(settings)),
        source=policy.source,
        file=str(policy.file),
        episode_score=episode_score,
        episode_dollars=episode_dollars,
        daily_score=daily_score,
        daily_dollars=daily_dollars,
        parts=MappingProxyType({p: v / episodes for p, v in tally.parts.items()}),
        fill_rate=tally.sold / tally.asked if tally.asked > 0 else None,
        changeovers=tally.switches / episodes,
        cross_family=tally.cross / episodes,
        idle_share=tally.idle / max(1, tally.decisions),
        score=stat(episode_score, level),
        dollars=stat(episode_dollars, level),
        cumulative_score=band(daily_score),
        cumulative_dollars=band(daily_dollars),
    )


# --- the race ----------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class RaceResult:
    """Everything one race found (COMP-157). `to_json()` is `race.json`."""

    project: Project
    settings: RaceSettings
    score_name: str
    observations: tuple[str, ...]
    results: tuple[PolicyResult, ...]
    skipped: tuple[Problem, ...] = ()
    problems: tuple[Problem, ...] = ()
    kept: Mapping[str, MoveTable] = field(default=_EMPTY)
    trials: tuple[Trial, ...] = ()
    chains: Mapping[str, MarkovChain] = field(default=_EMPTY)

    @property
    def observation(self) -> str:
        """The observation the main ranking uses (the first one raced)."""
        return self.observations[0]

    def ranking(self, observation: str | None = None) -> list[PolicyResult]:
        """The policies raced under one observation (default: the first), best score first."""
        seen = self.observation if observation is None else observation
        return sorted((r for r in self.results if r.observation == seen), key=lambda r: r.rank)

    def result(self, policy: str, observation: str | None = None) -> PolicyResult:
        seen = self.observation if observation is None else observation
        for row in self.results:
            if row.policy == policy and row.observation == seen:
                return row
        known = sorted({r.policy for r in self.results})
        raise KeyError(f"no result for {policy!r} under {seen!r}. {did_you_mean(policy, known)}")

    def moves(self, policy: str) -> tuple[Move, ...]:
        """The moves of one of the top policies (first observation), in episode order,
        built again from their compact columns."""
        if policy not in self.kept:
            kept = ", ".join(self.kept) or "none"
            raise KeyError(f"the moves of {policy!r} were not kept; kept: {kept}")
        return self.kept[policy].moves()

    def cumulative(
        self, measure: str = "score", observation: str | None = None
    ) -> dict[str, tuple[float, ...]]:
        """Mean running total per day of each policy (`measure` is score or dollars)."""
        pick = (lambda r: r.cumulative_score) if measure == "score" else _dollar_band
        return {r.policy: pick(r).mean for r in self.ranking(observation)}

    def rank_by_day(
        self, measure: str = "score", observation: str | None = None
    ) -> dict[str, list[int]]:
        return rank_by_day(self.cumulative(measure, observation))

    def first_day_passed(self, leader: str, challenger: str, measure: str = "score") -> int | None:
        return first_day_passed(self.cumulative(measure), leader, challenger)

    @property
    def early_leader(self) -> str | None:
        """The policy ranked first after day 1 (the story of notebook 1: early leaders are
        not always late leaders)."""
        return _early_leader(self.rank_by_day())

    def profit_by_view(self, measure: str = "dollars") -> dict[str, dict[str, float]]:
        """Mean dollars (or score) per run of each policy under each observation, in race
        order (REQ-226: what information is worth in dollars; RL repo notebook 2)."""
        table: dict[str, dict[str, float]] = {}
        for row in self.results:
            value = row.dollars.mean if measure == "dollars" else row.score.mean
            table.setdefault(row.policy, {})[row.observation] = value
        return table

    def to_json(self) -> dict[str, Any]:
        settings = self.settings
        ranking = self.ranking()
        ranks = self.rank_by_day()
        leader = _early_leader(ranks)
        cumulative = self.cumulative()
        rows = []
        for row in self.results:
            data = row.to_json()
            if row.observation == self.observation:
                data["rank_by_day"] = ranks.get(row.policy, [])
                data["passes_early_leader_on"] = (
                    None
                    if leader is None or leader == row.policy
                    else first_day_passed(cumulative, leader, row.policy)
                )
            rows.append(data)
        return {
            "about": "A twinflow race (TF-006 COMP-157). Every policy ran on the same demand.",
            "project": str(self.project.capsule_file),
            "score": self.score_name,
            "settings": {
                "episodes": settings.episodes,
                "seed": settings.seed,
                "tune_episodes": settings.tune_episodes,
                "tune_seed": settings.tune_seed,
                "interval": settings.interval,
                "top_policies": settings.top_policies,
                "decision_timeout_s": settings.decision_timeout_s,
            },
            "observations": list(self.observations),
            "early_leader": leader,
            "ranking": [r.policy for r in ranking],
            "results": rows,
            "profit_by_view": self.profit_by_view(),
            "tuning": [t.to_json() for t in self.trials],
            "markov": {name: chain_json(chain) for name, chain in self.chains.items()},
            "skipped": [p.to_json() for p in self.skipped],
            "problems": [p.to_json() for p in self.problems],
        }

    def write(self, folder: Path | str) -> list[Path]:
        """Write `race.json`, `scenario.json` (the capsule, so `twinflow states` can use the
        plant's own views) and `policies/<name>/moves.parquet` for each kept policy."""
        out = Path(folder)
        out.mkdir(parents=True, exist_ok=True)
        race_file = out / RACE_FILE
        race_file.write_text(
            json.dumps(self.to_json(), indent=1, allow_nan=False) + "\n", encoding="utf-8"
        )
        scenario_file = out / SCENARIO_FILE
        scenario_file.write_text(
            json.dumps(self.project.capsule.to_dict(), indent=1) + "\n", encoding="utf-8"
        )
        written = [race_file, scenario_file]
        for name, table in self.kept.items():
            recorder = StateRecorder(run_id=f"race:{name}")
            for episode, moves in table.episodes():
                for move in moves:
                    recorder.add(self.project.annotate(move), episode=episode, policy=name)
            target = out / POLICY_FOLDER / _safe(name)
            recorder.flush(target)
            written.append(target / "moves.parquet")
        return written


def chain_json(chain: MarkovChain) -> dict[str, Any]:
    """A chain for race.json: the map's states only (not every visited state), plus its
    Mermaid text."""
    data = chain.to_json()
    shown = set(chain.states)
    data["share"] = {s: v for s, v in data["share"].items() if s in shown}
    data["money"] = {s: v for s, v in data["money"].items() if s in shown}
    data["mermaid"] = chain.to_mermaid()
    return data


def _view_problem(project: Project, policy: Extension, exc: Exception) -> Problem:
    """A view file that breaks while the race counts its Markov map: a problem, never a
    crash."""
    return Problem(
        "view",
        "raised",
        f"The Markov map for {policy.name} could not be drawn: {type(exc).__name__}: {exc}.",
        "Run `twinflow extensions check` and fix the view file it names.",
        name=project.config.markov.view,
    )


def _dollar_band(row: PolicyResult) -> Band:
    return row.cumulative_dollars


def _early_leader(ranks: Mapping[str, Sequence[int]]) -> str | None:
    """The policy ranked first after day 1 (the first name on a tie)."""
    for name, by_day in ranks.items():
        if by_day and by_day[0] == 1:
            return name
    return None


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _missing(project: Project, name: str) -> Problem:
    broken = [
        p
        for p in project.extensions.problems
        if p.kind == "policy" and p.file is not None and p.file.stem == name
    ]
    if broken:
        return replace(broken[0], name=name)
    known = project.extensions.names("policy")
    return Problem(
        "policy",
        "unknown_name",
        f"There is no policy named {name!r}.",
        did_you_mean(name, known) or "Add a policy file to policies/.",
        name=name,
    )


def _slow(policy: Extension, limit: float, seconds: float) -> Problem:
    took = "too long" if math.isinf(seconds) else f"{seconds:.2f} s"
    return Problem(
        "policy",
        "slow",
        f"One decision took {took}; the race stops a policy after {limit:g} s.",
        "Make decide do less work each time, or raise experiment.race.decision_timeout_s "
        "in the capsule.",
        policy.file,
        policy.line,
        policy.name,
    )


@dataclass
class Run:
    """One policy under one observation: its result, and (first observation only) its
    compact moves and Markov counts."""

    result: PolicyResult
    table: MoveTable | None = None
    counter: ChainCounter | None = None
    view_problem: Problem | None = None


def _run_one(
    project: Project,
    policy: Extension,
    plan: EpisodePlan,
    settings: Mapping[str, Any],
    observation: str,
    race_settings: RaceSettings,
    *,
    keep: bool = False,
) -> Run | Problem:
    """Race one policy, summing each episode as it ends (COMP-157 streams: memory does not
    grow with episodes). With `keep`, the moves are kept as compact columns and counted
    into the policy's Markov map."""
    tally = _Tally(project, project.reward)
    table = MoveTable() if keep else None
    counter = project.markov_counter() if keep else None
    view_problem: Problem | None = None
    episodes = iter_episodes(
        project,
        policy,
        plan,
        settings=settings,
        observation=observation,
        timeout_s=race_settings.decision_timeout_s,
    )
    try:
        for episode, moves in enumerate(episodes):
            tally.add(moves)
            if table is not None:
                table.add(moves, episode)
            if counter is not None:
                try:
                    counter.add(moves)
                except Exception as exc:  # a view file is user code
                    view_problem, counter = _view_problem(project, policy, exc), None
    except ExtensionFailed as failed:
        return failed.problem
    except DecisionTimeout as late:
        return _slow(policy, race_settings.decision_timeout_s, late.seconds)
    result = _result(tally, policy, observation, settings, race_settings.interval)
    return Run(result, table, counter, view_problem)


def _ranked(rows: Sequence[PolicyResult], level: float) -> list[PolicyResult]:
    """Ranks by mean score, paired intervals against the best, and the D-152 flag."""
    order = sorted(rows, key=lambda r: -r.score.mean)
    if not order:
        return []
    best = order[0]
    out = []
    for rank, row in enumerate(order, start=1):
        vs = None
        if row is not best:
            diffs = np.subtract(row.episode_score, best.episode_score)
            vs = stat(list(diffs), level)
        below = [r for r in order[rank:] if r.score.mean < row.score.mean]
        richer = [r for r in below if r.dollars.mean > row.dollars.mean]
        warning = None
        if richer:
            top = max(richer, key=lambda r: r.dollars.mean)
            warning = (
                f"{row.policy} beats {top.policy} on the score but makes "
                f"${top.dollars.mean - row.dollars.mean:,.0f} less per run."
            )
        out.append(replace(row, rank=rank, vs_best=vs, dollar_warning=warning))
    return out


def _policies(
    project: Project, names: Sequence[str] | None
) -> tuple[list[Extension], list[Problem]]:
    """The policy extensions to race (make_next files only) and problems for names that
    cannot be raced."""
    if names is None:
        found = [p for p in project.extensions.of("policy") if p.decision == "make_next"]
        broken = [p for p in project.extensions.problems if p.kind == "policy"]
        return found, broken
    chosen, problems = [], []
    for name in names:
        if name not in project.extensions.names("policy"):
            problems.append(_missing(project, name))
            continue
        ext = project.extensions.get("policy", name)
        if ext.decision != "make_next":
            problems.append(
                Problem(
                    "policy",
                    "bad_decision",
                    f"{name} is a dispatch policy; the race runs make_next policies.",
                    "Race it in a discrete-event twin with `des_runtime`, or leave it out.",
                    ext.file,
                    name=name,
                )
            )
            continue
        chosen.append(ext)
    return chosen, problems


def tune(
    project: Project,
    policy: Extension,
    plan: EpisodePlan,
    observation: str,
    race_settings: RaceSettings,
) -> tuple[dict[str, Any] | None, list[Trial], Problem | None]:
    """Try every grid point of a policy's SETTINGS on the tuning plan (D-143).

    Returns the best settings (the first best on a tie; None when every point broke), the
    trials, and the first problem when every point broke. A policy without SETTINGS gets
    `{}` and no trials: it runs once, untuned."""
    if not policy.grid:
        return {}, [], None
    trials: list[Trial] = []
    best: tuple[float, dict[str, Any]] | None = None
    first_problem: Problem | None = None
    for point in grid_points(policy.grid):
        run = _run_one(project, policy, plan, point, observation, race_settings)
        if isinstance(run, Problem):
            trials.append(Trial(policy.name, MappingProxyType(point), None, None, run))
            first_problem = first_problem or run
            continue
        result = run.result
        trials.append(
            Trial(policy.name, MappingProxyType(point), result.score.mean, result.dollars.mean)
        )
        if best is None or result.score.mean > best[0]:
            best = (result.score.mean, point)
    if best is None:
        return None, trials, first_problem
    return best[1], trials, None


def race(
    project: Project,
    policies: Sequence[str] | None = None,
    *,
    settings: RaceSettings | None = None,
    observations: Sequence[str] | None = None,
    fixed: Mapping[str, Mapping[str, Any]] | None = None,
    progress: Callable[[str], None] | None = None,
) -> RaceResult:
    """Race `policies` (default: every make_next policy file of the project) on the same
    demand draws and rank them by the capsule's score (REQ-206).

    `settings` replaces `experiment.race`; `observations` replaces `experiment.observation`
    (each policy runs under each one, REQ-226); `fixed` gives a policy its settings without
    tuning. `progress` is called with a short line as each policy finishes.
    """
    chosen = settings or project.config.race
    masks = tuple(observations or project.config.observations)
    extensions, problems = _policies(project, policies)
    skipped = list(problems)
    plan = project.episodes(chosen.episodes, chosen.seed)
    tune_plan: EpisodePlan | None = None
    results: list[PolicyResult] = []
    trials: list[Trial] = []
    kept: dict[str, tuple[MoveTable, float]] = {}
    chains: dict[str, MarkovChain] = {}
    for policy in extensions:
        if fixed is not None and policy.name in fixed:
            policy_settings = dict(fixed[policy.name])
        else:
            if tune_plan is None and policy.grid:
                tune_plan = project.episodes(chosen.tune_episodes, chosen.tune_seed)
            best, tried, broke = tune(project, policy, tune_plan or plan, masks[0], chosen)
            trials += tried
            if best is None:
                skipped.append(broke or _missing(project, policy.name))
                continue
            policy_settings = best
        runs: list[Run] = []
        for mask in masks:
            run = _run_one(
                project, policy, plan, policy_settings, mask, chosen, keep=mask == masks[0]
            )
            if isinstance(run, Problem):
                skipped.append(run)
                break
            runs.append(run)
            if progress is not None:
                progress(f"{policy.name} ({mask}): {run.result.score.mean:,.2f}")
        else:
            first = runs[0]
            if first.view_problem is not None:
                skipped.append(first.view_problem)
            elif first.counter is not None:
                markov = project.config.markov
                chain = first.counter.chain(top_k=markov.top_k, min_prob=markov.min_prob)
                chains[policy.name] = chain
                leaks = tuple(money_leaks(chain, chosen.episodes))
                first.result = replace(first.result, leaks=leaks)
            results += [run.result for run in runs]
            if first.table is not None:
                _keep(kept, policy.name, first.table, first.result.score.mean, chosen.top_policies)
    ranked: list[PolicyResult] = []
    for mask in masks:
        ranked += _ranked([r for r in results if r.observation == mask], chosen.interval)
    return RaceResult(
        project=project,
        settings=chosen,
        score_name=project.config.score,
        observations=masks,
        results=tuple(ranked),
        skipped=tuple(skipped),
        problems=tuple(skipped),
        kept=MappingProxyType({name: table for name, (table, _) in kept.items()}),
        trials=tuple(trials),
        chains=MappingProxyType(
            {
                r.policy: chains[r.policy]
                for r in ranked[: chosen.top_policies]
                if r.observation == masks[0] and r.policy in chains
            }
        ),
    )


def _keep(
    kept: dict[str, tuple[MoveTable, float]],
    name: str,
    table: MoveTable,
    score: float,
    top: int,
) -> None:
    """Keep the compact moves of the best `top` policies so far."""
    kept[name] = (table, score)
    while len(kept) > top:
        worst = min(kept, key=lambda n: kept[n][1])
        del kept[worst]


__all__ = [
    "Band",
    "DecisionTimeout",
    "LEAK_PARTS",
    "Leak",
    "MoveTable",
    "PolicyResult",
    "RaceResult",
    "Stat",
    "Trial",
    "band",
    "chain_json",
    "grid_points",
    "iter_episodes",
    "money_leaks",
    "first_day_passed",
    "race",
    "rank_by_day",
    "run_policy",
    "stat",
    "tune",
]
