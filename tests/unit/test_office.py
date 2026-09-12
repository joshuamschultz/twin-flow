from __future__ import annotations

from pathlib import Path

from twinflow.domain import registry
from twinflow.scenario import load


def _model() -> dict[str, object]:
    return {
        "domain": "office",
        "resources": [{"id": "analyst", "roles": ["analyst"], "capacity": 1}],
        "documents": [{"id": "drawing", "revision": "B"}],
        "tasks": [
            {"id": "intake", "duration": 2, "role": "analyst"},
            {"id": "legal", "duration": 3, "role": "analyst", "prerequisites": ["intake"]},
            {"id": "finance", "duration": 3, "role": "analyst", "prerequisites": ["intake"]},
            {
                "id": "release",
                "duration": 1,
                "role": "analyst",
                "prerequisites": ["legal", "finance"],
            },
        ],
    }


def test_parallel_fork_join_and_registry_description(tmp_path: Path) -> None:
    snapshot = {"cases": [{"id": "c1", "data": {}}]}
    assert registry.validate("office", _model(), snapshot) == []
    description = registry.describe("office", _model(), snapshot)
    assert len(description["edges"]) == 4
    result = registry.evaluate("office", _model(), snapshot, artifact_dir=str(tmp_path))
    case = result["cases"][0]
    assert case["case_id"] == "c1"
    assert case["outcome"] == "completed"
    assert case["completed_tasks"] == ["intake", "legal", "finance", "release"]
    assert case["started_at"] == 0
    assert case["completed_at"] == 9
    assert result["horizon"] == 9
    assert (tmp_path / "office-result.json").exists()


def test_stale_document_missing_approval_and_data_are_explicit() -> None:
    model = _model()
    model["tasks"] = [
        {
            "id": "release",
            "duration": 1,
            "role": "analyst",
            "required_data": ["quote"],
            "approval": {"document_id": "drawing", "revision": "B", "role": "manager"},
        }
    ]
    snapshot = {
        "cases": [{"id": "c1", "data": {}, "documents": [{"id": "drawing", "revision": "A"}]}]
    }
    issues = registry.validate("office", model, snapshot)
    messages = {issue.message for issue in issues}
    assert any("missing required data" in message for message in messages)
    assert any("stale or missing revision" in message for message in messages)
    assert any("missing approval" in message for message in messages)
    assert all(issue.severity == "warning" for issue in issues)
    result = registry.evaluate("office", model, snapshot)
    assert result["cases"][0]["outcome"] == "incomplete"
    assert any(event["reason"] == "missing_data" for event in result["trace"])


def test_constrained_resource_and_bounded_rework() -> None:
    model = _model()
    model["tasks"][0]["max_rework"] = 1
    snapshot = {
        "cases": [{"id": "a", "data": {}, "rework": {"intake": 1}}, {"id": "b", "data": {}}]
    }
    result = registry.evaluate("office", model, snapshot)
    assert result["horizon"] == 20
    assert result["metrics"]["completed_cases"] == 2


def test_office_capsule_uses_registered_domain_adapter() -> None:
    capsule = load("examples/capsules/office.twin.yaml")
    assert capsule.validate() == []
    result = capsule.evaluate()
    assert result["metrics"]["completed_cases"] == 1


def test_calendar_never_executes_outside_finite_window() -> None:
    model = _model()
    model["resources"][0]["calendar"] = [[0, 1]]
    result = registry.evaluate("office", model, {"cases": [{"id": "c1", "data": {}}]})
    assert result["status"] == "incomplete"
    assert any(event["reason"] == "calendar_exhausted" for event in result["trace"])


def test_wrong_revision_approval_blocks_exact_gate() -> None:
    model = _model()
    model["tasks"] = [
        {
            "id": "release",
            "duration": 1,
            "role": "analyst",
            "approval": {"document_id": "drawing", "revision": "B", "role": "manager"},
        }
    ]
    snapshot = {
        "cases": [
            {
                "id": "c1",
                "data": {},
                "documents": [{"id": "drawing", "revision": "B"}],
                "approvals": [
                    {
                        "task_id": "release",
                        "document_id": "drawing",
                        "revision": "A",
                        "role": "manager",
                    }
                ],
            }
        ]
    }
    result = registry.evaluate("office", model, snapshot)
    assert any(event["reason"] == "missing_approval" for event in result["trace"])


def test_rework_over_budget_is_incomplete_and_limits_bound_execution() -> None:
    model = _model()
    snapshot = {"cases": [{"id": "c1", "data": {}, "rework": {"intake": 1}}]}
    result = registry.evaluate("office", model, snapshot)
    assert any(event.get("reason") == "rework_budget" for event in result["trace"])
    bounded = registry.evaluate(
        "office", model, {"cases": [{"id": "c2", "data": {}}]}, limits={"max_sim_time": 1}
    )
    assert bounded["status"] == "incomplete"


def test_eligible_alternate_resources_are_used() -> None:
    model = _model()
    model["resources"] = [{"id": "a", "roles": ["analyst"]}, {"id": "b", "roles": ["analyst"]}]
    model["tasks"] = [{"id": "intake", "duration": 2, "role": "analyst"}]
    result = registry.evaluate(
        "office", model, {"cases": [{"id": "a", "data": {}}, {"id": "b", "data": {}}]}
    )
    assert result["horizon"] == 2
    assert all(case["started_at"] == 0 for case in result["cases"])
