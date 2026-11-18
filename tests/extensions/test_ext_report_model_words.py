"""State words in the race view model (DESIGN.md 3.8, R18).

Python is the one source of the words a state shows. The front end prints them as they come.
"""

from __future__ import annotations

import pytest

from twinflow.extensions.report_model import state_words

LEVELS = ["out", "low", "ok", "high"]


@pytest.mark.parametrize(
    ("label", "title", "setup"),
    [
        ("grey:high black:ok natural:low | black", "Grey high, natural low", "Press on black"),
        ("grey:high black:low natural:low | grey", "Grey high, black and natural low", "Press on grey"),
        ("grey:low black:low natural:ok | grey", "Grey and black low", "Press on grey"),
        ("grey:ok black:ok natural:ok | grey", "All stock OK", "Press on grey"),
        ("grey:low black:low natural:low | grey", "All stock low", "Press on grey"),
        ("grey:high black:high natural:high | grey", "All stock high", "Press on grey"),
        ("grey:ok black:ok natural:ok | idle", "All stock OK", "Press rests"),
    ],
)  # fmt: skip
def test_state_words_group_levels_and_name_the_setup(label: str, title: str, setup: str) -> None:
    assert state_words(label, LEVELS, "press") == {"title": title, "setup": setup}


def test_state_words_without_a_setup_or_levels_list() -> None:
    assert state_words("a:low b:ok", None, "line") == {"title": "A low", "setup": ""}


def test_state_words_for_a_label_with_no_parts_keep_the_label() -> None:
    assert state_words("busy now", LEVELS, "line") == {"title": "busy now", "setup": ""}


def test_state_words_with_odd_levels_skip_the_middle_one() -> None:
    levels = ["empty", "some", "full"]
    words = state_words("a:full b:some | x", levels, "line")
    assert words == {"title": "A full", "setup": "Line on x"}


def test_state_words_have_no_em_dash_and_ok_is_capital() -> None:
    words = state_words("grey:ok black:ok | grey", LEVELS, "press")
    assert "ok" not in words["title"]
    assert "—" not in words["title"] + words["setup"]
