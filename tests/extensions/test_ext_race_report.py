"""T-242 and TF-006 B-33: the race report is one self-contained HTML page (COMP-158, REQ-207).

Since B-33 `race.html` is the decision view: `race_report_model` builds the view model and
`twinflow.report.decision` writes it into the committed offline template. Plotly, Mermaid and
`race_html` are gone from this path.

The old tests move one for one (BUILD-PLAN B-33 feature inventory):

- the page loads nothing from the network: `test_the_page_loads_nothing_from_the_network`
  (plus no Plotly, no Mermaid, size) and `test_the_old_plotly_writer_is_gone`
- every section is there with a caption: `test_every_old_section_has_a_new_place`
  (the inventory below) and `test_the_takeaways_are_there`
- ranking, maps, leaks and problems name the policies:
  `test_ranking_maps_leaks_and_problems_name_the_policies`
- captions are plain words: `test_words_are_plain`
- the page parses with balanced scripts and sections:
  `test_the_page_parses_with_two_scripts_and_a_noscript`
- write_race_report writes one file: `test_write_race_report_writes_one_file`
- a custom score shows dollars next to it: `test_a_custom_score_shows_dollars_next_to_it`
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from twinflow.extensions import report
from twinflow.extensions.project import Project
from twinflow.extensions.race import RaceResult, race
from twinflow.extensions.report import race_report_model, write_race_report
from twinflow.report import decision

MakeProject = Callable[..., Path]

BROKEN = 'NAME = "broken"\n\ndef decide(state):\n    return "nope"\n'
NAMESPACES = (
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1998/Math/MathML",
    "http://www.w3.org/XML/1998/namespace",
    "http://www.w3.org/2000/xmlns/",
)
# Old section -> the view model part the race view draws it from (BUILD-PLAN B-33).
INVENTORY = {
    "summary": "recommendation",  # The winner -> hero
    "ranking": "policies",  # Ranking -> All rules + Details table
    "profit": "timing",  # Profit over time -> "When does it pull ahead"
    "parts": "drivers",  # Where the money goes -> waterfall + Compare
    "rank": "timing",  # Rank by day -> timing facts (and the early-leader note in Details)
    "tuning": "tuning",  # Tuning trials -> Tuning
    "views": "views",  # Profit by view -> Details, "What is information worth"
    "markov": "markov",  # Markov maps -> State map
    "leaks": "leaks",  # Money leaks -> Money leaks
    "problems": "problems",  # Problems -> Details
}


@pytest.fixture(scope="module")
def raced(shared_project: MakeProject) -> RaceResult:
    folder = shared_project(
        {"race": {"episodes": 4, "tune_episodes": 2, "top_policies": 2}},
        files={"policies/broken.py": BROKEN},
    )
    return race(Project.open(folder), ["lowest_cover", "family_campaign", "idle", "broken"])


@pytest.fixture(scope="module")
def written(raced: RaceResult, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    path = write_race_report(raced, tmp_path_factory.mktemp("race") / "race.html")
    return path, path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page(written: tuple[Path, str]) -> str:
    return written[1]


@pytest.fixture(scope="module")
def model(page: str) -> dict[str, Any]:
    found = re.search(r'<script[^>]*id="twinflow-data"[^>]*>(.*?)</script>', page, re.DOTALL)
    assert found is not None
    data: dict[str, Any] = json.loads(found.group(1))
    return data


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def test_the_page_loads_nothing_from_the_network(written: tuple[Path, str]) -> None:
    path, page = written
    urls = re.findall(r"https?://[^\s\"'`<>)\\]*", page)
    assert [u for u in urls if not u.startswith(NAMESPACES)] == []
    assert "//cdn" not in page.lower()
    assert "plotly" not in page.lower() and "mermaid" not in page.lower()
    assert path.stat().st_size < 1.2 * 1024 * 1024


def test_the_old_plotly_writer_is_gone() -> None:
    for name in ("race_html", "write_legacy_race_report", "SECTIONS"):
        assert not hasattr(report, name), name
    source = Path(report.__file__).read_text(encoding="utf-8")
    for used in ("import plotly", "plotly_script", "mermaid_script", "to_mermaid"):
        assert used not in source, used


def test_every_old_section_has_a_new_place(model: dict[str, Any]) -> None:
    assert model["view"] == "race"
    for old, key in INVENTORY.items():
        assert model.get(key), f"{old} -> {key} is empty"
    assert model["views"]["dollars"]  # the "what is information worth" table


def test_the_takeaways_are_there(model: dict[str, Any]) -> None:
    # The old captions become the sentences the view prints under each chart.
    assert model["recommendation"]["sentence"].endswith(".")
    assert model["recommendation"]["why"].endswith(".")
    assert model["timing"]["words"].endswith(".")
    assert model["agent_summary"]


def test_ranking_maps_leaks_and_problems_name_the_policies(
    model: dict[str, Any], raced: RaceResult
) -> None:
    names = [p["policy"] for p in model["policies"]]
    assert names == [row.policy for row in raced.ranking()]
    assert set(model["markov"]) == set(raced.chains)
    assert len(model["markov"]) >= 2  # the top policies, and today's rule is always kept
    best = raced.ranking()[0]
    assert model["leaks"][best.policy][0]["state"] == best.leaks[0].state
    assert any("broken.py" in json.dumps(p) for p in model["problems"])


def test_words_are_plain(model: dict[str, Any]) -> None:
    assert not any("—" in s for s in _strings(model))  # no em dashes
    for line in model["agent_summary"]:
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            assert len(sentence.split()) <= 30, sentence  # short sentences


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.open: list[str] = []
        self.scripts = 0
        self.bad: list[str] = []
        self.noscript = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "noscript", "table"):
            self.open.append(tag)
        self.scripts += tag == "script"
        self.noscript |= tag == "noscript"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "noscript", "table"):
            if not self.open or self.open[-1] != tag:
                self.bad.append(tag)
            else:
                self.open.pop()


def test_the_page_parses_with_two_scripts_and_a_noscript(page: str, model: dict[str, Any]) -> None:
    parser = _Tags()
    parser.feed(page)
    assert parser.bad == [] and parser.open == []
    assert parser.scripts == 2  # the data and the app
    assert parser.noscript


def test_write_race_report_writes_one_file(
    raced: RaceResult, written: tuple[Path, str], model: dict[str, Any], tmp_path: Path
) -> None:
    path, page = written
    assert path.name == "race.html"
    assert page == decision.render(model)
    again = write_race_report(raced, tmp_path / "race.html", model=model)
    assert again == tmp_path / "race.html"
    assert again.read_text(encoding="utf-8") == page


def test_a_custom_score_shows_dollars_next_to_it(make_project: MakeProject) -> None:
    folder = make_project({"score": "margin_only", "race": {"episodes": 2, "tune_episodes": 1}})
    result = race(Project.open(folder), ["idle", "lowest_cover"])
    model = race_report_model(result, long_check=False)
    assert model["about"]["score"] == "margin_only"
    assert model["about"]["score_is_dollars"] is False
    assert all("mean" in p["dollars"] for p in model["policies"])
