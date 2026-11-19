"""The how-to guides keep up with the features.

`test_how_to_commands.py` proves each command in a guide still runs. This file proves the
other half: a new feature cannot ship without a guide.

- Every top-level CLI command is used in a how-to guide, or is listed in `NO_GUIDE` with
  the reason.
- Every floor feature in `FEATURE_GUIDES` has its guide, and the guide names the setting.
  When you add a user-facing setting or scenario, add a row here and write the guide.
- Every setting used in an example shop file is explained somewhere under `docs/`.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest
import yaml

from twinflow.cli.main import _build_parser

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
HOW_TO = DOCS / "how-to"
EXAMPLES = ROOT / "examples"

# Top-level commands with no how-to guide yet, and why.
NO_GUIDE: dict[str, str] = {
    "admin": "service operator tool (backups, tokens); see docs/enterprise",
    "serve": "starts the web workspace; see docs/running.md and docs/frontend.md",
    "data": "ERP data import; see docs/data.md and docs/adapters.md",
    "schedule": "finite-capacity scheduler; see examples/scheduling/README.md",
}

# Floor setting or scenario -> (guide that teaches it, text the guide must contain).
FEATURE_GUIDES: dict[str, tuple[str, str]] = {
    "machines and steps": ("model-your-floor.md", "locations"),
    "people": ("add-people-and-shifts.md", "headcount"),
    "shifts": ("add-people-and-shifts.md", "calendar"),
    "holidays": ("add-people-and-shifts.md", "exceptions"),
    "orders": ("add-jobs.md", "due_date"),
    "bottleneck": ("find-your-bottleneck.md", "starved"),
    "optimize": ("optimize-your-floor.md", "twinflow optimize"),
    "compare to today": ("compare-to-today.md", "--seed"),
    "machine breakdown": ("test-a-down-machine.md", "breakdown"),
    "missing person": ("test-a-missing-person.md", "absence_rate"),
    "rush order": ("test-a-rush-order.md", "priority"),
    "order surge": ("test-an-order-surge.md", "order-influx.csv"),
    "add a worker or machine": ("test-adding-a-worker-or-machine.md", "twinflow balance"),
    "saturday shift": ("test-a-saturday-shift.md", "calendar.weekly"),
    "wip cap": ("test-a-wip-cap.md", "wip_cap"),
    "dispatch rule": ("change-dispatch-rule.md", "dispatch"),
}

# Map keys in example shop files that are data names, not settings (part numbers, SKUs).
DATA_KEY = re.compile(r"^[A-Z0-9][A-Z0-9_-]*\d")


def _guides_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(HOW_TO.glob("*.md")))


def _commands_in_guides() -> set[str]:
    return set(re.findall(r"^\s*twinflow ([a-z-]+)", _guides_text(), flags=re.M))


def _top_level_commands() -> set[str]:
    for action in _build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("twinflow parser has no subcommands")


def test_every_command_has_a_guide() -> None:
    missing = _top_level_commands() - _commands_in_guides() - set(NO_GUIDE)
    assert not missing, (
        f"commands with no how-to guide: {sorted(missing)}. "
        "Write a guide, or add the command to NO_GUIDE with the reason."
    )


def test_no_guide_list_is_current() -> None:
    stale = set(NO_GUIDE) & _commands_in_guides()
    gone = set(NO_GUIDE) - _top_level_commands()
    assert not stale, f"these commands now have a guide; remove them from NO_GUIDE: {stale}"
    assert not gone, f"these commands no longer exist; remove them from NO_GUIDE: {gone}"


@pytest.mark.parametrize("feature", sorted(FEATURE_GUIDES))
def test_feature_has_a_guide(feature: str) -> None:
    guide, must_contain = FEATURE_GUIDES[feature]
    page = HOW_TO / guide
    assert page.is_file(), f"{feature}: guide {guide} is missing"
    assert must_contain in page.read_text(encoding="utf-8"), (
        f"{feature}: {guide} does not mention `{must_contain}`"
    )


def test_feature_guides_are_in_the_index() -> None:
    index = (HOW_TO / "README.md").read_text(encoding="utf-8")
    unlisted = sorted({g for g, _ in FEATURE_GUIDES.values() if f"({g})" not in index})
    assert not unlisted, f"guides missing from docs/how-to/README.md: {unlisted}"


def _keys(node: object, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(str(key))
            _keys(value, found)
    elif isinstance(node, list):
        for value in node:
            _keys(value, found)


def _example_shop_keys() -> set[str]:
    found: set[str] = set()
    for path in EXAMPLES.rglob("*.yaml"):
        if "runs" in path.parts or path.name.endswith(".twin.yaml"):
            continue
        _keys(yaml.safe_load(path.read_text(encoding="utf-8")), found)
    return {key for key in found if not DATA_KEY.match(key)}


def test_every_shop_setting_is_documented() -> None:
    docs = "\n".join(p.read_text(encoding="utf-8") for p in DOCS.rglob("*.md"))
    missing = sorted(
        key for key in _example_shop_keys() if not re.search(rf"\b{re.escape(key)}\b", docs)
    )
    assert not missing, f"shop settings used in examples but explained nowhere in docs/: {missing}"
