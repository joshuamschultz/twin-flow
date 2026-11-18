"""Copy rules for the decision views (DESIGN.md 8.5 words, reading level, no em dash; TF-006 B-40).

Three checks run over every user-facing string:

- no em dash (or en dash),
- no banned word from the 8.5 glossary (`policy` says `rule`, `setup` says `changeover`, ...),
- each sentence builder's output reads at Flesch-Kincaid grade 6 or lower.

The strings come from the view-model golden outputs and from the string literals and JSX text
of the decision front end. Code names (file paths, keys, `backticked` names, identifiers) are
not copy, so they are skipped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "design"
FIXTURES = ROOT / "web" / "tests" / "browser" / "views" / "fixtures"
GOLDEN_JSON = sorted(FIXTURES.glob("*.json")) + [
    ROOT / "tests" / "extensions" / "race_golden.json",
    DESIGN / "race-view-model.example.json",
    DESIGN / "race-view-model.full.json",
    DESIGN / "run-view-model.example.json",
    DESIGN / "sweep-view-model.example.json",
]
DECISION_SOURCE = sorted((ROOT / "web" / "src" / "decision").rglob("*.ts*"))

# DESIGN.md 8.5, the "Not" column, plus the banned forms the glossary implies.
BANNED = re.compile(
    r"\b(policy|policies|episodes?|replications?|baselines?|winners?|"
    r"recommended policy|confidence intervals?|setup|setups|cross-family|"
    r"holding cost|fill rate|markov state|transitions?|bottlenecks?|"
    r"utilization|utilisation)\b",
    re.IGNORECASE,
)
DASHES = ("—", "–")  # em dash, en dash

# Keys whose values are code names, not copy.
CODE_KEYS = {
    "cause",
    "copied_to",
    "file",
    "kind",
    "part",
    "parts",
    "path",
    "policy",
    "source",
    "view",
    "where",
}
# Keys whose values are sentences a sentence builder writes.
SENTENCE_KEYS = {
    "about",
    "agent_summary",
    "detail",
    "fix",
    "headline",
    "message",
    "note",
    "reason",
    "rules_note",
    "sentence",
    "summary",
    "text",
    "why",
}
MAX_GRADE = 6.0


def _golden_strings(data: Any, key: str = "") -> Iterator[tuple[str, str]]:
    """Yield (nearest dict key, text) for every string in a JSON document."""
    if isinstance(data, dict):
        for name, value in data.items():
            yield from _golden_strings(value, name)
    elif isinstance(data, list):
        for value in data:
            yield from _golden_strings(value, key)
    elif isinstance(data, str):
        yield key, data


_CODE_TOKEN = re.compile(r"\S*[/\\]\S*|\S*\.\w{1,5}$|\w+(?:\.\w+)+|\S*_\S*")


def _copy_only(text: str) -> str:
    """Drop the code in a string: backticked or quoted names, paths, dotted and underscored names.

    A string that is a shell command (`twinflow ...`) is code too.
    """
    if text.startswith("twinflow "):
        return ""
    text = re.sub(r"`[^`]*`|\"[^\"]*\"", " ", text)
    return " ".join(tok for tok in text.split() if not _CODE_TOKEN.fullmatch(tok))


def golden_copy() -> list[tuple[str, str]]:
    """(id, text) for each user-facing string in the golden view-model outputs."""
    out: list[tuple[str, str]] = []
    for path in GOLDEN_JSON:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, text in _golden_strings(data):
            if key in CODE_KEYS or key in {"generated_at", "twinflow_version"}:
                continue
            out.append((f"{path.name}:{key}", text))
    return out


def source_copy() -> list[tuple[str, str]]:
    """(id, text) for quoted literals and JSX text in the decision front end."""
    out: list[tuple[str, str]] = []
    for path in DECISION_SOURCE:
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*", "import ", "export type")):
                continue
            where = f"{path.relative_to(ROOT)}:{number}"
            for literal in re.findall(r'"([^"\n]*)"|\'([^\'\n]*)\'', line):
                text = literal[0] or literal[1]
                if " " in text or text[:1].isupper():  # copy has a space or a capital
                    out.append((where, text))
            for jsx in re.findall(r">([^<>{}\n]*[A-Za-z][^<>{}\n]*)<", line):
                out.append((where, jsx))
    return out


def all_copy() -> list[tuple[str, str]]:
    return golden_copy() + source_copy()


_SYLLABLE_GROUP = re.compile(r"[aeiouy]{1,2}")


def syllables(word: str) -> int:
    """Syllables by vowel groups, with silent endings removed. Good enough for a grade check."""
    w = word.lower()
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(_SYLLABLE_GROUP.findall(w)))


def flesch_kincaid_grade(text: str) -> float:
    """Flesch-Kincaid grade level. Only plain words count; numbers and code are skipped."""
    if " " not in text.strip():  # one word is a code value or a label, not a sentence
        return 0.0
    sentences = [
        s for s in re.split(r"[.!?:;]+(?:\s|$)", _copy_only(text)) if re.search(r"[A-Za-z]", s)
    ]
    words = re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)*", " ".join(sentences))
    if not words or not sentences:
        return 0.0
    syllable_count = sum(syllables(w) for w in words)
    return 0.39 * len(words) / len(sentences) + 11.8 * syllable_count / len(words) - 15.59


def sentence_builder_outputs() -> list[tuple[str, str]]:
    """(id, text) for each sentence a builder wrote, from the golden outputs."""
    out: list[tuple[str, str]] = []
    for path in GOLDEN_JSON:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, text in _golden_strings(data):
            if key in SENTENCE_KEYS:
                out.append((f"{path.name}:{key}", text))
    return out


def test_golden_outputs_are_found() -> None:
    assert len(golden_copy()) > 100
    assert len(sentence_builder_outputs()) > 50
    assert len(source_copy()) > 100


@pytest.mark.parametrize(("where", "text"), all_copy(), ids=lambda v: v[:60])
def test_copy_has_no_em_dash(where: str, text: str) -> None:
    assert not any(dash in text for dash in DASHES), f"{where}: {text}"


@pytest.mark.parametrize(("where", "text"), all_copy(), ids=lambda v: v[:60])
def test_copy_has_no_banned_word(where: str, text: str) -> None:
    hit = BANNED.search(_copy_only(text))
    assert hit is None, f"{where} says {hit and hit.group(0)!r}: {text}"


@pytest.mark.parametrize(("where", "text"), sentence_builder_outputs(), ids=lambda v: v[:60])
def test_sentence_builder_reads_at_grade_six(where: str, text: str) -> None:
    grade = flesch_kincaid_grade(text)
    assert grade <= MAX_GRADE, f"{where} reads at grade {grade:.1f}: {text}"


@pytest.mark.parametrize(
    ("text", "grade_ok"),
    [
        ("Keep running Family campaign. It makes the most money.", True),
        (
            "Consequently, the aforementioned organizational intervention demonstrates "
            "considerable operational improvements.",
            False,
        ),
    ],
)
def test_flesch_kincaid_separates_plain_from_dense(text: str, grade_ok: bool) -> None:
    assert (flesch_kincaid_grade(text) <= MAX_GRADE) is grade_ok


def test_banned_word_check_ignores_code_names() -> None:
    assert BANNED.search(_copy_only("Put a file in `policies/` or policies/my_rule.py.")) is None
    assert BANNED.search(_copy_only("Set experiment.race.baseline in your project file.")) is None
    assert BANNED.search(_copy_only('The costliest leak is "setup".')) is None
    assert BANNED.search(_copy_only("Pick the policy that wins.")) is not None
