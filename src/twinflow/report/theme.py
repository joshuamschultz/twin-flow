"""B-38: chart defaults for notebooks and scripts, read from the design tokens.

The web views read `--ba-*` and `--tf-*` CSS tokens. `web/scripts/export-tokens.mjs` writes the
same values to `tokens.json`. A copy of that file ships in this package (`assets/tokens.json`),
and a test keeps the two equal. So a Python chart and a React chart use the same colours and fonts.

    from twinflow.report import theme
    theme.apply()                      # Plotly and Matplotlib defaults
    fig = theme.question(fig, "When does it pull ahead of today?", "It leads after day 12.")

Rules from DESIGN.md: one chart answers one question, so the title is a question; the takeaway
sits under it; today is grey, the pick is blue, the rest are quiet; numbers are differences from
today.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

MODES = ("light", "dark")
TEMPLATE_NAME = "twinflow"
_SERIES = tuple(f"--tf-fam-{i}" for i in range(1, 6))


@cache
def tokens() -> dict[str, Any]:
    """The whole token file: `light` and `dark`, each a map of token name to value."""
    text = resources.files("twinflow.report").joinpath("assets/tokens.json").read_text("utf-8")
    loaded: dict[str, Any] = json.loads(text)
    return loaded


@dataclass(frozen=True)
class Palette:
    """The token values a chart needs, for one mode."""

    mode: str
    pick: str
    today: str
    gain: str
    gain_fill: str
    loss: str
    loss_text: str
    series: tuple[str, ...]
    quiet: str
    grid: str
    axis: str
    page: str
    text: str
    headline: str
    meta: str
    font_body: str
    font_display: str


@cache
def palette(mode: str = "light") -> Palette:
    """The palette for `light` or `dark`."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
    t = tokens()[mode]
    return Palette(
        mode=mode,
        pick=t["--tf-pick"],
        today=t["--tf-today"],
        gain=t["--tf-gain"],
        gain_fill=t["--tf-gain-fill"],
        loss=t["--tf-loss"],
        loss_text=t["--tf-loss-text"],
        series=tuple(t[name] for name in _SERIES),
        quiet=t["--ba-dark-slate"],
        grid=t["--tf-grid"],
        axis=t["--tf-axis"],
        page=t["--tf-page"],
        text=t["--ba-body"],
        headline=t["--ba-headline"],
        meta=t["--tf-meta"],
        font_body=t["--ba-font-body"],
        font_display=t["--ba-font-display"],
    )


def _font_stack(css: str) -> str:
    """A CSS font list as Plotly wants it: no double quotes."""
    return css.replace('"', "")


def plotly_template(mode: str = "light") -> Any:
    """A Plotly template that draws with the palette."""
    import plotly.graph_objects as go  # type: ignore[import-untyped]

    p = palette(mode)
    axis = {
        "gridcolor": p.grid,
        "linecolor": p.axis,
        "zerolinecolor": p.axis,
        "tickcolor": p.axis,
        "ticks": "outside",
        "title": {"font": {"color": p.meta, "size": 12}},
    }
    return go.layout.Template(
        layout=go.Layout(
            colorway=list(p.series),
            paper_bgcolor=p.page,
            plot_bgcolor=p.page,
            font={"family": _font_stack(p.font_body), "color": p.text, "size": 13},
            title={
                "font": {"family": _font_stack(p.font_display), "color": p.headline, "size": 18}
            },
            xaxis=axis,
            yaxis=axis,
            legend={"font": {"size": 12}},
            margin={"t": 90, "l": 60, "r": 30, "b": 55},
        )
    )


def matplotlib_params(mode: str = "light") -> dict[str, Any]:
    """Matplotlib `rcParams` that draw with the palette."""
    from cycler import cycler

    p = palette(mode)
    return {
        "axes.prop_cycle": cycler(color=[c.lower() for c in p.series]),
        "figure.facecolor": p.page,
        "axes.facecolor": p.page,
        "savefig.facecolor": p.page,
        "axes.edgecolor": p.axis,
        "axes.labelcolor": p.meta,
        "axes.titlecolor": p.headline,
        "axes.grid": True,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "grid.color": p.grid,
        "xtick.color": p.axis,
        "ytick.color": p.axis,
        "text.color": p.text,
        "font.family": "sans-serif",
        "font.sans-serif": ["Inter", "Helvetica", "Arial", "DejaVu Sans"],
        "axes.titlesize": 15,
        "axes.titlelocation": "left",
        "axes.titleweight": "bold",
        "legend.frameon": False,
    }


def apply(mode: str = "light") -> Palette:
    """Make the palette the default for Plotly, and for Matplotlib when it is installed."""
    import plotly.io as pio  # type: ignore[import-untyped]

    pio.templates[TEMPLATE_NAME] = plotly_template(mode)
    pio.templates.default = TEMPLATE_NAME
    try:
        import matplotlib as mpl
    except ImportError:
        pass
    else:
        mpl.rcParams.update(matplotlib_params(mode))  # type: ignore[arg-type]
    return palette(mode)


def colors_for(
    names: Iterable[str],
    *,
    pick: str | None = None,
    today: str | None = None,
    mode: str = "light",
) -> dict[str, str]:
    """One fixed colour for each name, so a rule looks the same in every chart.

    With `pick` or `today` the chart has a focus: the pick is blue, today is grey and every
    other name is quiet. With neither, names take the series colours in turn."""
    p = palette(mode)
    ordered = list(names)
    if pick is None and today is None:
        return {name: p.series[i % len(p.series)] for i, name in enumerate(ordered)}
    out = {name: p.quiet for name in ordered}
    if today in out:
        out[str(today)] = p.today
    if pick in out:
        out[str(pick)] = p.pick
    return out


def sequential_scale(mode: str = "light") -> list[list[Any]]:
    """A three-step colour scale for heatmaps: low, ok, high (`--tf-seq-1` to `--tf-seq-3`)."""
    t = tokens()[mode]
    return [[0.0, t["--tf-seq-1"]], [0.5, t["--tf-seq-2"]], [1.0, t["--tf-seq-3"]]]


def level_scale(mode: str = "light") -> list[list[Any]]:
    """A low, medium, high scale for stock: out of stock is orange, then two blues."""
    t = tokens()[mode]
    return [[0.0, t["--tf-out"]], [0.5, t["--tf-seq-2"]], [1.0, t["--tf-seq-3"]]]


def faded(color: str, alpha: float = 0.18) -> str:
    """A see-through version of a `#rrggbb` colour, for bands."""
    red, green, blue = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({red},{green},{blue},{alpha})"


def delta_text(value: float, unit: str = "$") -> str:
    """A difference from today in words, for a label: `+$22,088 vs today`."""
    if round(value) == 0:
        return "same as today"
    sign = "+" if value > 0 else "−"
    return f"{sign}{unit}{abs(value):,.0f} vs today"


def _check_question(text: str) -> None:
    if not text.strip().endswith("?"):
        raise ValueError(f"A chart title is the question it answers. Add a question mark: {text!r}")


def question(chart: Any, text: str, takeaway: str | None = None) -> Any:
    """Give a chart its question as title, with the takeaway under it.

    `chart` is a Plotly figure or a Matplotlib axes. Returns the same object."""
    _check_question(text)
    if hasattr(chart, "update_layout"):
        title = text if takeaway is None else f"{text}<br><sup>{takeaway}</sup>"
        chart.update_layout(title={"text": title, "x": 0, "xanchor": "left"})
        return chart
    chart.set_title(text, loc="left")
    if takeaway:
        chart.figure.subplots_adjust(bottom=0.2)
        chart.figure.text(
            0.01, 0.02, takeaway, ha="left", va="bottom", color=palette().meta, fontsize=11
        )
    return chart


def vs_today(table: Mapping[str, Iterable[float]], today: str) -> dict[str, list[float]]:
    """Each series minus the `today` series, point by point. Today becomes a zero line."""
    base = list(table[today])
    return {
        name: [v - b for v, b in zip(values, base, strict=True)] for name, values in table.items()
    }


__all__ = [
    "MODES",
    "Palette",
    "apply",
    "colors_for",
    "delta_text",
    "faded",
    "matplotlib_params",
    "palette",
    "plotly_template",
    "level_scale",
    "question",
    "sequential_scale",
    "tokens",
    "vs_today",
]
