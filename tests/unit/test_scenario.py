from __future__ import annotations

from pathlib import Path

import pytest

from twinflow.scenario import CapsuleValidationError, ScenarioCapsule, import_legacy, loads


def test_legacy_import_digest_edit_and_round_trip() -> None:
    capsule = import_legacy(
        "examples/spring/model.yaml",
        "examples/spring/plan.csv",
        as_of="2026-09-12T08:00:00-05:00",
    )
    assert capsule.validate() == []
    same = ScenarioCapsule.from_dict(capsule.to_dict())
    assert same.digest == capsule.digest
    edited = capsule.edit("/snapshot/production_plan/0/qty", 99)
    assert edited.digest != capsule.digest
    assert capsule.snapshot["production_plan"][0]["qty"] != 99


def test_missing_and_unknown_fields_are_path_addressed() -> None:
    with pytest.raises(CapsuleValidationError) as missing:
        ScenarioCapsule.from_dict({})
    assert "required field" in str(missing.value)

    data = import_legacy("examples/spring/model.yaml", "examples/spring/plan.csv").to_dict()
    data["surprise"] = True
    with pytest.raises(CapsuleValidationError) as unknown:
        ScenarioCapsule.from_dict(data)
    assert "unknown capsule field" in str(unknown.value)


def test_capability_validation_is_explicit() -> None:
    capsule = import_legacy("examples/spring/model.yaml", "examples/spring/plan.csv")
    issues = capsule.validate(available_capabilities=set())
    assert any("unsupported required capability" in issue.message for issue in issues)


def test_loads_rejects_yaml_aliases_and_compiles_existing_contracts() -> None:
    capsule = import_legacy("examples/spring/model.yaml", "examples/spring/plan.csv")
    assert len(capsule.compile().plan) == 9
    with pytest.raises(ValueError, match="anchors and aliases"):
        loads("schema_version: '0.1'\nmodel: &m {}\nsnapshot: *m\n")


def test_parser_rejects_malformed_duplicate_and_nonfinite_values() -> None:
    with pytest.raises(ValueError, match="invalid capsule YAML"):
        loads("schema_version: [")
    with pytest.raises(ValueError, match="duplicate YAML key"):
        loads("schema_version: '0.1'\nschema_version: '0.1'\n")
    data = import_legacy("examples/spring/model.yaml", "examples/spring/plan.csv").to_dict()
    data["experiment"]["seed"] = -1
    with pytest.raises(CapsuleValidationError):
        ScenarioCapsule.from_dict(data)


def test_edit_is_semantically_validated_and_branch_is_copy_isolated() -> None:
    capsule = import_legacy("examples/spring/model.yaml", "examples/spring/plan.csv")
    with pytest.raises(CapsuleValidationError):
        capsule.edit("/snapshot/production_plan/0/qty", "not-a-number")
    with pytest.raises(ValueError):
        capsule.edit("/snapshot/production_plan/-1/qty", 3)
    branch = capsule.branch(scenario_id="child")
    branch.provenance["marker"] = "child-only"
    assert "marker" not in capsule.provenance


def test_all_runnable_example_pairs_import() -> None:
    for model_path in sorted(Path("examples").glob("*/model.yaml")):
        plan_path = model_path.with_name("plan.csv")
        capsule = import_legacy(model_path, plan_path, as_of="2026-09-12T08:00:00-05:00")
        assert capsule.validate() == []
