"""T-248: `experiment.state_capture` on a manufacturing capsule (COMP-152, REQ-197).

`state_capture: {every_hours, decisions}` passes capsule validation, and `evaluate_capsule`
writes `moves.parquet` / `states.parquet` next to each replication's logs. Absent means
no state files and no change to the result or the logs.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from twinflow.application.scenarios import evaluate_capsule
from twinflow.scenario import ScenarioCapsule, load, schema
from twinflow.state.recorder import read_moves

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "cnc-shop" / "cnc-shop.twin.yaml"
LOGS = ("events.parquet", "inventory.parquet", "resource_usage.parquet")

ECONOMICS = {
    "things": {
        "shaft": {"price": 90, "unit_cost": 40, "late_cost_per_day": 500},
        "bracket": {"price": 60, "unit_cost": 25, "late_cost_per_day": 500},
        "round_bar": {"holding_per_unit_day": 0.5},
    },
    "setup": {"cost": 75},
}


def _capsule(state_capture: object = None, **model_extra: object) -> dict[str, Any]:
    data = copy.deepcopy(load(EXAMPLE).to_dict())
    if state_capture is not None:
        data["experiment"]["state_capture"] = state_capture
    data["model"].update(model_extra)
    return data


def _issues(data: dict[str, Any]) -> list[tuple[str, str]]:
    return [(issue.path, issue.message) for issue in ScenarioCapsule.from_dict(data).validate()]


def _rep_dirs(out: Path) -> list[Path]:
    return sorted(path.parent for path in out.glob("rep-*/*/events.parquet"))


def test_schema_lists_state_capture() -> None:
    capture = schema()["properties"]["experiment"]["properties"]["state_capture"]
    assert capture["type"] == "object"
    assert capture["additionalProperties"] is False
    assert set(capture["properties"]) == {"every_hours", "decisions"}


@pytest.mark.parametrize(
    "config",
    [{"every_hours": 4}, {"every_hours": 0.5, "decisions": True}, {"decisions": True}],
)
def test_good_state_capture_validates(config: dict[str, object]) -> None:
    assert _issues(_capsule(config)) == []
    assert _issues(_capsule(config, economics=ECONOMICS)) == []


@pytest.mark.parametrize(
    ("config", "path"),
    [
        ("hourly", "$.experiment.state_capture"),
        ({}, "$.experiment.state_capture"),
        ({"decisions": False}, "$.experiment.state_capture"),
        ({"every_hours": 0}, "$.experiment.state_capture.every_hours"),
        ({"every_hours": "4h"}, "$.experiment.state_capture.every_hours"),
        ({"every_hours": True}, "$.experiment.state_capture.every_hours"),
        ({"every_hours": 4, "decisions": "yes"}, "$.experiment.state_capture.decisions"),
        ({"every_hour": 4}, "$.experiment.state_capture.every_hour"),
    ],
)
def test_bad_state_capture_names_the_field(config: object, path: str) -> None:
    issues = _issues(_capsule(config))
    assert [p for p, _ in issues] == [path]


def test_bad_economics_is_reported_when_capturing() -> None:
    bad = {"things": {"shaft": {"price": -1}}}
    issues = _issues(_capsule({"every_hours": 1}, economics=bad))
    assert [p for p, _ in issues] == ["$.model.economics.things.shaft.price"]


def test_evaluate_writes_moves_per_replication(tmp_path: Path) -> None:
    data = _capsule({"every_hours": 0.1, "decisions": True}, economics=ECONOMICS)
    data["experiment"]["dispatch_policy"] = {"default": "edd"}
    result = evaluate_capsule(data, tmp_path, 2, 0, lambda: False)
    reps = _rep_dirs(tmp_path)
    assert len(reps) == 2
    traces = result["policy_traces"]
    for rep, trace in zip(reps, traces, strict=True):
        moves = read_moves(rep)
        decisions = [m for m in moves if m.kind == "decision"]
        periods = [m for m in moves if m.kind == "period"]
        assert len(decisions) == len(trace["decisions"])
        assert periods
        assert sum(m.ledger.revenue for m in periods) == pytest.approx(
            90 * (15 + 25 + 30 + 12 + 20) + 60 * (20 + 10 + 18 + 22 + 16)
        )
        assert (rep / "states.parquet").is_file()
    capture = result["state_capture"]
    assert capture["every_hours"] == 0.1 and capture["decisions"] is True
    assert capture["files"] == ["moves.parquet", "states.parquet"]
    assert all(issue["severity"] == "info" for issue in capture["issues"])


def test_missing_economics_still_captures_and_says_so(tmp_path: Path) -> None:
    result = evaluate_capsule(_capsule({"every_hours": 0.1}), tmp_path, 1, 0, lambda: False)
    (rep,) = _rep_dirs(tmp_path)
    assert all(move.ledger.total == 0 for move in read_moves(rep))
    assert [issue["path"] for issue in result["state_capture"]["issues"]] == ["economics"]


def test_absent_state_capture_writes_no_state_files_and_changes_nothing(tmp_path: Path) -> None:
    plain = evaluate_capsule(_capsule(), tmp_path / "plain", 1, 0, lambda: False)
    watched = evaluate_capsule(
        _capsule({"every_hours": 0.1, "decisions": True}), tmp_path / "watched", 1, 0, lambda: False
    )
    (plain_rep,) = _rep_dirs(tmp_path / "plain")
    (watched_rep,) = _rep_dirs(tmp_path / "watched")
    assert not list((tmp_path / "plain").rglob("moves.parquet"))
    assert not list((tmp_path / "plain").rglob("states.parquet"))
    assert "state_capture" not in plain
    for name in LOGS:
        assert (plain_rep / name).read_bytes() == (watched_rep / name).read_bytes()
    assert {k: v for k, v in watched.items() if k != "state_capture"} == plain
