"""B-04: a JSON Schema per view model, checked against real output.

The TypeScript types in web/src/decision/types.ts are generated from the same files, so the
two sides cannot drift (vitest fails when they are stale).
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from twinflow.extensions.project import Project, RaceSettings
from twinflow.extensions.race import race
from twinflow.extensions.report_model import race_view_model

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "src" / "twinflow" / "report" / "schemas"
DESIGN = ROOT / "docs" / "design"


@cache
def _validator(view: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{view}.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _errors(view: str, model: dict[str, Any]) -> list[str]:
    return [f"{e.json_path}: {e.message}" for e in _validator(view).iter_errors(model)]


@pytest.mark.parametrize("name", ["race-view-model.example.json", "race-view-model.full.json"])
def test_the_design_examples_validate(name: str) -> None:
    assert _errors("race", json.loads((DESIGN / name).read_text())) == []


def test_a_missing_required_field_fails() -> None:
    model = json.loads((DESIGN / "race-view-model.example.json").read_text())
    del model["recommendation"]
    assert any("recommendation" in e for e in _errors("race", model))
    model = json.loads((DESIGN / "race-view-model.example.json").read_text())
    del model["confidence"]["level"]
    assert _errors("race", model)


def test_real_race_output_validates() -> None:
    project = Project.open(ROOT / "examples" / "molding-press")
    settings = RaceSettings(episodes=3, seed=1, tune_episodes=2)
    result = race(project, settings=settings)
    model = json.loads(json.dumps(race_view_model(result, project)))
    assert _errors("race", model) == []
