"""The analysis API behind the two sample notebooks (T-267, REQ-227).

Plain functions over a `Project` and a `RaceResult` that rebuild the RL repo's
`factory_mdp.analysis` and `charts` on twinflow: tables are polars data frames, figures are
plotly figures (D-140: no pandas, networkx or matplotlib). On the golden seeds every number
equals the RL repo's (`tests/extensions/race_golden.json`).

Notebook 1, long-term policy:

- `tuning_trials(result)`, `tuning_summary(result)`  every trial of every SETTINGS grid
- `cumulative_profit(result)`  mean running profit per day with a band of one spread
- `rank_by_day(result)`, `first_day_passed(result, leader, challenger)`
- `parts_per_run(result)`  dollars by cause per run
- `horizon_table(project, policies, horizons, ...)`  final profit for other plan lengths
- `markov_heatmap(chains)`, `markov_network(chains)`  figures of `project.markov(...)`
- `inventory_grid(project, moves)`  days of cover by product by day, and low/ok/high

Notebook 2, state information:

- `profit_by_view(result)`  profit of each policy under each observation mask
- `find_aliased_pair(project, episodes, policy, ...)`, `aliasing_table(project, pair)`
  two real states that look the same in a view but need different actions
- `next_state_entropy(project, episodes, ...)`  how much a view tells about tomorrow
- `action_values(project, state, policy, ...)`  money of each first action, then the rule

`run_episodes(project, policy, episodes, seed)` runs one policy on a seed's dice.
Only the CLI and in-process Python may use this module: it runs policy files (D-139).
"""

from __future__ import annotations

import dataclasses
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import plotly.graph_objects as go  # type: ignore[import-untyped]  # no stub; D-026 lib choice
import polars as pl
from plotly.subplots import make_subplots  # type: ignore[import-untyped]

from twinflow.engines.environment import grouped_labels
from twinflow.engines.step.engine import StepEngine
from twinflow.engines.step.model import StepModel
from twinflow.extensions.loader import ExtensionNotFound
from twinflow.extensions.project import Project
from twinflow.extensions.race import RaceResult, run_policy
from twinflow.extensions.race import first_day_passed as _first_day_passed
from twinflow.extensions.race import rank_by_day as _rank_by_day
from twinflow.extensions.view import ModelFacts
from twinflow.report import theme
from twinflow.state.groups import GroupedView, Level, StockGroups
from twinflow.state.markov import MarkovChain
from twinflow.state.records import LEDGER_PARTS, Move, Snapshot

Episodes = Sequence[Sequence[Move]]
Labeller = Callable[[Snapshot], str]

LOW_COVER_DAYS = 3.0  # the RL repo's low / medium / high bands, in days of cover
HIGH_COVER_DAYS = 10.0
COVER_CAP_DAYS = 30.0  # the inventory grid's colour scale stops here
BUCKETED = GroupedView(
    "bucketed",
    stock=StockGroups(
        by="days_of_cover",
        levels=(Level("low", LOW_COVER_DAYS), Level("medium", HIGH_COVER_DAYS), Level("high")),
    ),
    setup="thing",
)
"""Every product low / medium / high plus the product on the press (RL repo `bucketed`)."""


def _fmt(settings: Mapping[str, Any]) -> str:
    return ", ".join(
        f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in settings.items()
    )


# --- running ------------------------------------------------------------------------------------


def run_episodes(
    project: Project,
    policy: str,
    episodes: int,
    seed: int,
    *,
    settings: Mapping[str, Any] | None = None,
    observation: str | None = None,
) -> list[list[Move]]:
    """`episodes` runs of one policy on seed `seed`'s demand (the RL repo's `simulate`);
    one list of moves per run."""
    plan = project.episodes(episodes, seed)
    ext = project.extensions.get("policy", policy)
    return run_policy(project, ext, plan, settings=settings, observation=observation)


# --- tuning -------------------------------------------------------------------------------------


def tuning_trials(result: RaceResult) -> pl.DataFrame:
    """One row per tuning trial: policy, settings, mean score and dollars per run on the
    tuning seeds, whether it is the setting the race used, and the problem code when the
    trial broke."""
    used = {r.policy: dict(r.settings) for r in result.ranking()}
    return pl.DataFrame(
        {
            "policy": [t.policy for t in result.trials],
            "settings": [_fmt(t.settings) for t in result.trials],
            "score": [t.score for t in result.trials],
            "dollars": [t.dollars for t in result.trials],
            "best": [used.get(t.policy) == dict(t.settings) for t in result.trials],
            "broke": [None if t.problem is None else t.problem.code for t in result.trials],
        },
        schema={
            "policy": pl.String,
            "settings": pl.String,
            "score": pl.Float64,
            "dollars": pl.Float64,
            "best": pl.Boolean,
            "broke": pl.String,
        },
    )


def tuning_summary(result: RaceResult) -> pl.DataFrame:
    """Per tuned policy: how many settings were tried, the best one, and the best and
    worst mean score (the RL notebook's tuning report)."""
    rows: dict[str, list[Any]] = {}
    for trial in result.trials:
        rows.setdefault(trial.policy, []).append(trial)
    used = {r.policy: _fmt(r.settings) for r in result.ranking()}
    names = list(rows)
    scores = [[t.score for t in rows[n] if t.score is not None] for n in names]
    return pl.DataFrame(
        {
            "policy": names,
            "settings tried": [len(rows[n]) for n in names],
            "best setting": [used.get(n, "") for n in names],
            "best": [max(s) if s else None for s in scores],
            "worst": [min(s) if s else None for s in scores],
        },
        schema={
            "policy": pl.String,
            "settings tried": pl.Int64,
            "best setting": pl.String,
            "best": pl.Float64,
            "worst": pl.Float64,
        },
    )


# --- notebook 1: long-term thinking -------------------------------------------------------------


def cumulative_profit(
    result: RaceResult, measure: str = "dollars", observation: str | None = None
) -> pl.DataFrame:
    """Mean running total per day (day 1 = after the first day) and its band of one sample
    spread: columns day, policy, mean, std, low (mean - std), high (mean + std)."""
    frames = []
    for row in result.ranking(observation):
        band = row.cumulative_dollars if measure == "dollars" else row.cumulative_score
        mean, std = np.array(band.mean), np.array(band.std)
        frames.append(
            pl.DataFrame(
                {
                    "day": np.arange(1, len(mean) + 1),
                    "policy": [row.policy] * len(mean),
                    "mean": mean,
                    "std": std,
                    "low": mean - std,
                    "high": mean + std,
                }
            )
        )
    if not frames:
        return pl.DataFrame(
            schema={
                "day": pl.Int64,
                "policy": pl.String,
                "mean": pl.Float64,
                "std": pl.Float64,
                "low": pl.Float64,
                "high": pl.Float64,
            }
        )
    return pl.concat(frames)


def rank_by_day(
    result: RaceResult, measure: str = "dollars", observation: str | None = None
) -> pl.DataFrame:
    """Rank 1 = most so far, one column per policy (ranking order), one row per day."""
    ranks = _rank_by_day(result.cumulative(measure, observation))
    days = len(next(iter(ranks.values()), []))
    return pl.DataFrame({"day": list(range(1, days + 1)), **ranks})


def first_day_passed(
    result: RaceResult, leader: str, challenger: str, measure: str = "dollars"
) -> int | None:
    """The first day (from 1) the challenger's mean running total passes the leader's."""
    return _first_day_passed(result.cumulative(measure), leader, challenger)


def parts_per_run(result: RaceResult, observation: str | None = None) -> pl.DataFrame:
    """Dollars per run by cause (revenue, production, setup, holding, lost sales, late)
    and the total, one row per policy in ranking order."""
    ranked = result.ranking(observation)
    data: dict[str, list[Any]] = {"policy": [r.policy for r in ranked]}
    for part in LEDGER_PARTS:
        data[part] = [r.parts[part] for r in ranked]
    data["total"] = [r.dollars.mean for r in ranked]
    return pl.DataFrame(data)


def with_horizon(project: Project, horizon: int) -> Project:
    """The same project with another plan length (make-to-stock only)."""
    if project.model is None:
        raise ValueError("a custom engine sets its own horizon; change it in the capsule")
    model: StepModel = dataclasses.replace(project.model, horizon=horizon)
    return dataclasses.replace(project, model=model, facts=ModelFacts.from_step_model(model))


def horizon_table(
    project: Project,
    policies: Sequence[str],
    horizons: Sequence[int],
    *,
    episodes: int,
    seed: int,
    settings: Mapping[str, Mapping[str, Any]] | None = None,
    observation: str | None = None,
) -> pl.DataFrame:
    """Mean final profit per run of each policy at each plan length (RL repo
    `horizon_table`): columns policy, "<h> days" per horizon."""
    table: dict[str, list[Any]] = {"policy": list(policies)}
    for horizon in horizons:
        shorter = with_horizon(project, horizon)
        column = []
        for name in policies:
            runs = run_episodes(
                shorter,
                name,
                episodes,
                seed,
                settings=(settings or {}).get(name),
                observation=observation,
            )
            column.append(float(np.mean([sum(m.ledger.total for m in run) for run in runs])))
        table[f"{horizon} days"] = column
    return pl.DataFrame(table)


# --- views --------------------------------------------------------------------------------------


def analysis_view(project: Project, view: str | GroupedView | Labeller) -> Labeller:
    """A view as a snapshot-to-label function: a capsule or file view by name, the
    built-in `bucketed` view, a `GroupedView` or a function."""
    chosen: Any = view
    if isinstance(view, str):
        if view in project.view_names:
            chosen = project.view(view)
        elif view == BUCKETED.name:
            chosen = BUCKETED
        else:
            raise ExtensionNotFound("view", view, (*project.view_names, BUCKETED.name))
    if isinstance(chosen, GroupedView):
        grouped, rates, families = chosen, project.facts.demand_rate, project.facts.family

        def label(snapshot: Snapshot) -> str:
            return grouped.label(snapshot, rates, families)

        return label
    labeller: Labeller = chosen
    return labeller


def _view_object(project: Project, view: str | GroupedView | Labeller) -> Any:
    if isinstance(view, str):
        if view in project.view_names:
            return project.view(view)
        return BUCKETED if view == BUCKETED.name else None
    return view


def _label_order(project: Project, view: str | GroupedView | Labeller) -> dict[str, int]:
    """Each label's place in the view's state numbering (the RL repo's order)."""
    grouped = _view_object(project, view)
    if not isinstance(grouped, GroupedView) or grouped.exact:
        return {}
    labels = grouped_labels(grouped, project.facts.products, project.facts.family)
    return {label: i for i, label in enumerate(labels)}


def profit_by_view(result: RaceResult, *, loss: bool = False) -> pl.DataFrame:
    """Mean dollars per run, one row per observation, one column per policy (RL notebook 2).
    With `loss`, each value is the drop from the first observation instead."""
    table = result.profit_by_view()
    masks = list(result.observations)
    data: dict[str, list[Any]] = {"observation": masks}
    for policy, row in table.items():
        values = [row.get(m) for m in masks]
        if loss:
            first = values[0]
            values = [None if v is None or first is None else first - v for v in values]
        data[policy] = values
    return pl.DataFrame(data)


# --- notebook 2: aliasing -----------------------------------------------------------------------


def _engine_with(project: Project, rows: npt.NDArray[np.int64], day: int) -> StepEngine:
    """A step engine whose episode i has demand `rows[i]` from `day` on."""
    model = project.model
    if model is None:
        raise ValueError("this analysis needs a make-to-stock model")
    table = np.zeros((rows.shape[0], model.horizon, len(model.products)), dtype=np.int64)
    span = min(rows.shape[1], model.horizon - day)
    table[:, day : day + span] = rows[:, :span]
    return StepEngine(model, table)


def action_values(
    project: Project,
    state: Snapshot,
    policy: str,
    *,
    settings: Mapping[str, Any] | None = None,
    lookahead: int = 20,
    draws: int = 40,
    seed: int = 0,
) -> dict[str, float]:
    """Mean dollars over the next `lookahead` days for each first action, then following
    `policy`, on the same dice for every action (RL repo `action_values`). The run stops at
    the end of the plan."""
    model = project.model
    if model is None:
        raise ValueError("action values need a make-to-stock model")
    rng = np.random.default_rng(seed)
    demand = rng.poisson(model.demand_rates, size=(draws, lookahead, len(model.products)))
    engine = _engine_with(project, demand.astype(np.int64), state.period)
    decide = project.decider(project.extensions.get("policy", policy), engine, settings=settings)
    days = min(lookahead, model.horizon - state.period)
    values: dict[str, float] = {}
    for action in engine.actions(state):
        totals = []
        for draw in range(draws):
            now = engine.start(draw, from_state=state)
            move = engine.step(action)
            total, now = move.ledger.total, move.after
            for _ in range(1, days):
                move = engine.step(decide(now))
                total, now = total + move.ledger.total, move.after
            totals.append(total)
        values[action] = float(np.mean(totals))
    return values


@dataclass(frozen=True)
class AliasedPair:
    """Two real states with the same label whose best first actions differ."""

    bucket: str
    states: tuple[Snapshot, Snapshot]
    values: tuple[dict[str, float], dict[str, float]]
    best: tuple[str, str]
    regret: float  # dollars lost (both states) by using the other state's best action
    lookahead: int


def _key(snapshot: Snapshot) -> tuple[Any, ...]:
    return (tuple(snapshot.stock.items()), tuple(snapshot.setup.items()))


def _sample(count: int, size: int, seed: int) -> list[int]:
    """pandas `sample(size, random_state=seed)` positions: a legacy RandomState
    permutation, first `size` (so the RL repo's picks are the same)."""
    picks = np.random.RandomState(seed).permutation(count)[: min(size, count)]  # noqa: NPY002
    return [int(i) for i in picks]


def _better(
    best: AliasedPair | None,
    bucket: str,
    states: Sequence[Snapshot],
    values: Sequence[dict[str, float]],
    lookahead: int,
) -> AliasedPair | None:
    for i in range(len(states)):
        for j in range(i + 1, len(states)):
            a = max(values[i], key=lambda k: values[i][k])
            b = max(values[j], key=lambda k: values[j][k])
            if a == b:
                continue
            regret = (values[i][a] - values[i][b]) + (values[j][b] - values[j][a])
            if best is None or regret > best.regret:
                best = AliasedPair(
                    bucket, (states[i], states[j]), (values[i], values[j]), (a, b), regret,
                    lookahead,
                )  # fmt: skip
    return best


def find_aliased_pair(
    project: Project,
    episodes: Episodes,
    policy: str,
    *,
    settings: Mapping[str, Any] | None = None,
    view: str | GroupedView | Labeller = "bucketed",
    buckets: int = 8,
    per_bucket: int = 10,
    lookahead: int = 20,
    draws: int = 40,
    seed: int = 0,
    days: tuple[int, int] = (5, 40),
) -> AliasedPair | None:
    """Two full states in the same label whose best actions differ, picking the pair where
    using the other state's best action costs the most (RL repo `find_aliased_pair`).

    Looks at days `days` (inclusive) of `episodes`, the `buckets` most common labels, and
    up to `per_bucket` different states in each; values come from `action_values`.
    """
    label = analysis_view(project, view)
    rows = [m.before for run in episodes for m in run if days[0] <= m.index <= days[1]]
    labels = [label(s) for s in rows]
    counts = Counter(labels)
    order = sorted(counts, key=lambda s: -counts[s])  # ties keep first-seen order
    rng = np.random.default_rng(seed)
    best: AliasedPair | None = None
    for bucket in order[:buckets]:
        seen: set[tuple[Any, ...]] = set()
        group = []
        for state, name in zip(rows, labels, strict=True):
            if name == bucket and _key(state) not in seen:
                seen.add(_key(state))
                group.append(state)
        picks = _sample(len(group), per_bucket, int(rng.integers(1 << 30)))
        states = [group[i] for i in picks]
        values = [
            action_values(
                project, s, policy, settings=settings, lookahead=lookahead, draws=draws, seed=seed
            )
            for s in states
        ]
        best = _better(best, bucket, states, values, lookahead)
    return best


def one_day_reward(project: Project, state: Snapshot, action: str) -> float:
    """Dollars of one action on a day when demand equals its (rounded) average."""
    model = project.model
    if model is None:
        raise ValueError("this analysis needs a make-to-stock model")
    average = np.array([[[round(r) for r in model.demand_rates]]], dtype=np.int64)
    engine = _engine_with(project, average, state.period)
    engine.start(0, from_state=state)
    return engine.step(action).ledger.total


def aliasing_table(project: Project, pair: AliasedPair) -> pl.DataFrame:
    """The two states of an aliased pair side by side (RL repo `aliasing_table`)."""
    columns: dict[str, list[str]] = {
        "": [
            "day",
            "stock (units, in product order)",
            "press is on",
            "same view (label)",
            "best action",
            "1-day dollars (best)",
            f"{pair.lookahead}-day dollars (best)",
            "other state's best action",
            "1-day dollars (other)",
            f"{pair.lookahead}-day dollars (other)",
        ]
    }
    for k, name in enumerate("AB"):
        state, values, mine = pair.states[k], pair.values[k], pair.best[k]
        other = pair.best[1 - k]
        setup = next(iter(state.setup.values()), None)
        columns[name] = [
            str(state.period),
            " ".join(f"{state.stock[p]:g}" for p in project.facts.products),
            str(setup),
            pair.bucket,
            mine,
            f"{one_day_reward(project, state, mine):,.2f}",
            f"{values[mine]:,.2f}",
            other,
            f"{one_day_reward(project, state, other):,.2f}",
            f"{values[other]:,.2f}",
        ]
    return pl.DataFrame(columns)


# --- notebook 2: how much a view tells about tomorrow -------------------------------------------


def entropy_bits(labels: Sequence[str]) -> float:
    """Spread of a list of labels in bits (0 = always the same)."""
    counts = np.array(list(Counter(labels).values()), dtype=float)
    shares = counts / counts.sum()
    return float(-(shares * np.log2(shares)).sum())


def _next_spread(
    project: Project,
    state: Snapshot,
    action: str,
    label: Labeller,
    draws: int,
    rng: np.random.Generator,
) -> float:
    model = project.model
    if model is None:
        raise ValueError("this analysis needs a make-to-stock model")
    rows = np.array([[rng.poisson(model.demand_rates)] for _ in range(draws)], dtype=np.int64)
    engine = _engine_with(project, rows, state.period)
    nexts = []
    for draw in range(draws):
        engine.start(draw, from_state=state)
        nexts.append(label(engine.step(action).after))
    return entropy_bits(nexts)


def next_state_entropy(
    project: Project,
    episodes: Episodes,
    *,
    view: str | GroupedView | Labeller = "family",
    min_rows: int = 100,
    max_groups: int = 25,
    states_per_group: int = 15,
    draws: int = 150,
    seed: int = 0,
) -> pl.DataFrame:
    """For common (label, action) pairs, the spread of tomorrow's label (in bits) when you
    see only today's label ("grouped view") and when you see the full state ("full view"),
    the RL repo's `entropy_comparison`. More bits means the state says less about tomorrow.

    Pairs with fewer than `min_rows` days are left out; the full-view spread is the mean
    over up to `states_per_group` sampled states, each stepped `draws` times. The result is
    sorted by days, most first, and cut to `max_groups` rows."""
    label = analysis_view(project, view)
    moves = [m for run in episodes for m in run]
    now = [label(m.before) for m in moves]
    nxt = [label(m.after) for m in moves]
    groups: dict[tuple[str, str], list[int]] = {}
    for i, move in enumerate(moves):
        groups.setdefault((now[i], str(move.action)), []).append(i)
    order = _label_order(project, view)
    actions = {a: i for i, a in enumerate(project.actions)}

    def place(key: tuple[str, str]) -> tuple[int, str, int, str]:
        return (order.get(key[0], len(order)), key[0], actions.get(key[1], len(actions)), key[1])

    rng = np.random.default_rng(seed)
    rows = []
    for key in sorted(groups, key=place):
        members = groups[key]
        if len(members) < min_rows:
            continue
        picks = _sample(len(members), states_per_group, seed)
        full = float(
            np.mean(
                [
                    _next_spread(project, moves[members[j]].before, key[1], label, draws, rng)
                    for j in picks
                ]
            )
        )
        rows.append((key[0], key[1], len(members), entropy_bits([nxt[i] for i in members]), full))
    rows.sort(key=lambda r: -r[2])  # stable: equal days keep the state order
    rows = rows[:max_groups]
    return pl.DataFrame(
        {
            "label": [r[0] for r in rows],
            "action": [r[1] for r in rows],
            "days": [r[2] for r in rows],
            "grouped view": [r[3] for r in rows],
            "full view": [r[4] for r in rows],
        },
        schema={
            "label": pl.String,
            "action": pl.String,
            "days": pl.Int64,
            "grouped view": pl.Float64,
            "full view": pl.Float64,
        },
    )


# --- figures ------------------------------------------------------------------------------------


def markov_heatmap(chains: Mapping[str, MarkovChain]) -> go.Figure:
    """One heatmap per chain: the chance of each next state (column) from each state (row),
    over the chain's top states (RL repo `plot_markov_heatmaps`)."""
    names = list(chains)
    fig = make_subplots(rows=1, cols=max(1, len(names)), horizontal_spacing=0.25)
    for k, name in enumerate(names, start=1):
        chain = chains[name]
        labels = list(chain.states)
        fig.add_trace(
            go.Heatmap(
                z=chain.matrix,
                x=labels,
                y=labels,
                zmin=0,
                zmax=1,
                colorscale=theme.sequential_scale(),
                colorbar={"title": "chance"},
                showscale=k == 1,
                name=name,
            ),
            row=1,
            col=k,
        )
        fig.update_xaxes(
            title_text=f"{name}: top {len(labels)} states, {chain.coverage:.0%} of days",
            row=1,
            col=k,
        )
        fig.update_yaxes(autorange="reversed", row=1, col=k)
    fig.update_layout(height=650, width=700 * max(1, len(names)), template=theme.plotly_template())
    return fig


def _circle(count: int) -> tuple[list[float], list[float]]:
    angles = [math.pi / 2 - 2 * math.pi * k / max(1, count) for k in range(count)]
    return [math.cos(a) for a in angles], [math.sin(a) for a in angles]


def markov_network(chains: Mapping[str, MarkovChain]) -> go.Figure:
    """One circle of states per chain: dot size = share of days, arrow = a likely move
    (its chance written on it), states that stay put get their chance above the dot (RL
    repo `plot_markov_networks`, drawn without networkx, D-140)."""
    names = list(chains)
    fig = make_subplots(rows=1, cols=max(1, len(names)))
    for k, name in enumerate(names, start=1):
        chain = chains[name]
        xs, ys = _circle(len(chain.states))
        where = {s: (xs[i], ys[i]) for i, s in enumerate(chain.states)}
        sizes = [14 + 70 * math.sqrt(chain.share[s]) for s in chain.states]
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers+text",
                text=list(chain.states),
                textposition="bottom center",
                textfont={"size": 9},
                marker={"size": sizes, "color": theme.palette().pick, "opacity": 0.8},
                hovertext=[f"{s}: {chain.share[s]:.1%} of days" for s in chain.states],
                hoverinfo="text",
                name=name,
                showlegend=False,
            ),
            row=1,
            col=k,
        )
        axis = "" if k == 1 else str(k)
        for edge in chain.edges:
            x0, y0 = where[edge.source]
            x1, y1 = where[edge.target]
            loop = edge.source == edge.target
            fig.add_annotation(
                x=x1,
                y=y1 + (0.09 if loop else 0.0),
                ax=x0,
                ay=y0,
                xref=f"x{axis}",
                yref=f"y{axis}",
                axref=f"x{axis}",
                ayref=f"y{axis}",
                text=f"{edge.prob:.0%}" if loop else "",
                showarrow=not loop,
                arrowhead=2,
                arrowwidth=1 + 4 * edge.prob,
                arrowcolor=theme.faded(theme.palette().today, 0.7),
                standoff=8,
                font={"size": 9},
            )
        fig.update_xaxes(visible=False, range=[-1.4, 1.4], row=1, col=k)
        fig.update_yaxes(visible=False, range=[-1.4, 1.4], row=1, col=k)
    fig.update_layout(height=650, width=700 * max(1, len(names)), template=theme.plotly_template())
    return fig


def cover_levels(cover: npt.NDArray[np.float64]) -> npt.NDArray[np.int64]:
    """0 = low (< 3 days of cover), 1 = medium (< 10), 2 = high (RL repo bands)."""
    return ((cover >= LOW_COVER_DAYS).astype(np.int64) + (cover >= HIGH_COVER_DAYS)).astype(
        np.int64
    )


def inventory_grid(project: Project, moves: Sequence[Move]) -> go.Figure:
    """One run, two heatmaps: days of cover by product by day (capped at 30), and the same
    as low / medium / high; dashed lines mark the days the press changed product (RL repo
    `plot_inventory_grid`). `moves` is one run, in day order."""
    products = list(project.facts.products)
    rates = np.array([project.facts.demand_rate[p] for p in products], dtype=float)
    stock = np.array([[m.before.stock[p] for m in moves] for p in products], dtype=float)
    cover = stock / rates[:, None]
    days = [m.index for m in moves]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08)
    fig.add_trace(
        go.Heatmap(
            z=np.minimum(cover, COVER_CAP_DAYS),
            x=days,
            y=products,
            colorscale=theme.sequential_scale(),
            colorbar={"title": "days of cover", "y": 0.78, "len": 0.45},
            name="days of cover",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Heatmap(
            z=cover_levels(cover),
            x=days,
            y=products,
            zmin=0,
            zmax=2,
            colorscale=theme.level_scale(),
            colorbar={
                "tickvals": [0, 1, 2],
                "ticktext": ["low", "medium", "high"],
                "y": 0.22,
                "len": 0.45,
            },
            name="level",
        ),
        row=2,
        col=1,
    )
    setups = [tuple(m.before.setup.values()) for m in moves]
    for i in range(1, len(moves)):
        if setups[i] != setups[i - 1]:
            for axis in ("", "2"):
                fig.add_shape(
                    type="line",
                    x0=days[i] - 0.5,
                    x1=days[i] - 0.5,
                    y0=0,
                    y1=1,
                    xref=f"x{axis}",
                    yref=f"y{axis} domain",
                    line={"color": theme.palette().page, "width": 1, "dash": "dash"},
                )
    fig.update_xaxes(title_text="Day (dashed lines: the press changed product)", row=2, col=1)
    fig.update_layout(height=700, template=theme.plotly_template())
    return fig


__all__ = [
    "BUCKETED",
    "AliasedPair",
    "action_values",
    "aliasing_table",
    "analysis_view",
    "cover_levels",
    "cumulative_profit",
    "entropy_bits",
    "find_aliased_pair",
    "first_day_passed",
    "horizon_table",
    "inventory_grid",
    "markov_heatmap",
    "markov_network",
    "next_state_entropy",
    "one_day_reward",
    "parts_per_run",
    "profit_by_view",
    "rank_by_day",
    "run_episodes",
    "tuning_summary",
    "tuning_trials",
    "with_horizon",
]
