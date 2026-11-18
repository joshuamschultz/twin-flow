"""B-38: the Python chart theme follows the design tokens (DESIGN.md 0.1, 8, 9.4).

`twinflow.report.theme` reads `tokens.json` and sets Matplotlib and Plotly defaults. The colours
a chart draws must be the token values, light and dark. The copy of the tokens that ships in the
package must equal the one the web build writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from twinflow.report import decision, theme

ROOT = Path(__file__).resolve().parents[2]
WEB_TOKENS = ROOT / "web" / "src" / "decision" / "tokens.json"
EXAMPLE = ROOT / "docs" / "design" / "race-view-model.example.json"
MODES = ("light", "dark")


def test_packaged_tokens_equal_the_web_tokens() -> None:
    assert theme.tokens() == json.loads(WEB_TOKENS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode", MODES)
def test_palette_values_are_token_values(mode: str) -> None:
    tokens = theme.tokens()[mode]
    palette = theme.palette(mode)
    assert palette.pick == tokens["--tf-pick"]
    assert palette.today == tokens["--tf-today"]
    assert palette.gain_fill == tokens["--tf-gain-fill"]
    assert palette.loss == tokens["--tf-loss"]
    assert palette.grid == tokens["--tf-grid"]
    assert palette.axis == tokens["--tf-axis"]
    assert palette.page == tokens["--tf-page"]
    assert palette.text == tokens["--ba-body"]
    assert palette.headline == tokens["--ba-headline"]
    assert palette.quiet == tokens["--ba-dark-slate"]
    assert palette.series == tuple(tokens[f"--tf-fam-{i}"] for i in range(1, 6))


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="light"):
        theme.palette("sepia")


@pytest.mark.parametrize("mode", MODES)
def test_plotly_template_uses_the_palette(mode: str) -> None:
    palette = theme.palette(mode)
    template = theme.plotly_template(mode)
    assert tuple(template.layout.colorway) == palette.series
    assert template.layout.paper_bgcolor == palette.page
    assert template.layout.xaxis.gridcolor == palette.grid
    assert template.layout.xaxis.linecolor == palette.axis
    assert template.layout.font.color == palette.text
    assert "Inter" in template.layout.font.family


def test_apply_sets_the_plotly_default() -> None:
    import plotly.graph_objects as go
    import plotly.io as pio

    theme.apply("light")
    assert pio.templates.default == "twinflow"
    fig = go.Figure(go.Scatter(x=[1, 2], y=[1, 2]))
    assert fig.layout.template.layout.colorway == theme.plotly_template("light").layout.colorway


@pytest.mark.parametrize("mode", MODES)
def test_matplotlib_defaults_use_the_palette(mode: str) -> None:
    pytest.importorskip("matplotlib")
    import matplotlib as mpl
    from matplotlib import rcParamsDefault

    palette = theme.apply(mode)
    try:
        cycle = [c["color"] for c in mpl.rcParams["axes.prop_cycle"]]
        assert cycle == [c.lower() for c in palette.series]
        assert mpl.colors.same_color(mpl.rcParams["axes.edgecolor"], palette.axis)
        assert mpl.colors.same_color(mpl.rcParams["grid.color"], palette.grid)
        assert mpl.colors.same_color(mpl.rcParams["figure.facecolor"], palette.page)
        assert mpl.colors.same_color(mpl.rcParams["text.color"], palette.text)
    finally:
        mpl.rcParams.update(rcParamsDefault)


def test_colors_for_marks_pick_and_today_and_quiets_the_rest() -> None:
    palette = theme.palette("light")
    colors = theme.colors_for(["a", "b", "c", "d"], pick="b", today="c")
    assert colors["b"] == palette.pick
    assert colors["c"] == palette.today
    assert colors["a"] == colors["d"] == palette.quiet
    assert palette.quiet not in (palette.pick, palette.today)


def test_colors_for_without_focus_cycles_the_series() -> None:
    palette = theme.palette("light")
    names = [f"n{i}" for i in range(7)]
    colors = theme.colors_for(names)
    assert [colors[n] for n in names[:5]] == list(palette.series)
    assert colors["n5"] == palette.series[0]


def test_faded_makes_a_see_through_colour() -> None:
    assert theme.faded("#0073fe", 0.5) == "rgba(0,115,254,0.5)"


def test_question_sets_the_plotly_title_and_takeaway() -> None:
    import plotly.graph_objects as go

    fig = theme.question(go.Figure(), "When does it pull ahead of today?", "It leads after day 12.")
    assert fig.layout.title.text.startswith("When does it pull ahead of today?")
    assert "It leads after day 12." in fig.layout.title.text


def test_question_must_be_a_question() -> None:
    import plotly.graph_objects as go

    with pytest.raises(ValueError, match="question"):
        theme.question(go.Figure(), "Profit by day")


def test_question_sets_a_matplotlib_title() -> None:
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    theme.question(ax, "Which rule earns most?", "Family campaign does.")
    assert ax.get_title(loc="left") == "Which rule earns most?"
    assert any("Family campaign does." in t.get_text() for t in fig.texts)
    plt.close(fig)


def test_delta_text_is_a_difference_from_today() -> None:
    assert theme.delta_text(22088) == "+$22,088 vs today"
    assert theme.delta_text(-1500) == "−$1,500 vs today"
    assert theme.delta_text(0) == "same as today"


def test_show_wraps_the_offline_page_in_an_iframe() -> None:
    view_model = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    shown = decision.show(view_model, height=700)
    html_text = shown._repr_html_()
    assert html_text.startswith("<iframe")
    assert 'sandbox="allow-scripts"' in html_text
    assert 'height="700"' in html_text
    assert "srcdoc=" in html_text
    assert "<script" not in html_text  # the page is escaped inside the attribute


def test_vs_today_turns_the_baseline_into_a_zero_line() -> None:
    out = theme.vs_today({"today": [1.0, 2.0], "pick": [3.0, 5.0]}, "today")
    assert out == {"today": [0.0, 0.0], "pick": [2.0, 3.0]}
