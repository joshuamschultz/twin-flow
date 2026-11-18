"""B-02 and B-03: the offline report template and the Python render (DESIGN.md 11).

`twinflow.report.decision.render(view_model)` fills the committed template with the view
model. The file must parse, carry the JSON intact, stay safe against hostile strings, and
hold no network URL. The template must be fresh against the web sources.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from twinflow.report import decision

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
EXAMPLE = ROOT / "docs" / "design" / "race-view-model.example.json"
NAMESPACES = (
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1998/Math/MathML",
    "http://www.w3.org/XML/1998/namespace",
    "http://www.w3.org/2000/xmlns/",
)


def _example() -> dict[str, Any]:
    model = json.loads(EXAMPLE.read_text())
    model.pop("_about", None)
    return model


class _Collect(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: dict[str, str] = {}
        self.title = ""
        self.meta: dict[str, str] = {}
        self._in: str | None = None
        self.noscript = ""
        self.in_noscript = False
        self.script_tags = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "noscript":
            self.in_noscript = True
        if tag == "script":
            self.script_tags += 1
            self._in = "script:" + (a.get("id") or a.get("type") or "")
        elif tag in ("title", "noscript"):
            self._in = tag
        elif tag == "meta" and a.get("name"):
            self.meta[str(a["name"])] = str(a.get("content"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "noscript":
            self.in_noscript = False
        self._in = None

    def handle_data(self, data: str) -> None:
        if self._in and self._in.startswith("script:"):
            key = self._in.removeprefix("script:")
            self.scripts[key] = self.scripts.get(key, "") + data
        elif self._in == "title":
            self.title += data
        if self.in_noscript:
            self.noscript += data


def _parse(text: str) -> _Collect:
    parser = _Collect()
    parser.feed(text)
    return parser


def _network_urls(text: str) -> list[str]:
    hits = re.findall(r"https?://[^\s\"'`<>)\\]*", text)
    return [
        h for h in hits if not any(h.startswith(ns) and len(h) <= len(ns) + 1 for ns in NAMESPACES)
    ]


def test_render_parses_and_the_json_round_trips() -> None:
    model = _example()
    page = _parse(decision.render(model))
    assert json.loads(page.scripts["twinflow-data"]) == model
    assert page.title == model["about"]["title"]
    assert model["recommendation"]["sentence"] in page.meta["description"]


def test_noscript_says_the_decision() -> None:
    model = _example()
    text = _parse(decision.render(model)).noscript
    assert model["recommendation"]["sentence"] in text


def test_hostile_strings_do_not_break_out() -> None:
    model = _example()
    evil = '</script><script>alert("x")</script> & "quotes"   '
    model["recommendation"]["label"] = evil
    model["about"]["title"] = evil
    model["recommendation"]["sentence"] = evil
    out = decision.render(model)
    page = _parse(out)
    assert json.loads(page.scripts["twinflow-data"])["recommendation"]["label"] == evil
    assert "alert" not in "".join(v for k, v in page.scripts.items() if k != "twinflow-data")
    assert page.script_tags == 2  # the data tag and the app, nothing injected
    assert " " not in out and " " not in out
    assert page.title == " ".join(evil.split())


def test_no_network_url_in_the_output() -> None:
    assert _network_urls(decision.render(_example())) == []


def test_write_puts_the_file_on_disk(tmp_path: Path) -> None:
    path = decision.write(tmp_path / "race.html", _example())
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_template_is_small() -> None:
    assert len(decision.template().encode()) < 500 * 1024


def _source_hash() -> str:
    spec = json.loads((WEB / "report.sources.json").read_text())
    files = set(spec["files"])
    for folder in spec["dirs"]:
        files |= {p.relative_to(WEB).as_posix() for p in (WEB / folder).rglob("*") if p.is_file()}
    outer = hashlib.sha256()
    for rel in sorted(files):
        inner = hashlib.sha256((WEB / rel).read_bytes()).hexdigest()
        outer.update(f"{rel}\n{inner}\n".encode())
    return outer.hexdigest()


@pytest.mark.skipif(not (WEB / "report.sources.json").exists(), reason="no web sources here")
def test_committed_template_is_fresh() -> None:
    """Fails when web/ changed but the template was not rebuilt (npm run build:report)."""
    meta = _parse(decision.template()).meta["twinflow-template-source"]
    assert meta == f"sha256:{_source_hash()}", "Stale template. Run: cd web && npm run build:report"
