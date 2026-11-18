"""T-269, TF-006 B-05: the decision data behind the race report (REQ-228, REQ-229).

`race_view_model(result, project)` turns a `RaceResult` into one JSON-ready dict that the
report page reads. It leads with the decision: which policy to run, how much more money it
makes than the baseline ("what we do today", `experiment.race.baseline`) per run and per
year, how sure we are in plain words, and what changes on the floor. The stats behind it
(cost parts, daily profit bands, an example run, Markov maps with money, leaks, tuning and
warnings) come after.

Every number is rounded for people: dollars to whole dollars, units to one decimal, shares
to four decimals. Words are plain (sixth-grade reading level) with no em dashes.

B-05 adds the gaps of `docs/design/DATA-CONTRACT.md` 1.3 (R1 to R20) and the hero's floor
rows, plain labels and the longer check (`horizon_check`, R21 to R24).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
import numpy.typing as npt
import polars as pl

from twinflow import __version__
from twinflow.extensions.analysis import with_horizon
from twinflow.extensions.project import Project
from twinflow.extensions.race import (
    MoveTable,
    PolicyResult,
    RaceResult,
    Stat,
    iter_episodes,
    stat,
)
from twinflow.state.groups import GroupedView
from twinflow.state.markov import MarkovChain
from twinflow.state.records import LEDGER_PARTS, Move, unflatten

SCHEMA_VERSION = 1
DAYS_PER_YEAR = 365
LONG_CHECK_DAYS = 180
LOW_STOCK_DAYS = 3  # the `family` view's low band: under 3 days of cover
FALLBACK_BASELINE = "lowest_cover"
WHAT_IF_PARTS = ("setup", "holding", "lost_sales")
KEYS = (
    "view",
    "schema_version",
    "generated_at",
    "twinflow_version",
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
    "timing",
    "cautions",
    "economics",
    "problems",
    "agent_summary",
)
PART_WORDS = {
    "revenue": "sales",
    "production": "making product",
    "setup": "changeovers",
    "holding": "keeping stock",
    "lost_sales": "lost sales",
    "late": "late orders",
}
NAMES = {  # the names people see (R22)
    "s_big_s": "Reorder point",
    "setup_aware_runout": "Setup-aware run-out",
    "abc_wheel": "ABC wheel",
    "dollar_shortfall": "Biggest dollar shortfall",
    "myopic": "Best for today only",
    "idle": "Do nothing",
    "kanban": "Kanban",
    "lowest_cover": "Lowest cover",
    "lowest_stock_first": "Lowest stock first",
    "fixed_wheel": "Fixed wheel",
    "random": "Random",
    "family_campaign": "Family campaign",
}
COLOURS = frozenset(
    "black white grey gray red blue green yellow orange purple pink brown natural clear "
    "beige silver gold tan navy".split()
)  # families named like this are colours, so a change between them is a colour change
LEAK_TRY = {  # one thing to try, keyed on a leak's biggest cause (R19)
    "setup": "Try longer runs of one family so the {line} changes over less.",
    "holding": "Try smaller lots or lower targets so less stock sits on the shelf.",
    "lost_sales": "Try starting the short products sooner so they do not run out.",
    "late": "Try putting the late orders first.",
}
TOUCHY_SPREAD = 0.25  # tuning is touchy when settings spread more than 25% of the best


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
    # key, label, unit, lower is better (None: neither), plural noun for the words
    ("changeovers", "Changeovers per run", "count", True, "changeovers"),
    ("fill_rate", "Orders filled from stock", "share", False, "orders filled"),
    ("avg_stock", "Stock on hand", "units", True, "units in stock"),
    ("rest_days", "Days the {line} rests", "days", None, "rest days"),
    ("lost_units", "Sales lost per run", "units", True, "units of lost sales"),
)  # fmt: skip
ATTENTION = ("avg_stock", "rest_days")  # more of these needs a look (R21)


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


def _line_word(project: Project) -> str:
    """The plain word for the line: its own name when the capsule has one line."""
    line = project.facts.line
    return line.replace("_", " ") if line else "line"


def _switch_noun(project: Project) -> str:
    families = {f.lower() for f in project.facts.family.values()}
    if families and families <= COLOURS:
        return "Colour changes"
    return "Changes to another family"


def _floor_values(row: PolicyResult, key: str, horizon: int) -> float | None:
    if key == "rest_days":
        return row.idle_share * horizon
    value = getattr(row, key)
    return None if value is None else float(value)


def _short(key: str, mine: float, theirs: float, horizon: int) -> tuple[str, int]:
    """The right-hand words of a floor row, and the way it moved (-1, 0, +1)."""
    change = mine - theirs
    if key == "fill_rate":
        points = 100 * change
        if abs(points) < 0.05:
            return "About the same", 0
        return f"{points:+.1f} points", 1 if points > 0 else -1
    if key == "avg_stock" and theirs > 0 and mine / theirs >= 1.5:
        return f"{mine / theirs:.1f} times more", 1
    if key == "avg_stock" and mine > 0 and theirs / mine >= 1.5:
        return f"{round(100 * (1 - mine / theirs))}% less", -1
    small = 0.01 * abs(theirs) if key == "avg_stock" else 0.5
    if abs(change) < max(small, 0.05):
        return "About the same", 0
    way = 1 if change > 0 else -1
    amount = f"{abs(change):,.0f}" if abs(change) >= 1 or key != "lost_units" else "<1"
    more = "more" if way > 0 else "fewer"
    if key == "rest_days":
        return f"{amount} {more} of {horizon}", way
    return f"{amount} {more}", way


def _tail_rests(table: MoveTable | None, horizon: int) -> float:
    """Rest days per run in the last tenth of the run (at least 3 days), from kept moves."""
    if table is None or len(table) == 0:
        return 0.0
    frame = table.frame
    tail = max(3, round(0.1 * horizon))
    runs = frame["episode"].n_unique()
    late = frame.filter((pl.col("action") == "idle") & (pl.col("index") >= horizon - tail))
    return late.height / max(1, runs)


def _note(
    key: str,
    way: int,
    best: PolicyResult,
    base: PolicyResult,
    project: Project,
    horizon: int,
    tail_rests: float,
) -> str:
    """One "so what" sentence under a floor row (R21)."""
    if key == "changeovers":
        noun = _switch_noun(project)
        before, after = round(base.cross_family), round(best.cross_family)
        if before == after:
            words = f"{noun} stay at about {after:,}."
        else:
            words = f"{noun} {'drop' if after < before else 'rise'} from {before:,} to {after:,}."
        cost = project.facts.changeover.get("across_family", {}).get("cost", 0.0)
        return f"{words} Those cost {_money(cost)} each." if cost else words
    if key == "fill_rate":
        if way == 0:
            return "Customers find an empty shelf about as often."
        return f"{'Fewer' if way > 0 else 'More'} customers find an empty shelf."
    if key == "avg_stock":
        return {
            1: "You need the room and the cash for it.",
            -1: "Less cash sits on the shelf.",
            0: "You need about the same room and cash.",
        }[way]
    if key == "rest_days":
        line = _line_word(project)
        if way > 0:
            tail = f" Part of it is the test ending at day {horizon}." if tail_rests >= 1 else ""
            return f"Free time for upkeep.{tail}"
        if way < 0:
            return f"The {line} runs more of the time."
        return f"The {line} rests about as much as today."
    saved = base.parts.get("lost_sales", 0.0) - best.parts.get("lost_sales", 0.0)
    if abs(saved) < 0.5:
        return "Lost sales cost about the same."
    return f"That is {_money(abs(saved))} {'less' if saved > 0 else 'more'} in lost sales per run."


def _floor(
    best: PolicyResult,
    base: PolicyResult,
    same: bool,
    project: Project,
    horizon: int,
    kept: Mapping[str, MoveTable],
) -> list[dict[str, Any]]:
    rows = []
    line = _line_word(project)
    tail_rests = _tail_rests(kept.get(best.policy), horizon)
    for key, name, unit, lower_better, noun in FLOOR:
        mine, theirs = _floor_values(best, key, horizon), _floor_values(base, key, horizon)
        if mine is None or theirs is None:
            continue
        change = mine - theirs
        rounder = _share if unit == "share" else _units
        better = (
            None if lower_better is None or abs(change) < 1e-9 else (change < 0) == lower_better
        )
        short, way = ("No change", 0) if same else _short(key, mine, theirs, horizon)
        rows.append(
            {
                "key": key,
                "label": name.format(line=line),
                "unit": unit,
                "recommended": rounder(mine),
                "baseline": rounder(theirs),
                "change": rounder(change),
                "better": better,
                "words": _floor_words(change, unit, noun, same),
                "short": short,
                "note": "This is what you run today."
                if same
                else _note(key, way, best, base, project, horizon, tail_rests),
                "attention": key in ATTENTION and way > 0,
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
        return f"Keep running {label(best.policy)}. No other policy in the race made more money."
    more = "more" if per_run >= 0 else "less"
    return (
        f"Run {label(best.policy)} instead of {label(base.policy)}. It makes "
        f"{_money(abs(per_run))} {more} per run, about {_money(abs(per_year))} {more} a year."
    )


# --- run by run, day by day (R2, R5, R9) -------------------------------------------------------


def _gaps(best: PolicyResult, base: PolicyResult) -> npt.NDArray[np.float64]:
    return np.subtract(best.episode_dollars, base.episode_dollars)


def _typical(gaps: npt.NDArray[np.float64]) -> int | None:
    """The run whose gain is closest to the mean gain (the first one on a tie)."""
    return int(np.argmin(np.abs(gaps - gaps.mean()))) if len(gaps) else None


def _cumulative_gap(best: PolicyResult, base: PolicyResult) -> npt.NDArray[np.float64]:
    days = min(best.daily_dollars.shape[1], base.daily_dollars.shape[1])
    mine = np.cumsum(best.daily_dollars[:, :days], axis=1)
    theirs = np.cumsum(base.daily_dollars[:, :days], axis=1)
    return np.asarray(mine - theirs, dtype=float)


def _pass_day(mean: Sequence[float] | npt.NDArray[np.float64]) -> int | None:
    """The first day (from 1) after which the mean gap stays above zero to the end."""
    values = np.asarray(mean, dtype=float)
    if not len(values) or values[-1] <= 0:
        return None
    behind = np.flatnonzero(values <= 0)
    return int(behind[-1]) + 2 if len(behind) else 1


def _timing(best: PolicyResult, base: PolicyResult) -> dict[str, Any] | None:
    """When the pick pulls ahead of the baseline: the paired gap by day (R5)."""
    if best.policy == base.policy:
        return None
    gap = _cumulative_gap(best, base)
    if gap.size == 0:
        return None
    mean = gap.mean(axis=0)
    std = gap.std(axis=0, ddof=1) if gap.shape[0] > 1 else np.zeros(gap.shape[1])
    pass_day = _pass_day(mean)
    low_day = int(np.argmin(mean))
    lowest = float(mean[low_day])
    days = gap.shape[1]
    if pass_day is None:
        words = f"It is still behind today's rule on day {days}."
    elif pass_day == 1:
        words = "It is ahead of today's rule from the first day."
    elif lowest < 0:
        words = (
            f"It is behind today's rule until day {pass_day} of {days}. At its lowest, on day "
            f"{low_day + 1}, it is {_money(-lowest)} behind."
        )
    else:
        words = f"It pulls ahead of today's rule on day {pass_day} of {days}."
    return {
        "gap_by_day": {
            "mean": [_dollars(x) for x in mean],
            "low": [_dollars(x) for x in mean - std],
            "high": [_dollars(x) for x in mean + std],
        },
        "pass_day": pass_day,
        "lowest": {"day": low_day + 1, "dollars": _dollars(lowest)},
        "words": words,
    }


# --- before you switch (R6) --------------------------------------------------------------------


def _about(units: float) -> str:
    step = -2 if abs(units) >= 1000 else -1
    return f"{round(units, step):,.0f}"


def _one_in(share: float) -> str:
    return f"1 day in {max(1, round(1 / share))}"


def _family_out(row: PolicyResult, project: Project) -> dict[str, float]:
    """The mean out-of-stock share of each family's products."""
    groups: dict[str, list[float]] = {}
    for product, share in row.out_share.items():
        groups.setdefault(project.facts.family.get(product, product), []).append(share)
    return {family: float(np.mean(values)) for family, values in groups.items()}


def _caution(key: str, title: str, body: str, severity: str = "warn") -> dict[str, str]:
    return {"key": key, "title": title, "body": body, "severity": severity}


def _cautions(
    best: PolicyResult,
    base: PolicyResult,
    project: Project,
    timing: Mapping[str, Any] | None,
    confidence: Mapping[str, Any],
    tuning: Sequence[Mapping[str, Any]],
    horizon: int,
    long_check: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    """What to check before you switch, most important first (DATA-CONTRACT R6 rules)."""
    if best.policy == base.policy:
        return []
    out = []
    pass_day = None if timing is None else timing["pass_day"]
    if pass_day is not None and pass_day > 0.75 * horizon:
        body = (
            f"It is behind today's rule until day {pass_day} of {horizon}. "
            "Test a longer run before you commit."
        )
        if long_check is not None:
            body += f" {long_check['words']}"
        out.append(_caution("late_gain", "The gain comes late", body))
    mine, theirs = best.avg_stock, base.avg_stock
    if mine is not None and theirs is not None and theirs > 0 and mine >= 1.5 * theirs:
        out.append(
            _caution(
                "more_stock",
                f"You will hold {mine / theirs:.1f} times more stock",
                f"About {_about(mine)} units on the shelf, up from {_about(theirs)}. "
                "Check the room and the cash first.",
            )
        )
    short = {f: s for f, s in _family_out(best, project).items() if s > 0.10}
    if short:
        family = max(short, key=lambda f: short[f])
        today = _family_out(base, project).get(family, 0.0)
        then = (
            f"With today's rule it is about {_one_in(today)}."
            if today >= 0.01
            else "With today's rule they almost never run out."
        )
        out.append(
            _caution(
                "runs_short",
                f"{family.capitalize()} products still run short",
                f"Each {family} product is out about {_one_in(short[family])}. {then}",
            )
        )
    end, end_today = best.stock_end, base.stock_end
    if end is not None and end_today is not None and end < 0.7 * end_today:
        out.append(
            _caution(
                "end_stock",
                "Part of the gain is sold-down stock",
                f"It ends the run with about {_about(end)} units on the shelf. "
                f"Today's rule ends with {_about(end_today)}.",
            )
        )
    tuned = next((t for t in tuning if t["policy"] == best.policy), None)
    if tuned is not None and tuned["touchy"]:
        out.append(
            _caution(
                "touchy_tuning",
                "This rule is touchy",
                f"A wrong setting costs up to {_money(tuned['spread'])} per run. "
                "Recheck it when demand changes.",
                "note",
            )
        )
    if confidence.get("level") == "maybe_better":
        out.append(
            _caution(
                "maybe",
                "The lead is not proven",
                f"It won {confidence['wins']} of {confidence['runs']} runs. Run more test runs.",
            )
        )
    return out


# --- a longer check (R23) ----------------------------------------------------------------------


def _daily_totals(
    project: Project, row: PolicyResult, episodes: int, seed: int, days: int
) -> npt.NDArray[np.float64]:
    """Dollars per day of one policy (its race settings), episodes x days, streamed."""
    plan = project.episodes(episodes, seed)
    policy = project.extensions.get("policy", row.policy)
    out = np.zeros((episodes, days))
    runs = iter_episodes(
        project, policy, plan, settings=dict(row.settings), observation=row.observation
    )
    for episode, moves in enumerate(runs):
        for day, move in enumerate(moves[:days]):
            out[episode, day] = move.ledger.total
    return out


def horizon_check(
    project: Project, result: RaceResult, days: int = LONG_CHECK_DAYS
) -> dict[str, Any] | None:
    """Run only the pick and the baseline (their race settings) for `days` days, on the
    race's number of runs and seed, paired run by run (R23, the owner's yearly check).

    None when the race is already that long, the pick is the baseline, or the engine sets
    its own horizon."""
    ranked = result.ranking()
    if not ranked:
        return None
    base, _, _ = _pick_baseline(project, ranked)
    best = ranked[0]
    if base is None or best.policy == base.policy or _horizon(project, ranked) >= days:
        return None
    try:
        longer = with_horizon(project, days)
    except ValueError:
        return None
    runs, seed = result.settings.episodes, result.settings.seed
    mine = _daily_totals(longer, best, runs, seed, days)
    theirs = _daily_totals(longer, base, runs, seed, days)
    gaps = mine.sum(axis=1) - theirs.sum(axis=1)
    paired = stat(list(gaps), result.settings.interval)
    scale = DAYS_PER_YEAR / days
    per_year = paired.mean * scale
    more = "more" if per_year >= 0 else "less"
    return {
        "days": days,
        "runs": runs,
        "per_run": _dollars(paired.mean),
        "per_year": _dollars(per_year),
        "low_per_year": None if paired.low is None else _dollars(paired.low * scale),
        "high_per_year": None if paired.high is None else _dollars(paired.high * scale),
        "pass_day": _pass_day(np.cumsum(mine - theirs, axis=1).mean(axis=0)),
        "words": f"A {days}-day check gives about {_money(round(abs(per_year), -2))} {more} a year.",
    }


# --- the stats one click deeper ----------------------------------------------------------------


def _policy_row(
    row: PolicyResult, project: Project, base: PolicyResult | None, level: float
) -> dict[str, Any]:
    band = row.cumulative_dollars
    mean = np.asarray(band.mean)
    std = np.asarray(band.std)
    vs_base, wins = None, 0
    if base is not None and row.policy != base.policy:
        paired, wins, _ = _paired(row, base, level)
        vs_base = _stat(paired)
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
        "dollars_by_run": [_dollars(x) for x in row.episode_dollars],
        "wins_vs_baseline": wins,
        "stock_by_day": {"mean": [_units(x) for x in row.stock_by_day]},
        "stock_start": _units(row.stock_start),
        "stock_end": _units(row.stock_end),
        "stock_peak": _units(row.stock_peak),
        "out_share_by_product": {p: _share(v) for p, v in row.out_share.items()},
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


def _lost_by_product(move: Move) -> dict[str, float | None]:
    return {
        k.removeprefix("lost."): _units(v)
        for k, v in move.events.items()
        if k.startswith("lost.") and v > 0
    }


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
                "lost_by_product": _lost_by_product(move),
                "parts": {p: _dollars(getattr(move.ledger, p)) for p in LEDGER_PARTS},
                "profit": _dollars(move.ledger.total),
                "cumulative": _dollars(total),
            }
        )
    start = {p: int(round(v)) for p, v in moves[0].before.stock.items()} if moves else {}
    return {"episode": episode, "why": "typical", "start_stock": start, "days": days}


def _episode_moves(table: MoveTable, episode: int) -> list[Move]:
    frame = table.frame
    if frame.height == 0:
        return []
    return [unflatten(row) for row in frame.filter(pl.col("episode") == episode).to_dicts()]


def _example_runs(project: Project, result: RaceResult, episode: int | None) -> dict[str, Any]:
    """The same typical run (R9) for every policy whose moves were kept."""
    runs = {}
    for name, table in result.kept.items():
        moves = _episode_moves(table, episode) if episode is not None else []
        if moves:
            runs[name] = _example_run(project, episode or 0, moves)
            continue
        for first, part in table.episodes():  # no typical run: the first one
            runs[name] = {**_example_run(project, first, part), "why": "first"}
            break
    return runs


def _label_parts(text: str) -> dict[str, Any] | None:
    """`grey:ok black:high | natural` as levels and setup, when a label has that shape."""
    stock, _, setup = text.partition(" | ")
    pairs = [p.split(":", 1) for p in stock.split()]
    if not pairs or any(len(p) != 2 for p in pairs):
        return None
    return {"levels": {k: v for k, v in pairs}, "setup": setup or None}


DEFAULT_LEVELS = ("out", "low", "ok", "high")
NORMAL_LEVELS = frozenset({"ok", "normal"})


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _level_word(name: str) -> str:
    return "OK" if name == "ok" else name


def _join_and(items: list[str]) -> str:
    return items[0] if len(items) < 2 else f"{', '.join(items[:-1])} and {items[-1]}"


def _is_normal(level: str, names: Sequence[str]) -> bool:
    """The level a reader expects most days. It is left out of a state's title."""
    if any(n in NORMAL_LEVELS for n in names):
        return level in NORMAL_LEVELS
    odd = len(names) >= 3 and len(names) % 2 == 1
    return odd and level in names and names.index(level) == (len(names) - 1) // 2


def state_words(text: str, levels: Sequence[str] | None, line: str) -> dict[str, str]:
    """Plain words for a state label (DESIGN.md 3.8, R18). The one source of state words.

    `grey:high black:ok natural:low | black` reads "Grey high, natural low" and
    "Press on black". Families at one level are joined ("black and natural low"), the
    highest level comes first, the normal level is left out, and a state where every
    family sits at one level reads "All stock OK". A label of another shape is kept."""
    parts = _label_parts(text)
    if parts is None:
        return {"title": text, "setup": ""}
    names = list(levels) if levels else list(DEFAULT_LEVELS)
    setup = parts["setup"]
    if setup is None:
        where = ""
    elif setup == "idle":
        where = f"{_cap(line)} rests"
    else:
        where = f"{_cap(line)} on {setup}"
    by_level: dict[str, list[str]] = {}
    for family, level in parts["levels"].items():
        by_level.setdefault(level, []).append(family)
    if len(by_level) == 1:
        return {"title": f"All stock {_level_word(next(iter(by_level)))}", "setup": where}
    rank = {name: i for i, name in enumerate(names)}
    shown = sorted(
        (lv for lv in by_level if not _is_normal(lv, names)), key=lambda lv: -rank.get(lv, -1)
    )
    title = ", ".join(f"{_join_and(by_level[lv])} {_level_word(lv)}" for lv in shown)
    return {"title": _cap(title), "setup": where}


def _graph(
    chain: MarkovChain,
    episodes: int,
    leak_states: set[str],
    line: str = "line",
    levels: Sequence[str] | None = None,
) -> dict[str, Any]:
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
                "words": state_words(state, levels, line),
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


def _leak_rows(
    row: PolicyResult,
    chain: MarkovChain | None,
    line: str = "line",
    levels: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    ids = {state: f"s{i}" for i, state in enumerate(chain.states)} if chain is not None else {}
    return [
        {
            "state": leak.state,
            "state_id": ids.get(leak.state),
            "words": state_words(leak.state, levels, line),
            "share": _share(leak.share),
            "visits": leak.visits,
            "per_visit": _dollars(leak.per_visit),
            "per_run": _dollars(leak.per_run),
            "parts": {k: _dollars(v) for k, v in leak.parts.items()},
            "cause": leak.cause,
            "cause_words": leak.cause_words,
            "try": LEAK_TRY.get(leak.cause, "Look at what the {line} does here.").format(line=line),
        }
        for leak in row.leaks
    ]


def _tuning(result: RaceResult) -> list[dict[str, Any]]:
    chosen = {r.policy: _settings(r.settings) for r in result.ranking()}
    groups: dict[str, list[dict[str, Any]]] = {}
    raw: dict[str, list[float]] = {}
    for trial in result.trials:
        if trial.dollars is not None:
            raw.setdefault(trial.policy, []).append(trial.dollars)
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
    out = []
    for name, rows in groups.items():
        dollars = raw.get(name, [])
        spread = max(dollars) - min(dollars) if dollars else 0.0
        touchy = bool(dollars) and spread > TOUCHY_SPREAD * abs(max(dollars))
        out.append(
            {
                "policy": name,
                "chosen": chosen.get(name),
                "tried": len(rows),
                "trials": rows,
                "spread": _dollars(spread),
                "touchy": touchy,
            }
        )
    return out


def _same_rate(costs: Mapping[str, float], bases: Mapping[str, float]) -> float | None:
    """The one rate behind per-product costs (cost / base), or None when they differ."""
    rates = {round(costs[p] / bases[p], 6) for p in costs if bases.get(p)}
    return float(rates.pop()) if len(rates) == 1 else None


def _economics(project: Project) -> dict[str, Any]:
    """The money facts the words quote (R14)."""
    facts = project.facts
    changeover = {
        kind: {
            "cost": _dollars(values.get("cost", 0.0)),
            "output_lost": _share(values.get("output_lost", 0.0)),
        }
        for kind, values in facts.changeover.items()
    }
    return {
        "changeover": changeover,
        "holding_rate": _same_rate(facts.holding_cost, facts.unit_cost),
        "lost_sale_rate": _same_rate(facts.lost_sale_cost, facts.price),
    }


def _problems(result: RaceResult) -> list[dict[str, Any]]:
    """What could not run, with the fix (R15)."""
    return [
        {
            "policy": p.name if p.kind == "policy" else None,
            "file": None if p.file is None else f"{p.file.parent.name}/{p.file.name}",
            "line": p.line,
            "message": p.message,
            "fix": p.fix,
        }
        for p in result.problems
    ]


def _agent_summary(model: Mapping[str, Any]) -> list[str]:
    """Five to ten plain sentences for agents and mail previews (R16)."""
    rec, sure, delta = model["recommendation"], model["confidence"], model["delta"]
    out = [rec["sentence"]]
    if rec.get("same_as_baseline"):
        out.append(sure["detail"])
        out.append(rec["why"])
    else:
        out.append(
            f"{sure['words']}. It won {sure['wins']} of {sure['runs']} test runs on the same "
            "customer orders."
        )
        if delta["low_per_run"] is not None and delta["high_per_run"] is not None:
            out.append(
                f"The gain is likely between {_money(delta['low_per_run'])} and "
                f"{_money(delta['high_per_run'])} per run."
            )
        if rec["why"]:
            out.append(rec["why"])
        if model["timing"] is not None:
            out.append(model["timing"]["words"])
        out += [f"{c['title']}. {c['body']}" for c in model["cautions"][:3]]
        check = delta.get("long_check")
        if check is not None and not any(check["words"] in s for s in out):
            out.append(check["words"])
    about = model["about"]
    out.append(
        f"The race ran {about['policies_raced']} rules, {about['episodes']} times each, "
        f"{about['horizon_days']} days per run."
    )
    if model["problems"]:
        out.append(f"{len(model['problems'])} rules could not run. See the problems list.")
    return [s if s.endswith(".") else f"{s}." for s in out[:10]]


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


def _levels(project: Project) -> tuple[str | None, list[dict[str, Any]]]:
    """How the map's view groups stock, and its levels from low to high (R13)."""
    try:
        view = project.view(project.config.markov.view)
    except KeyError:
        return None, []
    if not isinstance(view, GroupedView) or view.stock is None:
        return None, []
    return view.stock.by, [{"name": x.name, "below": x.below} for x in view.stock.levels]


def _views(result: RaceResult, project: Project) -> dict[str, Any]:
    table = result.profit_by_view()
    by, levels = _levels(project)
    return {
        "by": by,
        "levels": levels,
        "observations": list(result.observations),
        "dollars": {
            policy: {mask: _dollars(v) for mask, v in row.items()} for policy, row in table.items()
        },
    }


# --- the whole model ---------------------------------------------------------------------------


def race_view_model(
    result: RaceResult,
    project: Project | None = None,
    *,
    long_check: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The race as decision-first JSON for the report page (T-269, REQ-228, REQ-229).

    `long_check` is the output of `horizon_check` (R23); it lands at `delta.long_check`
    and adds one sentence to the `late_gain` caution."""
    project = project or result.project
    settings = result.settings
    ranked = result.ranking()
    horizon = _horizon(project, ranked)
    runs_per_year = DAYS_PER_YEAR / horizon
    base, source, warnings = _pick_baseline(project, ranked)
    line = _line_word(project)
    level_names = [lv["name"] for lv in _levels(project)[1]] or None
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
    model: dict[str, Any] = {
        "view": "race",
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "twinflow_version": __version__,
        "about": about,
    }
    tuning = _tuning(result)
    if not ranked or base is None:
        empty_note = "No policy finished the race. See the warnings."
        model.update({k: None for k in KEYS if k not in model})
        model.update(
            recommendation={"policy": None, "sentence": empty_note, "why": ""},
            floor=[], drivers=[], policies=[], example_runs={}, markov={}, leaks={},
            tuning=tuning, warnings=warnings + _warnings(result, ranked),
            products=_products(project), families=_families(project),
            parts=list(LEDGER_PARTS), views=_views(result, project),
            what_if=_what_if(), timing=None, cautions=[], economics=_economics(project),
            problems=_problems(result),
        )  # fmt: skip
        model["agent_summary"] = [empty_note]
        return model
    best = ranked[0]
    same = best.policy == base.policy
    per_run = best.dollars.mean - base.dollars.mean
    per_year = per_run * runs_per_year
    paired = _paired(best, base, settings.interval)[0]
    gaps = _gaps(best, base)
    drivers = _drivers(best, base)
    leak_states = {p: {x.state for x in r.leaks} for p, r in ((r.policy, r) for r in ranked)}
    runner_up = ranked[1] if len(ranked) > 1 else None
    confidence = {
        **_confidence(best, base, settings.interval, same),
        "gaps": [_dollars(x) for x in gaps],
    }
    timing = _timing(best, base)
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
                f"The next best, {label(runner_up.policy)}, made "
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
            "long_check": None if long_check is None else dict(long_check),
        },
        confidence=confidence,
        floor=_floor(best, base, same, project, horizon, result.kept),
        drivers=drivers,
        parts=list(LEDGER_PARTS),
        policies=[_policy_row(r, project, base, settings.interval) for r in ranked],
        products=_products(project),
        families=_families(project),
        example_runs=_example_runs(project, result, None if same else _typical(gaps)),
        markov={
            name: _graph(chain, settings.episodes, leak_states.get(name, set()), line, level_names)
            for name, chain in result.chains.items()
        },
        leaks={
            r.policy: _leak_rows(r, result.chains.get(r.policy), line, level_names) for r in ranked
        },
        tuning=tuning,
        views=_views(result, project),
        what_if=_what_if(),
        warnings=warnings + _warnings(result, ranked),
        timing=timing,
        cautions=_cautions(best, base, project, timing, confidence, tuning, horizon, long_check),
        economics=_economics(project),
        problems=_problems(result),
    )
    model["agent_summary"] = _agent_summary(model)
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


__all__ = ["KEYS", "SCHEMA_VERSION", "horizon_check", "label", "race_view_model", "state_words"]
