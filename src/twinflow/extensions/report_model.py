"""T-269: the decision data behind the race report (REQ-228, REQ-229).

`race_view_model(result, project)` turns a `RaceResult` into one JSON-ready dict that the
report page reads. It leads with the decision: which policy to run, how much more money it
makes than the baseline ("what we do today", `experiment.race.baseline`) per run and per
year, how sure we are in plain words, and what changes on the floor. The stats behind it
(cost parts, daily profit bands, an example run, Markov maps with money, leaks, tuning and
warnings) come after.

Every number is rounded for people: dollars to whole dollars, units to one decimal, shares
to four decimals. Words are plain (sixth-grade reading level) with no em dashes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from twinflow.extensions.project import Project
from twinflow.extensions.race import PolicyResult, RaceResult, Stat, stat
from twinflow.state.markov import MarkovChain
from twinflow.state.records import LEDGER_PARTS, Move

DAYS_PER_YEAR = 365
LOW_STOCK_DAYS = 3  # the `family` view's low band: under 3 days of cover
FALLBACK_BASELINE = "lowest_cover"
WHAT_IF_PARTS = ("setup", "holding", "lost_sales")
KEYS = (
    "about",
    "recommendation",
    "baseline",
    "delta",
    "confidence",
    "floor",
    "drivers",
    "parts",
    "policies",
    "products",
    "families",
    "example_runs",
    "markov",
    "leaks",
    "tuning",
    "views",
    "what_if",
    "warnings",
)
PART_WORDS = {
    "revenue": "sales",
    "production": "making product",
    "setup": "changeovers",
    "holding": "keeping stock",
    "lost_sales": "lost sales",
    "late": "late orders",
}
NAMES = {"s_big_s": "(s, S) reorder"}


# --- rounding ----------------------------------------------------------------------------------


def _dollars(value: float | None) -> int | None:
    return None if value is None else int(round(value))


def _units(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def _share(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(round(value)):,}"


def _stat(item: Stat | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "mean": _dollars(item.mean),
        "low": _dollars(item.low),
        "high": _dollars(item.high),
        "std": _dollars(item.std),
        "n": item.n,
    }


def _settings(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in values.items()}


def label(name: str) -> str:
    """A policy name for people: `family_campaign` is `Family campaign`."""
    return NAMES.get(name, name.replace("_", " ").capitalize())


# --- the decision ------------------------------------------------------------------------------


def _horizon(project: Project, ranked: Sequence[PolicyResult]) -> int:
    if project.facts.horizon:
        return int(project.facts.horizon)
    return int(ranked[0].daily_dollars.shape[1]) if ranked else 1


def _pick_baseline(
    project: Project, ranked: Sequence[PolicyResult]
) -> tuple[PolicyResult | None, str, list[dict[str, str]]]:
    """The baseline row, where it came from, and a warning when it was not raced."""
    names = [r.policy for r in ranked]
    wanted = project.baseline
    if wanted in names:
        return ranked[names.index(wanted)], project.baseline_source, []
    if not ranked:
        return None, project.baseline_source, []
    stand_in = FALLBACK_BASELINE if FALLBACK_BASELINE in names else names[-1]
    warning = {
        "kind": "baseline",
        "text": f"The baseline {wanted} was not in this race, so {stand_in} stands in for it.",
        "fix": f"Race {wanted} too, or set experiment.race.baseline to a policy in the race.",
    }
    return ranked[names.index(stand_in)], "stand-in", [warning]


def _paired(best: PolicyResult, base: PolicyResult, level: float) -> tuple[Stat | None, int, int]:
    diffs = np.subtract(best.episode_dollars, base.episode_dollars)
    runs = len(diffs)
    paired = stat(list(diffs), level) if runs else None
    return paired, int((diffs > 0).sum()), runs


def _confidence(best: PolicyResult, base: PolicyResult, level: float, same: bool) -> dict[str, Any]:
    pct = round(100 * level)
    paired, wins, runs = _paired(best, base, level)
    out: dict[str, Any] = {
        "interval": level,
        "mean": None if paired is None else _dollars(paired.mean),
        "low": None if paired is None else _dollars(paired.low),
        "high": None if paired is None else _dollars(paired.high),
        "wins": wins,
        "runs": runs,
    }
    if same:
        return {
            **out,
            "level": "same_policy",
            "words": "This is what you run today",
            "detail": "No other policy in the race made more money.",
        }
    if paired is None or paired.low is None or paired.high is None:
        return {
            **out,
            "level": "unknown",
            "words": "Not sure yet",
            "detail": "There was only one run, so we cannot say how sure we are. Race more runs.",
        }
    low, high = _money(paired.low), _money(paired.high)
    ahead = f"It came out ahead in {wins} of {runs} runs."
    if paired.low > 0:
        level_name, words = "very_likely_better", "Very likely better"
        detail = f"We are {pct}% sure it earns between {low} and {high} more per run on average."
    elif paired.high < 0:
        level_name, words = "very_likely_worse", "Very likely worse in dollars"
        detail = (
            f"We are {pct}% sure it earns between {_money(-paired.high)} and "
            f"{_money(-paired.low)} less per run. It ranks first on the score, not on dollars."
        )
    elif paired.mean >= 0:
        level_name, words = "maybe_better", "Maybe better, not sure yet"
        detail = (
            f"The gain per run could be anywhere from {low} to {high}. Race more runs to be sure."
        )
    else:
        level_name, words = "maybe_worse", "Maybe worse, not sure yet"
        detail = (
            f"The change per run could be anywhere from {low} to {high}. Race more runs to be sure."
        )
    return {**out, "level": level_name, "words": words, "detail": f"{detail} {ahead}"}


FLOOR = (
    # key, label, unit, lower is better, plural noun for the words
    ("changeovers", "Changeovers per run", "count", True, "changeovers"),
    ("cross_family", "Changeovers to another family per run", "count", True,
     "changeovers to another family"),
    ("fill_rate", "Orders filled", "share", False, "orders filled"),
    ("avg_stock", "Average stock on hand", "units", True, "units in stock"),
    ("lost_units", "Sales lost per run", "units", True, "units of lost sales"),
)  # fmt: skip


def _floor_words(change: float, unit: str, noun: str, same: bool) -> str:
    if same:
        return "No change"
    if unit == "share":
        points = 100 * change
        if abs(points) < 0.05:
            return f"About the same {noun}"
        return f"{abs(points):.1f} points {'more' if points > 0 else 'fewer'} {noun}"
    if abs(change) < 0.05:
        return f"About the same {noun}"
    amount = f"{abs(change):,.0f}" if abs(change) >= 10 else f"{abs(change):,.1f}"
    return f"{amount} {'more' if change > 0 else 'fewer'} {noun}"


def _floor(best: PolicyResult, base: PolicyResult, same: bool) -> list[dict[str, Any]]:
    rows = []
    for key, name, unit, lower_better, noun in FLOOR:
        mine, theirs = getattr(best, key), getattr(base, key)
        if mine is None or theirs is None:
            continue
        change = float(mine) - float(theirs)
        rounder = _share if unit == "share" else _units
        better = None if abs(change) < 1e-9 else (change < 0) == lower_better
        rows.append(
            {
                "key": key,
                "label": name,
                "unit": unit,
                "recommended": rounder(mine),
                "baseline": rounder(theirs),
                "change": rounder(change),
                "better": better,
                "words": _floor_words(change, unit, noun, same),
            }
        )
    return rows


def _drivers(best: PolicyResult, base: PolicyResult) -> list[dict[str, Any]]:
    """How each cost part moves profit, recommendation against baseline (gain first)."""
    rows: list[dict[str, Any]] = []
    for part in LEDGER_PARTS:
        mine, theirs = best.parts.get(part, 0.0), base.parts.get(part, 0.0)
        gain = mine - theirs if part == "revenue" else theirs - mine
        if part == "revenue":
            words = f"it sells {_money(abs(gain))} {'more' if gain >= 0 else 'less'} per run"
        else:
            more_less = "less" if gain >= 0 else "more"
            words = f"it spends {_money(abs(gain))} {more_less} per run on {PART_WORDS[part]}"
        rows.append({"part": part, "gain": _dollars(gain), "words": words})
    rows.sort(key=lambda r: -int(r["gain"]))
    return rows


def _why(drivers: Sequence[Mapping[str, Any]], best: PolicyResult, base: PolicyResult) -> str:
    if not drivers or drivers[0]["gain"] <= 0:
        return "It makes about the same money as what you run today."
    top = drivers[0]
    words = f"Main reason: {top['words']}"
    if top["part"] == "setup":
        fewer = base.cross_family - best.cross_family
        if fewer >= 0.5:
            words += f", with {fewer:,.0f} fewer changeovers to another family"
    return words + "."


def _sentence(best: PolicyResult, base: PolicyResult, per_run: float, per_year: float) -> str:
    if best.policy == base.policy:
        return f"Keep running {best.policy}. No other policy in the race made more money."
    more = "more" if per_run >= 0 else "less"
    return (
        f"Run {best.policy} instead of {base.policy}. It makes {_money(abs(per_run))} {more} "
        f"per run, about {_money(abs(per_year))} {more} a year."
    )


# --- the stats one click deeper ----------------------------------------------------------------


def _policy_row(
    row: PolicyResult, project: Project, base: PolicyResult | None, level: float
) -> dict[str, Any]:
    band = row.cumulative_dollars
    mean = np.asarray(band.mean)
    std = np.asarray(band.std)
    vs_base = None
    if base is not None and row.policy != base.policy:
        vs_base = _stat(_paired(row, base, level)[0])
    try:
        about = project.extensions.get("policy", row.policy).summary
    except KeyError:
        about = ""
    return {
        "policy": row.policy,
        "label": label(row.policy),
        "about": about,
        "rank": row.rank,
        "source": row.source,
        "settings": _settings(row.settings),
        "is_baseline": base is not None and row.policy == base.policy,
        "is_recommended": row.rank == 1,
        "score": {"mean": round(row.score.mean, 2), "low": row.score.low, "high": row.score.high},
        "dollars": _stat(row.dollars),
        "vs_best": _stat(row.vs_best),
        "vs_baseline": vs_base,
        "parts": {k: _dollars(v) for k, v in row.parts.items()},
        "fill_rate": _share(row.fill_rate),
        "changeovers": _units(row.changeovers),
        "cross_family": _units(row.cross_family),
        "idle_share": _share(row.idle_share),
        "avg_stock": _units(row.avg_stock),
        "lost_units": _units(row.lost_units),
        "dollar_warning": row.dollar_warning,
        "profit_by_day": {
            "mean": [_dollars(x) for x in mean],
            "low": [_dollars(x) for x in mean - std],
            "high": [_dollars(x) for x in mean + std],
        },
    }


def _products(project: Project) -> list[dict[str, Any]]:
    facts = project.facts
    return [
        {
            "id": p,
            "family": facts.family.get(p),
            "abc": facts.abc.get(p),
            "demand_rate": _units(facts.demand_rate.get(p)),
            "max_storage": facts.max_storage.get(p),
            "low_stock": _units(LOW_STOCK_DAYS * facts.demand_rate.get(p, 0.0)),
        }
        for p in facts.products
    ]


def _families(project: Project) -> list[str]:
    seen: dict[str, None] = {}
    for product in project.facts.products:
        family = project.facts.family.get(product)
        if family is not None:
            seen.setdefault(family, None)
    return list(seen)


def _setup(move: Move) -> str | None:
    lines = sorted(move.after.setup)
    if len(lines) == 1:
        return move.after.setup[lines[0]]
    values = [move.after.setup[line] or "idle" for line in lines]
    return ",".join(values) if values else None


def _switches(move: Move, family: Mapping[str, str]) -> tuple[bool, bool]:
    changed = cross = False
    for line, before in move.before.setup.items():
        after = move.after.setup.get(line)
        if after != before and after is not None:
            changed = True
            cross = (
                cross or before is None or family.get(before, before) != family.get(after, after)
            )
    return changed, cross


def _event_sum(move: Move, prefix: str) -> float:
    return sum(v for k, v in move.events.items() if k.startswith(prefix))


def _example_run(project: Project, episode: int, moves: Sequence[Move]) -> dict[str, Any]:
    """One race run, day by day: what the press ran, stock per product, money."""
    family = project.facts.family
    days, total = [], 0.0
    for move in moves:
        changed, cross = _switches(move, family)
        total += move.ledger.total
        days.append(
            {
                "day": move.index + 1,
                "action": move.action,
                "setup": _setup(move),
                "family": family.get(_setup(move) or ""),
                "changeover": changed,
                "cross_family": cross,
                "stock": {p: int(round(v)) for p, v in move.after.stock.items()},
                "demand": _units(_event_sum(move, "demand.")),
                "sold": _units(_event_sum(move, "sold.")),
                "lost": _units(_event_sum(move, "lost.")),
                "profit": _dollars(move.ledger.total),
                "cumulative": _dollars(total),
            }
        )
    start = {p: int(round(v)) for p, v in moves[0].before.stock.items()} if moves else {}
    return {"episode": episode, "start_stock": start, "days": days}


def _example_runs(project: Project, result: RaceResult) -> dict[str, Any]:
    runs = {}
    for name, table in result.kept.items():
        for episode, moves in table.episodes():
            runs[name] = _example_run(project, episode, moves)
            break
    return runs


def _label_parts(text: str) -> dict[str, Any] | None:
    """`grey:ok black:high | natural` as levels and setup, when a label has that shape."""
    stock, _, setup = text.partition(" | ")
    pairs = [p.split(":", 1) for p in stock.split()]
    if not pairs or any(len(p) != 2 for p in pairs):
        return None
    return {"levels": {k: v for k, v in pairs}, "setup": setup or None}


def _graph(chain: MarkovChain, episodes: int, leak_states: set[str]) -> dict[str, Any]:
    ids = {state: f"s{i}" for i, state in enumerate(chain.states)}
    sticky = set(chain.sticky)
    nodes = []
    for i, state in enumerate(chain.states):
        money = chain.money.get(state)
        per_day = money.total if money is not None else 0.0
        visits = money.visits if money is not None else 0
        parts = {p: _dollars(getattr(money, p)) for p in LEDGER_PARTS} if money else {}
        leak = (
            visits * sum(getattr(money, p) for p in ("setup", "holding", "lost_sales", "late"))
            if money is not None
            else 0.0
        )
        nodes.append(
            {
                "id": ids[state],
                "label": state,
                "parts_of_label": _label_parts(state),
                "share": _share(chain.share[state]),
                "visits": visits,
                "sticky": state in sticky,
                "stay": _share(float(chain.matrix[i, i])),
                "per_day": _dollars(per_day),
                "per_run": _dollars(visits * per_day / max(1, episodes)),
                "leak_per_run": _dollars(leak / max(1, episodes)),
                "money_per_day": parts,
                "losing": per_day < 0,
                "top_leak": state in leak_states,
            }
        )
    edges = [
        {
            "from": ids[e.source],
            "to": ids[e.target],
            "prob": _share(e.prob),
            "self": e.source == e.target,
        }
        for e in chain.edges
    ]
    return {
        "view": chain.view,
        "moves": chain.moves,
        "coverage": _share(chain.coverage),
        "nodes": nodes,
        "edges": edges,
    }


def _leak_rows(row: PolicyResult) -> list[dict[str, Any]]:
    return [
        {
            "state": leak.state,
            "share": _share(leak.share),
            "visits": leak.visits,
            "per_visit": _dollars(leak.per_visit),
            "per_run": _dollars(leak.per_run),
            "parts": {k: _dollars(v) for k, v in leak.parts.items()},
            "cause": leak.cause,
            "cause_words": leak.cause_words,
        }
        for leak in row.leaks
    ]


def _tuning(result: RaceResult) -> list[dict[str, Any]]:
    chosen = {r.policy: _settings(r.settings) for r in result.ranking()}
    groups: dict[str, list[dict[str, Any]]] = {}
    for trial in result.trials:
        settings = _settings(trial.settings)
        groups.setdefault(trial.policy, []).append(
            {
                "settings": settings,
                "score": None if trial.score is None else round(trial.score, 2),
                "dollars": _dollars(trial.dollars),
                "problem": None if trial.problem is None else str(trial.problem),
                "chosen": chosen.get(trial.policy) == settings,
            }
        )
    return [
        {"policy": name, "chosen": chosen.get(name), "tried": len(rows), "trials": rows}
        for name, rows in groups.items()
    ]


def _warnings(result: RaceResult, ranked: Sequence[PolicyResult]) -> list[dict[str, str]]:
    out = [
        {
            "kind": "dollars",
            "text": r.dollar_warning,
            "fix": "Check that the score is what you want.",
        }
        for r in ranked
        if r.dollar_warning
    ]
    out += [
        {"kind": "skipped", "text": f"{p.where}: {p.message}", "fix": p.fix}
        for p in result.problems
    ]
    return out


def _views(result: RaceResult) -> dict[str, Any]:
    table = result.profit_by_view()
    return {
        "observations": list(result.observations),
        "dollars": {
            policy: {mask: _dollars(v) for mask, v in row.items()} for policy, row in table.items()
        },
    }


# --- the whole model ---------------------------------------------------------------------------


def race_view_model(result: RaceResult, project: Project | None = None) -> dict[str, Any]:
    """The race as decision-first JSON for the report page (T-269, REQ-228, REQ-229)."""
    project = project or result.project
    settings = result.settings
    ranked = result.ranking()
    horizon = _horizon(project, ranked)
    runs_per_year = DAYS_PER_YEAR / horizon
    base, source, warnings = _pick_baseline(project, ranked)
    about = {
        "title": f"Policy race: {project.capsule_file.stem.removesuffix('.twin')}",
        "project": project.capsule_file.stem.removesuffix(".twin"),
        "episodes": settings.episodes,
        "seed": settings.seed,
        "tune_episodes": settings.tune_episodes,
        "horizon_days": horizon,
        "score": result.score_name,
        "score_is_dollars": result.score_name == "dollars",
        "interval": settings.interval,
        "observation": result.observation,
        "view": project.config.markov.view,
        "policies_raced": len(ranked),
        "method": (
            f"Every policy ran {settings.episodes} times on the same customer orders, each run "
            f"{horizon} days long. Policies with settings to try were tuned first on "
            f"{settings.tune_episodes} practice runs with other orders. Ranges are "
            f"{round(100 * settings.interval)}% ranges of the average, paired run by run."
        ),
    }
    model: dict[str, Any] = {"about": about}
    if not ranked or base is None:
        empty_note = "No policy finished the race. See the warnings."
        model.update({k: None for k in KEYS if k != "about"})
        model.update(
            recommendation={"policy": None, "sentence": empty_note, "why": ""},
            floor=[], drivers=[], policies=[], example_runs={}, markov={}, leaks={},
            tuning=_tuning(result), warnings=warnings + _warnings(result, ranked),
            products=_products(project), families=_families(project),
            parts=list(LEDGER_PARTS), views=_views(result),
            what_if=_what_if(),
        )  # fmt: skip
        return model
    best = ranked[0]
    same = best.policy == base.policy
    per_run = best.dollars.mean - base.dollars.mean
    per_year = per_run * runs_per_year
    paired = _paired(best, base, settings.interval)[0]
    drivers = _drivers(best, base)
    leak_states = {p: {x.state for x in r.leaks} for p, r in ((r.policy, r) for r in ranked)}
    runner_up = ranked[1] if len(ranked) > 1 else None
    model.update(
        recommendation={
            "policy": best.policy,
            "label": label(best.policy),
            "settings": _settings(best.settings),
            "dollars_per_run": _dollars(best.dollars.mean),
            "same_as_baseline": same,
            "sentence": _sentence(best, base, per_run, per_year),
            "why": _why(drivers, best, base)
            if not same
            else (
                f"The next best, {runner_up.policy}, made "
                f"{_money(best.dollars.mean - runner_up.dollars.mean)} less per run."
                if runner_up is not None
                else "It was the only policy in the race."
            ),
        },  # fmt: skip
        baseline={
            "policy": base.policy,
            "label": label(base.policy),
            "source": source,
            "settings": _settings(base.settings),
            "rank": base.rank,
            "dollars_per_run": _dollars(base.dollars.mean),
        },
        delta={
            "per_run": _dollars(per_run),
            "per_year": _dollars(per_year),
            "runs_per_year": round(runs_per_year, 2),
            "low_per_run": None if paired is None else _dollars(paired.low),
            "high_per_run": None if paired is None else _dollars(paired.high),
            "low_per_year": None
            if paired is None or paired.low is None
            else _dollars(paired.low * runs_per_year),
            "high_per_year": None
            if paired is None or paired.high is None
            else _dollars(paired.high * runs_per_year),
            "assumption": (
                f"A year is {DAYS_PER_YEAR} days, so about {runs_per_year:.1f} runs of "
                f"{horizon} days. Each run starts fresh, so this is a rough guide."
            ),
        },
        confidence=_confidence(best, base, settings.interval, same),
        floor=_floor(best, base, same),
        drivers=drivers,
        parts=list(LEDGER_PARTS),
        policies=[_policy_row(r, project, base, settings.interval) for r in ranked],
        products=_products(project),
        families=_families(project),
        example_runs=_example_runs(project, result),
        markov={
            name: _graph(chain, settings.episodes, leak_states.get(name, set()))
            for name, chain in result.chains.items()
        },
        leaks={r.policy: _leak_rows(r) for r in ranked},
        tuning=_tuning(result),
        views=_views(result),
        what_if=_what_if(),
        warnings=warnings + _warnings(result, ranked),
    )
    return model


def _what_if() -> dict[str, Any]:
    return {
        "parts": list(WHAT_IF_PARTS),
        "words": {p: PART_WORDS[p] for p in WHAT_IF_PARTS},
        "min": 0.0,
        "max": 3.0,
        "step": 0.1,
        "note": "Policies act the same here. Rerun the race to retune them.",
        "formula": "profit = revenue - production - setup x m1 - holding x m2 "
        "- lost_sales x m3 - late",
    }


__all__ = ["KEYS", "race_view_model", "label"]
