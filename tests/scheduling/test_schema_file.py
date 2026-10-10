"""The checked-in request schema equals the schema generated from the models (REQ-070, D-060)."""

from __future__ import annotations

import json
from pathlib import Path

import twinflow.scheduling.model as model

SCHEMA_FILE = Path(model.__file__).with_name("problem.schema.json")


def test_checked_in_schema_file_equals_generated_schema() -> None:
    expected = json.dumps(model.request_json_schema(), indent=2, sort_keys=True) + "\n"
    assert SCHEMA_FILE.read_text(encoding="utf-8") == expected
