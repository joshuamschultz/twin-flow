"""The `Relaxation` model and `Schedule.relaxations` (T-155, COMP-020, REQ-113; SDD Data Model
"Schedule additions").

`Relaxation {job_id, old_due: float, new_due: float, reason: Text}` is a frozen contract model
(closed to unknown fields, finite floats) in `twinflow.scheduling.model`;
`Schedule.relaxations: tuple[Relaxation, ...] = ()`. The request schema
(`problem.schema.json`) describes `SolveRequest` only, so it does not change.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from twinflow.scheduling import model
from twinflow.scheduling.model import Schedule


def _relaxation(**fields: object) -> object:
    data: dict[str, object] = {
        "job_id": "J1",
        "old_due": 10.0,
        "new_due": 21.0,
        "reason": "least valuable affected due date",
    }
    return model.Relaxation.model_validate(data | fields)


def test_relaxation_has_the_data_model_fields() -> None:
    relaxation = _relaxation()
    assert relaxation.model_dump() == {
        "job_id": "J1",
        "old_due": 10.0,
        "new_due": 21.0,
        "reason": "least valuable affected due date",
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"extra": 1},
        {"old_due": math.inf},
        {"new_due": math.nan},
        {"reason": ""},
        {"job_id": ""},
    ],
)
def test_relaxation_rejects_unknown_fields_non_finite_dues_and_empty_text(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _relaxation(**fields)


def test_relaxation_is_frozen() -> None:
    relaxation = _relaxation()
    with pytest.raises(ValidationError):
        relaxation.new_due = 30.0  # type: ignore[attr-defined]  # proving the model refuses assignment


def test_schedule_relaxations_default_empty_and_carry_relaxations() -> None:
    bare = Schedule(status="FEASIBLE", assignments=(), solver="x", elapsed_seconds=0.0)
    assert bare.relaxations == ()
    relaxed = Schedule(
        status="FEASIBLE",
        assignments=(),
        solver="x",
        elapsed_seconds=0.0,
        relaxations=(_relaxation(),),
    )
    assert relaxed.relaxations[0].job_id == "J1"
    assert Schedule.model_validate(relaxed.model_dump()) == relaxed
