"""COMP-158: the race report, one self-contained HTML page (REQ-207, D-152).

`race_html(result)` turns a `RaceResult` into one page a planner can open with no network:
Plotly and Mermaid are inlined once from the report package (D-026), and nothing in our own
markup points at the web. Since TF-006 B-14 the CLI's `race.html` is the decision view
(`write_race_report`, built by twinflow.report.decision); this page is kept as
`write_legacy_race_report` until B-33 removes it.

Sections, in order (`SECTIONS`): a summary, the ranking (score and dollars side by side,
D-152), profit over time with bands, where the money goes, rank by day, the tuning trials,
profit by view (REQ-226), a Markov map per top policy, the money leaks and the problems.
Every section has a short caption in plain words (sixth-grade reading level, no em dashes).
"""

from __future__ import annotations

import html as html_lib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import plotly.graph_objects as go  # type: ignore[import-untyped]  # no stub; D-026 lib choice

from twinflow.extensions.race import PolicyResult, RaceResult, Stat
from twinflow.report.html import REPORT_STYLE, figure_html, mermaid_script, plotly_script

SECTIONS = (
    "summary",
    "ranking",
    "profit",
    "parts",
    "rank",
    "tuning",
    "views",
    "markov",
    "leaks",
    "problems",
)
TITLES = {
    "summary": "The winner",
    "ranking": "Ranking",
    "profit": "Profit over time",
    "parts": "Where the money goes",
    "rank": "Rank by day",
    "tuning": "Tuning trials",
    "views": "Profit by view",
    "markov": "Markov maps",
    "leaks": "Money leaks",
    "problems": "Problems",
}
COLORS = (
    "#0073FE", "#F68D2E", "#0891B2", "#7B2CBF", "#2A9D8F", "#E76F51",
    "#3A6BA8", "#E9C46A", "#6A4C93", "#8D99AE", "#264653", "#B5179E",
)  # fmt: skip
PART_COLORS = {
    "revenue": "#2A9D8F",
    "production": "#E9C46A",
    "setup": "#E76F51",
    "holding": "#8D99AE",
    "lost_sales": "#6A4C93",
    "late": "#B5179E",
}
PART_WORDS = {
    "revenue": "sales",
    "production": "making",
    "setup": "changeovers",
    "holding": "keeping stock",
    "lost_sales": "lost sales",
    "late": "late orders",
}

_PAGE_STYLE = """
.cap{color:var(--muted);font-size:13.5px;margin:6px 2px 14px;max-width:780px;}
.warn{color:var(--orange);font-family:var(--fm);font-size:12px;}
pre.mermaid{background:var(--paper);border:1px solid var(--line);border-radius:11px;
  padding:14px;overflow:auto;}
ul.problems{padding-left:18px;font-size:13px;} ul.problems li{margin:6px 0;}
section{margin-bottom:10px;}
"""


def _e(text: object) -> str:
    return html_lib.escape(str(text), quote=False)


def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def _number(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def _share(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _range(item: Stat | None, money: bool = True) -> str:
    if item is None or item.low is None or item.high is None:
        return "n/a"
    shown = _money if money else _number
    return f"{shown(item.low)} to {shown(item.high)}"


def _settings(values: Any) -> str:
    items = dict(values)
    return ", ".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in items.items())


def _table(head: Sequence[str], rows: Sequence[Sequence[str]], numeric: set[int]) -> str:
    th = "".join(f"<th class='num'>{_e(h)}</th>" if i in numeric else f"<th>{_e(h)}</th>"
                 for i, h in enumerate(head))  # fmt: skip
    body = []
    for row in rows:
        cells = "".join(
            f"<td class='num'>{c}</td>" if i in numeric else f"<td>{c}</td>"
            for i, c in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _section(name: str, caption: str, body: str) -> str:
    return (
        f"<section id='{name}'><h2 class='sec'>{_e(TITLES[name])}</h2>"
        f"<p class='cap'>{caption}</p>{body}</section>"
    )


def _color(index: int) -> str:
    return COLORS[index % len(COLORS)]


def _rgba(color: str, alpha: float) -> str:
    red, green, blue = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({red},{green},{blue},{alpha})"


def _layout(fig: go.Figure, y_title: str, x_title: str = "Day") -> go.Figure:
    fig.update_layout(
        template="plotly_white",
        height=430,
        margin={"l": 60, "r": 20, "t": 20, "b": 50},
        legend={"orientation": "h", "y": -0.18},
        xaxis_title=x_title,
        yaxis_title=y_title,
        font={"family": "Inter, sans-serif", "size": 12},
    )
    return fig


# --- sections ----------------------------------------------------------------------------------


def _summary(result: RaceResult) -> str:
    ranked = result.ranking()
    if not ranked:
        return _section(
            "summary",
            "No policy finished the race. See the problems at the end of the page.",
            "",
        )
    best = ranked[0]
    caption = (
        f"Every policy ran {result.settings.episodes} times on the same customer orders. "
        f"{_e(best.policy)} came first. It made {_money(best.dollars.mean)} per run on "
        "average."
    )
    if result.score_name != "dollars":
        caption += f" The ranking uses the score {_e(result.score_name)}, not dollars."
    warnings = [r.dollar_warning for r in ranked if r.dollar_warning]
    body = "".join(f"<p class='warn'>{_e(w)}</p>" for w in warnings)
    return _section("summary", caption, body)


def _ranking(result: RaceResult) -> str:
    custom = result.score_name != "dollars"
    head = ["Rank", "Policy", "Settings"]
    head += [f"Score ({result.score_name})"] if custom else []
    head += ["Dollars per run", "95% range", "Vs the winner", "Orders filled", "Changeovers"]
    rows = []
    for row in result.ranking():
        cells = [str(row.rank), _e(row.policy), _e(_settings(row.settings) or "none")]
        if custom:
            cells.append(_number(row.score.mean, 2))
        gap = "winner" if row.vs_best is None else _money(row.vs_best.mean)
        cells += [
            _money(row.dollars.mean),
            _range(row.dollars),
            gap,
            _share(row.fill_rate),
            _number(row.changeovers),
        ]
        rows.append(cells)
    caption = (
        "Each row is one policy. Dollars per run is the average profit of one run. "
        "The 95% range shows where the true average most likely sits. "
        "Vs the winner is the average gap, run by run, on the same orders."
    )
    if custom:
        caption += f" Policies are ranked by {_e(result.score_name)}. Dollars are shown too."
    numeric = set(range(3, len(head)))
    return _section("ranking", caption, _table(head, rows, numeric))


def _profit(result: RaceResult) -> str:
    fig = go.Figure()
    for i, row in enumerate(result.ranking()):
        mean = list(row.cumulative_dollars.mean)
        std = list(row.cumulative_dollars.std)
        days = list(range(1, len(mean) + 1))
        color = _color(i)
        upper = [m + s for m, s in zip(mean, std, strict=True)]
        lower = [m - s for m, s in zip(mean, std, strict=True)]
        group = row.policy
        fig.add_trace(
            go.Scatter(x=days, y=upper, mode="lines", line={"width": 0}, hoverinfo="skip",
                       showlegend=False, legendgroup=group)
        )  # fmt: skip
        fig.add_trace(
            go.Scatter(x=days, y=lower, mode="lines", line={"width": 0}, fill="tonexty",
                       fillcolor=_rgba(color, 0.15), hoverinfo="skip", showlegend=False,
                       legendgroup=group)
        )  # fmt: skip
        fig.add_trace(
            go.Scatter(x=days, y=mean, mode="lines", name=row.policy, line={"color": color},
                       legendgroup=group)
        )  # fmt: skip
    _layout(fig, "Profit so far ($)")
    caption = (
        "The line is the average profit so far on each day. "
        "The shaded band is one spread up and down, so most runs fall inside it. "
        "A policy that starts slow can still end on top."
    )
    return _section(
        "profit", caption, f"<div class='panel'>{figure_html(fig, 'race-profit')}</div>"
    )


def _parts(result: RaceResult) -> str:
    ranked = result.ranking()
    names = [r.policy for r in ranked]
    fig = go.Figure()
    parts = list(PART_COLORS)
    for part in parts:
        values = [r.parts.get(part, 0.0) * (1 if part == "revenue" else -1) for r in ranked]
        if not any(values):
            continue
        fig.add_trace(
            go.Bar(x=names, y=values, name=PART_WORDS[part], marker_color=PART_COLORS[part])
        )
    fig.add_trace(
        go.Scatter(x=names, y=[r.dollars.mean for r in ranked], mode="markers", name="profit",
                   marker={"color": "#0B1220", "size": 10})
    )  # fmt: skip
    fig.update_layout(barmode="relative")
    _layout(fig, "Dollars per run", x_title="")
    caption = (
        "Money in goes up and money out goes down. The dot is the profit that is left. "
        "Look for tall bars below the line: that is where money leaves."
    )
    return _section("parts", caption, f"<div class='panel'>{figure_html(fig, 'race-parts')}</div>")


def _rank(result: RaceResult) -> str:
    ranks = result.rank_by_day()
    fig = go.Figure()
    for i, (name, by_day) in enumerate(ranks.items()):
        fig.add_trace(
            go.Scatter(x=list(range(1, len(by_day) + 1)), y=by_day, mode="lines", name=name,
                       line={"color": _color(i), "shape": "hv"})
        )  # fmt: skip
    fig.update_yaxes(autorange="reversed", dtick=1)
    _layout(fig, "Rank (1 is best so far)")
    leader = result.early_leader
    caption = "Rank 1 is the policy ahead so far on that day."
    if leader is not None:
        caption += f" {_e(leader)} led after day 1. Leaders early are not always leaders late."
    return _section("rank", caption, f"<div class='panel'>{figure_html(fig, 'race-rank')}</div>")


def _tuning(result: RaceResult) -> str:
    if not result.trials:
        return _section(
            "tuning",
            "No policy had settings to try, so nothing was tuned.",
            "",
        )
    best = {r.policy: dict(r.settings) for r in result.ranking()}
    rows = []
    for trial in result.trials:
        mark = "best" if best.get(trial.policy) == dict(trial.settings) else ""
        if trial.problem is not None:
            mark = "broke"
        rows.append(
            [
                _e(trial.policy),
                _e(_settings(trial.settings)),
                _number(trial.score, 2) if result.score_name != "dollars" else _money(trial.score),
                _money(trial.dollars),
                mark,
            ]
        )
    caption = (
        f"Each policy tried every setting in its list on {result.settings.tune_episodes} "
        "practice runs. These runs use other orders than the race, so a setting cannot win "
        "by luck on the race orders. The best setting was used in the race."
    )
    table = _table(["Policy", "Setting", "Score", "Dollars per run", ""], rows, {2, 3})
    return _section("tuning", caption, table)


def _views(result: RaceResult) -> str:
    table = result.profit_by_view()
    masks = list(result.observations)
    rows = [[_e(policy), *(_money(row.get(m)) for m in masks)] for policy, row in table.items()]
    caption = (
        "Each column hides some facts from the policy. The press still runs for real. "
        "The drop from the first column is what that information is worth."
    )
    if len(masks) == 1:
        caption = (
            f"Every policy saw the {_e(masks[0])} view. "
            "Add more views to experiment.observation to see what information is worth."
        )
    numeric = set(range(1, len(masks) + 1))
    return _section("views", caption, _table(["Policy", *masks], rows, numeric))


def _markov(result: RaceResult) -> str:
    if not result.chains:
        return _section("markov", "No Markov map could be drawn for this race.", "")
    blocks = []
    for name, chain in result.chains.items():
        blocks.append(
            f"<h3 class='tbl'>{_e(name)}: top {len(chain.states)} states, "
            f"{chain.coverage:.0%} of all days</h3>"
            f"<pre class='mermaid'>{_e(chain.to_mermaid())}</pre>"
        )
    view = next(iter(result.chains.values())).view
    caption = (
        f"Each box is a kind of day in the {_e(view)} view. The number in the box is how "
        "often that day comes up. An arrow shows where the plant goes next and how likely "
        "that is. A thick box is a state the plant tends to stay in."
    )
    return _section("markov", caption, "".join(blocks))


def _leak_rows(row: PolicyResult) -> list[list[str]]:
    return [
        [
            _e(row.policy),
            _e(leak.state),
            _share(leak.share),
            _money(leak.per_run),
            _e(leak.cause_words),
        ]
        for leak in row.leaks
    ]


def _leaks(result: RaceResult) -> str:
    rows = [r for row in result.ranking()[: result.settings.top_policies] for r in _leak_rows(row)]
    caption = (
        "These are the kinds of days where the best policies lose the most money in one run. "
        "The cause is the biggest cost on those days."
    )
    head = ["Policy", "State", "Share of days", "Lost per run", "Main cause"]
    return _section("leaks", caption, _table(head, rows, {2, 3}))


def _problems(result: RaceResult) -> str:
    if not result.problems:
        return _section("problems", "No problems. Every policy finished the race.", "")
    items = "".join(
        f"<li><b>{_e(p.where)}</b>: {_e(p.message)} <i>Fix:</i> {_e(p.fix)}</li>"
        for p in result.problems
    )
    caption = (
        "These policies were left out of the race. Each line says what broke and how to fix it."
    )
    return _section("problems", caption, f"<ul class='problems'>{items}</ul>")


def _start_mermaid() -> str:
    return (
        "<script>window.addEventListener('load',function(){"
        "if(typeof mermaid==='undefined')return;"
        "try{mermaid.run({querySelector:'pre.mermaid'});}catch(e){}});</script>"
    )


def race_html(result: RaceResult) -> str:
    """The race report as one self-contained HTML page (COMP-158, REQ-207)."""
    project = result.project
    title = f"Policy race: {project.capsule_file.stem.removesuffix('.twin')}"
    head = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_e(title)}</title><style>{REPORT_STYLE}{_PAGE_STYLE}</style>"
        f"{plotly_script()}</head><body><div class='wrap'>"
        f"<header class='report'><div class='title'><h1>{_e(title)}</h1>"
        f"<div class='sub'>{result.settings.episodes} runs per policy, seed "
        f"{result.settings.seed}, score {_e(result.score_name)}</div></div></header>"
        "<div class='body'>"
    )
    sections = [
        _summary(result),
        _ranking(result),
        _profit(result),
        _parts(result),
        _rank(result),
        _tuning(result),
        _views(result),
        _markov(result),
        _leaks(result),
        _problems(result),
    ]
    tail = f"</div></div>{mermaid_script()}{_start_mermaid()}</body></html>"
    return head + "".join(sections) + tail + "\n"


def write_legacy_race_report(result: RaceResult, path: Path | str) -> Path:
    """The old Plotly page (`race_html`). Kept only for the transition; B-33 removes it."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(race_html(result), encoding="utf-8")
    return out


def write_race_report(result: RaceResult, path: Path | str, *, long_check: bool = True) -> Path:
    """Write the race as the decision view (TF-006 T-270, B-14): `race.html` next to
    `race.json`. With `long_check`, the pick and the baseline also run for 180 days so the
    yearly figure can be checked on the first screen (owner decision 1)."""
    from twinflow.extensions.report_model import horizon_check, race_view_model
    from twinflow.report import decision

    check = horizon_check(result.project, result) if long_check else None
    return decision.write(path, race_view_model(result, result.project, long_check=check))


__all__ = ["SECTIONS", "race_html", "write_legacy_race_report", "write_race_report"]
