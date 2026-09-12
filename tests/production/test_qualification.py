"""Acceptance tests for bounded setup-sample qualification (TF-WS-008)."""

from __future__ import annotations

from importlib import import_module

import pytest


def _api():
    try:
        module = import_module("twinflow.production.qualification")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(f"MISSING FEATURE twinflow.production.qualification: {error}", pytrace=False)
    required = (
        "QualificationPlan",
        "QualificationState",
        "UnknownTrialError",
        "QualificationStateError",
        "QualificationApprovalError",
        "QualificationLimitError",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(
            f"MISSING FEATURE twinflow.production.qualification public API: {', '.join(missing)}",
            pytrace=False,
        )
    return module


def test_main_production_waits_for_downstream_sample_and_failed_trial_adjusts() -> None:
    api = _api()
    plan = api.QualificationPlan(
        part_id="WS-01",
        machine_id="529",
        recipe_revision="rev-3",
        sample_qty=3,
        test_route=("stress-relief", "inspection"),
        acceptance_spec={"diameter_mm": (1.0, 1.1)},
        max_iterations=2,
    )
    state = api.QualificationState(plan)

    trial = state.start_trial()
    assert state.ready_for_production is False
    assert trial.sample_lot_id
    assert trial.test_route == ("stress-relief", "inspection")
    adjustment = state.record_result(
        trial.sample_lot_id, passed=False, measurements={"diameter_mm": 1.2}
    )
    assert adjustment.kind == "adjustment"
    assert state.ready_for_production is False
    assert state.iteration == 1

    trial_2 = state.start_trial()
    state.record_result(trial_2.sample_lot_id, passed=True, measurements={"diameter_mm": 1.05})
    assert state.ready_for_production is True
    assert state.approved_revision == "rev-3"


def test_iteration_limit_is_bounded_and_green_shortcut_is_explicit() -> None:
    api = _api()
    plan = api.QualificationPlan(
        part_id="WS-02",
        machine_id="441",
        recipe_revision="rev-7",
        sample_qty=2,
        test_route=("inspection",),
        acceptance_spec={"length_mm": (9.9, 10.1)},
        max_iterations=1,
        green_shortcut={"evidence_id": "setup-sheet-42", "approved": True},
    )
    state = api.QualificationState(plan)
    state.apply_green_shortcut()
    assert state.ready_for_production is True
    assert state.shortcut_evidence_id == "setup-sheet-42"
    assert state.sample_events == ()

    unapproved_plan = api.QualificationPlan(
        part_id="WS-02",
        machine_id="441",
        recipe_revision="rev-7",
        sample_qty=2,
        test_route=("inspection",),
        acceptance_spec={"length_mm": (9.9, 10.1)},
        max_iterations=1,
        green_shortcut={"evidence_id": "setup-sheet-43", "approved": False},
    )
    with pytest.raises(api.QualificationApprovalError):
        api.QualificationState(unapproved_plan).apply_green_shortcut()

    bounded_plan = api.QualificationPlan(
        part_id="WS-02",
        machine_id="441",
        recipe_revision="rev-7",
        sample_qty=2,
        test_route=("inspection",),
        acceptance_spec={"length_mm": (9.9, 10.1)},
        max_iterations=1,
    )
    bounded = api.QualificationState(bounded_plan)
    first = bounded.start_trial()
    bounded.record_result(first.sample_lot_id, passed=False, measurements={"length_mm": 9.0})
    assert bounded.status == "unresolved"
    assert bounded.ready_for_production is False
    with pytest.raises(api.QualificationLimitError):
        bounded.start_trial()


def test_pass_flag_without_in_spec_measurement_does_not_approve_and_trials_are_ordered() -> None:
    api = _api()
    plan = api.QualificationPlan(
        part_id="WS-03",
        machine_id="343",
        recipe_revision="rev-1",
        sample_qty=1,
        test_route=("inspection",),
        acceptance_spec={"length_mm": (9.9, 10.1)},
        max_iterations=3,
    )
    state = api.QualificationState(plan)
    with pytest.raises(api.UnknownTrialError):
        state.record_result("missing-trial", passed=True, measurements={"length_mm": 10.0})
    trial = state.start_trial()
    with pytest.raises(api.QualificationStateError):
        state.start_trial()
    state.record_result(trial.sample_lot_id, passed=True, measurements={"length_mm": 12.0})
    assert state.ready_for_production is False
    trial_2 = state.start_trial()
    state.record_result(trial_2.sample_lot_id, passed=True, measurements={})
    assert state.ready_for_production is False


def test_qualification_plan_snapshots_acceptance_mapping() -> None:
    api = _api()
    acceptance = {"diameter_mm": (1.0, 1.1)}
    plan = api.QualificationPlan(
        part_id="WS-04",
        machine_id="529",
        recipe_revision="rev-2",
        sample_qty=1,
        test_route=("inspection",),
        acceptance_spec=acceptance,
        max_iterations=1,
    )
    state = api.QualificationState(plan)
    acceptance["diameter_mm"] = (100.0, 101.0)
    trial = state.start_trial()
    state.record_result(trial.sample_lot_id, passed=True, measurements={"diameter_mm": 1.05})
    assert state.ready_for_production is True
