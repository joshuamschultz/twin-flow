from __future__ import annotations

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
