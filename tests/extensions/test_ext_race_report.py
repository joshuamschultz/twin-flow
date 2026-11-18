"""T-242: the race report is one self-contained HTML page (COMP-158, REQ-207).

Plotly and Mermaid are inlined once (the report package's vendored blocks); our own markup
loads nothing from the network. The page has the ranking, profit over time with bands, the
cost parts, rank by day, the tuning trials, a Markov map per top policy, the money leaks
and the problems, each with a plain-word caption.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path

import pytest

from twinflow.extensions.project import Project
from twinflow.extensions.race import RaceResult, race
from twinflow.extensions.report import SECTIONS, race_html, write_legacy_race_report

MakeProject = Callable[..., Path]

LIB_BLOCK = re.compile(
    r"<script id=['\"]twinflow-(?:plotly|mermaid)-lib['\"]>.*?</script>", re.IGNORECASE | re.DOTALL
)
BROKEN = 'NAME = "broken"\n\ndef decide(state):\n    return "nope"\n'


@pytest.fixture(scope="module")
def raced(shared_project: MakeProject) -> RaceResult:
    folder = shared_project(
        {"race": {"episodes": 4, "tune_episodes": 2, "top_policies": 2}},
        files={"policies/broken.py": BROKEN},
    )
    return race(Project.open(folder), ["lowest_cover", "family_campaign", "idle", "broken"])


@pytest.fixture(scope="module")
def page(raced: RaceResult) -> str:
    return race_html(raced)


def _own(html: str) -> str:
    return LIB_BLOCK.sub("", html)


def test_the_page_loads_nothing_from_the_network(page: str) -> None:
    own = _own(page)
    assert re.search(r"https?://", own) is None
    assert "//cdn" not in own.lower()
    assert page.count("id='twinflow-plotly-lib'") == 1
    assert page.count("id='twinflow-mermaid-lib'") == 1


def test_every_section_is_there_with_a_caption(page: str) -> None:
    for section in SECTIONS:
        block = re.search(rf"<section id='{section}'>(.*?)</section>", page, re.DOTALL)
        assert block is not None, section
        assert "class='cap'" in block.group(1), section


def test_ranking_maps_leaks_and_problems_name_the_policies(page: str, raced: RaceResult) -> None:
    ranking = re.search(r"<section id='ranking'>(.*?)</section>", page, re.DOTALL)
    assert ranking is not None
    for row in raced.ranking():
        assert row.policy in ranking.group(1)
    maps = re.search(r"<section id='markov'>(.*?)</section>", page, re.DOTALL)
    assert maps is not None
    assert maps.group(1).count("<pre class='mermaid'>") == len(raced.chains) == 2
    leaks = re.search(r"<section id='leaks'>(.*?)</section>", page, re.DOTALL)
    assert leaks is not None and raced.ranking()[0].leaks[0].state in leaks.group(1)
    problems = re.search(r"<section id='problems'>(.*?)</section>", page, re.DOTALL)
    assert problems is not None and "broken.py" in problems.group(1)


def test_captions_are_plain_words(page: str) -> None:
    own = _own(page)
    text = re.sub(r"<script.*?</script>", "", own, flags=re.DOTALL)
    assert "—" not in text  # no em dashes
    captions = re.findall(r"<p class='cap'>(.*?)</p>", text, re.DOTALL)
    assert captions
    for caption in captions:
        words = re.sub(r"<[^>]+>", "", caption).split()
        sentences = max(1, len(re.findall(r"[.!?](\s|$)", caption)))
        assert len(words) / sentences <= 20, caption  # short sentences


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.open: list[str] = []
        self.scripts = 0
        self.bad: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("section", "table", "script", "div"):
            self.open.append(tag)
        if tag == "script":
            self.scripts += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("section", "table", "script", "div"):
            if not self.open or self.open[-1] != tag:
                self.bad.append(tag)
            else:
                self.open.pop()


def test_the_page_parses_with_balanced_scripts_and_sections(page: str) -> None:
    parser = _Tags()
    parser.feed(page)
    assert parser.bad == [] and parser.open == []
    assert parser.scripts >= 3
    assert page.count("<script") == page.count("</script>")


def test_write_race_report_writes_one_file(raced: RaceResult, tmp_path: Path) -> None:
    out = write_legacy_race_report(raced, tmp_path / "race.html")
    assert out == tmp_path / "race.html"
    assert out.read_text(encoding="utf-8") == race_html(raced)


def test_a_custom_score_shows_dollars_next_to_it(make_project: MakeProject) -> None:
    folder = make_project({"score": "margin_only", "race": {"episodes": 2, "tune_episodes": 1}})
    result = race(Project.open(folder), ["idle", "lowest_cover"])
    html = race_html(result)
    ranking = re.search(r"<section id='ranking'>(.*?)</section>", html, re.DOTALL)
    assert ranking is not None
    assert "margin_only" in ranking.group(1) and "Dollars" in ranking.group(1)
