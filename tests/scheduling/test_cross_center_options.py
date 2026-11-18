"""Cross-center alternates: a processing option on a machine of a substitute work center.

Orchestrator ruling (TF-005 builder gaps, Western Spring 600-order snapshot):

* `ProcessingOption.work_center_id: Id | None = None`. None means the operation's own
  center; `Operation.work_center_id` stays the primary center.
* `validate` checks each option machine against the OPTION's center
  (`option.work_center_id or op.work_center_id`):
  - a machine inside the option's center is valid, even when that center is not the op's;
  - a machine outside the option's center is `option_outside_work_center` (path
    `...options[k].machine_id`, unchanged);
  - an option `work_center_id` naming no work center is `unknown_reference` at
    `...options[k].work_center_id`.
* Verifier, baseline and CP-SAT treat such an option like any other machine choice; setups
  are keyed by the chosen machine's own center (its setup function), as today.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from twinflow.scheduling import (
    BaselineSolver,
    Job,
    Machine,
    Operation,
    Problem,
    ProcessingOption,
    SetupFunction,
    SolveRequest,
    WorkCenter,
    solve,
    validate,
)

GRIND, FORM = "grind", "form"
OP_PATH = "problem.jobs[0].operations[0]"


def _option(machine_id: str, seconds: float = 100.0, **fields: Any) -> ProcessingOption:
    data: dict[str, Any] = {
        "machine_id": machine_id,
        "fixed_seconds": seconds,
        "unit_seconds": 0.0,
        "preference_cost": 0.0,
    }
    return ProcessingOption.model_validate(data | fields)


def _request(*options: ProcessingOption, **problem_fields: Any) -> SolveRequest:
    """One grind op with the given options; grind-1 in grind, form-1 in form."""
    op = Operation(id="J:10", work_center_id=GRIND, quantity=1.0, options=options)
    data: dict[str, Any] = {
        "id": "p",
        "jobs": (Job(id="J", operations=(op,)),),
        "machines": (
            Machine(id="grind-1", work_center_id=GRIND),
            Machine(id="form-1", work_center_id=FORM),
        ),
        "work_centers": (WorkCenter(id=GRIND), WorkCenter(id=FORM)),
    }
    problem = Problem.model_validate(data | problem_fields)
    return SolveRequest(schema_version="2.0", problem=problem)


def _codes_at(request: SolveRequest) -> list[tuple[str, str]]:
    return [(issue.code, issue.path) for issue in validate(request)]


# --- model -------------------------------------------------------------------------------------


def test_processing_option_work_center_id_defaults_to_none() -> None:
    assert _option("grind-1").work_center_id is None


def test_processing_option_accepts_a_work_center_id() -> None:
    assert _option("form-1", work_center_id=FORM).work_center_id == FORM


def test_processing_option_rejects_an_empty_work_center_id() -> None:
    # An Id is 1 to 200 characters (not "extra field forbidden", which today's model says).
    with pytest.raises(ValidationError) as caught:
        _option("form-1", work_center_id="")
    assert [(e["loc"], e["type"]) for e in caught.value.errors()] == [
        (("work_center_id",), "string_too_short")
    ]


# --- validate ----------------------------------------------------------------------------------


def test_validate_option_on_its_own_substitute_center_is_valid() -> None:
    request = _request(_option("grind-1"), _option("form-1", work_center_id=FORM))
    assert validate(request) == []


def test_validate_option_without_center_still_must_be_in_the_op_center() -> None:
    request = _request(_option("grind-1"), _option("form-1"))
    assert _codes_at(request) == [
        ("option_outside_work_center", f"{OP_PATH}.options[1].machine_id")
    ]


def test_validate_machine_outside_the_option_center_is_outside_work_center() -> None:
    # The option names grind as its center, but form-1 is a form machine.
    request = _request(_option("grind-1"), _option("form-1", work_center_id=GRIND))
    assert _codes_at(request) == [
        ("option_outside_work_center", f"{OP_PATH}.options[1].machine_id")
    ]


def test_validate_op_center_machine_under_a_substitute_center_option_is_outside() -> None:
    # grind-1 sits in the op's center, but the option says it runs at form.
    request = _request(_option("grind-1", work_center_id=FORM))
    assert _codes_at(request) == [
        ("option_outside_work_center", f"{OP_PATH}.options[0].machine_id")
    ]


def test_validate_unknown_option_work_center_is_unknown_reference() -> None:
    request = _request(_option("grind-1"), _option("form-1", work_center_id="paint"))
    codes = _codes_at(request)
    assert ("unknown_reference", f"{OP_PATH}.options[1].work_center_id") in codes
    assert all(code in {"unknown_reference", "option_outside_work_center"} for code, _ in codes)


# --- solve -------------------------------------------------------------------------------------


def test_baseline_can_choose_the_substitute_center_machine_and_verifies() -> None:
    # The substitute is the only option that finishes first: grind-1 is busy until 1000.
    request = _request(
        _option("grind-1", 100.0),
        _option("form-1", 150.0, work_center_id=FORM),
        machines=(
            Machine.model_validate(
                {"id": "grind-1", "work_center_id": GRIND, "state": {"available_from": 1000.0}}
            ),
            Machine(id="form-1", work_center_id=FORM),
        ),
    )
    result = solve(request, solver=BaselineSolver())
    assert result.verified is True, result.issues
    (assignment,) = result.schedule.assignments
    assert (assignment.machine_id, assignment.start, assignment.end) == ("form-1", 0.0, 150.0)


def test_substitute_machine_takes_its_own_center_setup_function() -> None:
    # form has a 40 s setup function, grind has none: the verifier and baseline agree that a
    # grind op on form-1 gets form's setup.
    request = _request(
        _option("form-1", 100.0, work_center_id=FORM),
        work_centers=(
            WorkCenter(id=GRIND),
            WorkCenter(id=FORM, setup_function_id="form-setup"),
        ),
        setup_functions=(SetupFunction(id="form-setup", terms=(), default_seconds=40.0),),
    )
    result = solve(request, solver=BaselineSolver())
    assert result.verified is True, result.issues
    (assignment,) = result.schedule.assignments
    assert (assignment.start, assignment.setup_end, assignment.end) == (0.0, 40.0, 140.0)


def test_ortools_solves_a_substitute_center_option_verified() -> None:
    pytest.importorskip("ortools")
    from twinflow.scheduling.algorithms.cpsat import ORToolsSolver

    request = _request(_option("grind-1", 100.0), _option("form-1", 50.0, work_center_id=FORM))
    result = solve(request, solver=ORToolsSolver(), budget_seconds=5.0)
    assert result.verified is True, result.issues
    (assignment,) = result.schedule.assignments
    assert assignment.machine_id == "form-1"


# --- every registered solver (TF-003 merge) ----------------------------------------------------


def _shared_substitute_request() -> SolveRequest:
    """Two grind jobs may use form-1 as a substitute while two form jobs need it too.

    form-1 then carries ops of two centers. A solver that sequences work per center must
    still give one consistent order on form-1, or refuse.
    """
    jobs = []
    for k in range(2):
        grind_op = Operation(
            id=f"G{k}:10",
            work_center_id=GRIND,
            quantity=1.0,
            options=(_option("grind-1", 300.0), _option("form-1", 100.0, work_center_id=FORM)),
        )
        form_op = Operation(
            id=f"F{k}:10", work_center_id=FORM, quantity=1.0, options=(_option("form-1", 80.0),)
        )
        jobs += [
            Job(id=f"G{k}", due=200.0, operations=(grind_op,)),
            Job(id=f"F{k}", due=200.0, operations=(form_op,)),
        ]
    problem = Problem(
        id="p",
        jobs=tuple(jobs),
        machines=(
            Machine(id="grind-1", work_center_id=GRIND),
            Machine(id="form-1", work_center_id=FORM),
        ),
        work_centers=(WorkCenter(id=GRIND), WorkCenter(id=FORM)),
    )
    return SolveRequest(schema_version="2.0", problem=problem)


def _registered_names() -> list[str]:
    from twinflow.scheduling import available_solvers

    return [info.name for info in available_solvers()]


@pytest.mark.parametrize("name", _registered_names())
def test_every_registered_solver_verifies_a_shared_substitute_machine(name: str) -> None:
    from twinflow.scheduling import get_solver
    from twinflow.scheduling.seam import OptionalDependencyError

    try:
        solver = get_solver(name)
    except OptionalDependencyError:
        pytest.skip(f"{name} needs an optional extra")
    result = solve(_shared_substitute_request(), solver=solver, budget_seconds=5.0)

    codes = [issue.code for issue in result.issues]
    if result.schedule.status in ("FEASIBLE", "OPTIMAL"):
        assert result.verified is True, (name, result.issues)
    else:
        assert codes, (name, result.schedule.status)
        assert "solver_error" not in codes, (name, result.issues)


@pytest.mark.parametrize("name", ["sb_makespan", "sb_wt"])
def test_shifting_bottleneck_sequences_a_machine_shared_by_two_centers(name: str) -> None:
    # Centers whose options share a machine form one subproblem, so form-1 gets one sequence.
    from twinflow.scheduling import get_solver

    result = solve(_shared_substitute_request(), solver=get_solver(name), budget_seconds=5.0)

    assert result.verified is True, result.issues
    assert len(result.schedule.assignments) == 4
