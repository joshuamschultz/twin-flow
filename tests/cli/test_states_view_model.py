"""TF-006 B-28: the states view model (DATA-CONTRACT 3) and `--html` on states and markov."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from twinflow.cli.main import main
from twinflow.extensions.states_model import StatesError, states_view_model

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "src" / "twinflow" / "report" / "schemas" / "states.schema.json"
EM_DASH = "—"


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _valid(model: dict[str, Any]) -> list[str]:
    schema = json.loads(SCHEMA.read_text())
    return [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(model)]


def test_a_step_run_gives_lanes_levels_and_a_headline(step_run: Path) -> None:
    model = states_view_model(step_run, view="family", top=15, min_prob=0.05)
    assert model["view"] == "states" and model["schema_version"] == 1
    assert model["about"]["runs"] == 2 and model["about"]["moves"] == 120
    assert model["about"]["view_name"] == "family"
    assert {"full", "family"} <= set(model["about"]["views_available"])
    assert model["families"] and model["levels"]
    chain = model["chain"]
    assert chain["nodes"][0]["id"] == "s0" and chain["nodes"][0]["words"]["title"]
    assert re.search(r"\d+% of its days", model["headline"])
    assert set(model["money"]) >= {"revenue", "setup", "lost_sales"}
    assert model["leaks"] and model["leaks"][0]["state_id"]
    assert _valid(model) == []


def test_states_words_come_from_the_one_builder_with_the_project_levels(step_run: Path) -> None:
    from twinflow.extensions.markov_view import state_words

    model = states_view_model(step_run, view="family", top=15, min_prob=0.05)
    names = [lv["name"] for lv in model["levels"]]
    line = model["chain"]["nodes"][0]["words"]["setup"].split()[0].lower() or "line"
    for node in model["chain"]["nodes"]:
        assert node["words"] == state_words(node["label"], names, line)
        assert " ok" not in node["words"]["title"]  # "All stock OK", never "ok"
    for leak in model["leaks"]:
        assert leak["words"] == state_words(leak["state"], names, line)


def test_a_job_shop_run_has_no_families_and_says_how_to_get_a_clearer_map(des_run: Path) -> None:
    model = states_view_model(des_run, view="family", top=10, min_prob=0.05)
    assert model["families"] is None and model["levels"] is None
    assert model["about"]["view_name"] == "full"
    first = model["chain"]["nodes"][0]
    assert first["parts_of_label"] is None and first["words"]["title"] == first["label"]
    assert "state_groups" in model["about"]["note"]
    assert _valid(model) == []


def test_no_moves_gives_the_fix_in_the_model(tmp_path: Path) -> None:
    model = states_view_model(tmp_path, view="family", top=10, min_prob=0.05)
    assert model["chain"]["nodes"] == []
    assert "experiment.state_capture" in model["problems"][0]["fix"]
    assert _valid(model) == []


def test_a_missing_folder_is_an_error_with_a_fix(tmp_path: Path) -> None:
    with pytest.raises(StatesError, match="Fix:"):
        states_view_model(tmp_path / "gone", view="family", top=10, min_prob=0.05)


def test_no_em_dash_in_any_sentence(step_run: Path, des_run: Path) -> None:
    for run in (step_run, des_run):
        model = states_view_model(run, view="family", top=15, min_prob=0.05)
        assert not [s for s in _strings(model) if EM_DASH in s]


def test_states_html_writes_states_html(step_run: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["states", str(step_run), "--html"]) == 0
    page = (step_run / "states.html").read_text()
    assert '"view":"states"' in page
    assert "<noscript>" in page


def test_markov_html_writes_states_html_in_the_out_folder(
    step_run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "maps"
    assert main(["markov", str(step_run), "--html", "--out", str(out)]) == 0
    assert (out / "states.html").is_file() and (out / "markov.json").is_file()


def test_html_with_no_moves_still_writes_the_page_with_the_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["states", str(tmp_path), "--html"]) == 1
    assert "experiment.state_capture" in (tmp_path / "states.html").read_text()
