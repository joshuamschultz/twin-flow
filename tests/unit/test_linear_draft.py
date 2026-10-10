"""The typed compiler never fills unknown or unsupported manufacturing facts."""

from copy import deepcopy

from twinflow.application.linear_draft import LinearDraft, compile_draft


def _fact(value, unit=None):
    return {"status": "supplied", "value": value, "unit": unit, "source": "operator"}


def _draft():
    return {
        "product": _fact("finished"),
        "input_part": _fact("blank"),
        "as_of": _fact("2026-10-08T00:00:00Z"),
        "operators": _fact(2, "person"),
        "simple_flow_confirmed": _fact(True),
        "stages": [
            {
                "id": "cut",
                "seconds_per_piece": _fact(4.0, "second/piece"),
                "machine_capacity": _fact(1, "machine"),
            },
            {
                "id": "pack",
                "seconds_per_piece": _fact(2.0, "second/piece"),
                "machine_capacity": _fact(1, "machine"),
            },
        ],
        "route_edges": [{"source": "cut", "target": "pack"}],
        "orders": [
            {
                "id": "o1",
                "quantity": _fact(2, "piece"),
                "release_second": _fact(0.0, "second"),
                "due_second": _fact(100.0, "second"),
            }
        ],
    }


def test_complete_draft_compiles_deterministically() -> None:
    draft = LinearDraft.model_validate(_draft())
    first = compile_draft(draft, "draft-1", 1)
    second = compile_draft(draft, "draft-1", 1)
    assert first["status"] == "compiled", first["issues"]
    assert first["digest"] == second["digest"]
    assert first["capsule_preview"]["model"] == second["capsule_preview"]["model"]
    assert (
        first["capsule_preview"]["provenance"]["facts"]["stages"][0]["seconds_per_piece"]["source"]
        == "operator"
    )


def test_unknown_bad_unit_and_branch_report_field_issues() -> None:
    raw = _draft()
    raw["stages"][0]["seconds_per_piece"] = {
        "status": "unknown",
        "value": None,
        "unit": "second/piece",
    }
    raw["stages"][1]["machine_capacity"]["unit"] = "person"
    raw["route_edges"] = [{"source": "cut", "target": "pack"}, {"source": "cut", "target": "other"}]
    result = compile_draft(LinearDraft.model_validate(raw), "draft-2", 1)
    assert result["status"] == "unsupported"
    assert "capsule_preview" not in result
    paths = {issue["path"] for issue in result["issues"]}
    assert {"stages.0.seconds_per_piece", "stages.1.machine_capacity.unit", "route_edges"} <= paths
    assert result["normalized_draft"]["stages"][0]["seconds_per_piece"]["status"] == "unknown"


def test_unsupported_feature_blocks_importable_capsule() -> None:
    raw = deepcopy(_draft())
    raw["unsupported_features"] = [{"kind": "shift", "description": "two shifts"}]
    result = compile_draft(LinearDraft.model_validate(raw), "draft-3", 1)
    assert result["status"] == "unsupported"
    assert "capsule_preview" not in result
