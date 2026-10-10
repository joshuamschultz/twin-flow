"""The checked-in ERP snapshot schema equals the schema generated from the models (T-195).

Mirrors `tests/scheduling/test_schema_file.py`. The file ships in the wheel, so it must be
listed in `pyproject.toml` package-data (T-192). Requirements: REQ-150.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_FILE = ROOT / "src" / "twinflow" / "erp" / "erp.schema.json"


def test_checked_in_erp_schema_file_equals_generated_schema() -> None:
    from twinflow.erp.schema import snapshot_json_schema

    expected = json.dumps(snapshot_json_schema(), indent=2, sort_keys=True) + "\n"
    assert SCHEMA_FILE.read_text(encoding="utf-8") == expected


def test_erp_schema_file_is_package_data() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["twinflow"]
    assert "erp/erp.schema.json" in package_data
