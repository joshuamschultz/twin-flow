"""Portfolio result contracts (T-160; COMP-024, REQ-115, REQ-118, REQ-121; SDD Data Model).

`twinflow.scheduling.model` gains two frozen, closed Pydantic types (D-060 config):

- `Candidate{id, pipeline: tuple[Id, ...], schedule: Schedule, verified: bool,
  objective_values: dict[ObjectiveKind, float], issues: tuple[ScheduleIssue, ...]}`; `id` is
  "{index:02d}:{pipeline}" or "pareto:{n}" (the model does not parse it).
- `PortfolioResult{candidates, menu: tuple[MenuEntry, ...], recommended: Id | None,
  effective_objective: ObjectiveSpec, load_ratio: float | None, atcs_runs: tuple[AtcsRun,
  ...], issues, elapsed_seconds: NonNegFloat}`.

Both forbid unknown fields, refuse mutation and round-trip through JSON, the
`effective_objective` union keeping its mode.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from twinflow.scheduling import model
from twinflow.scheduling.model import (
    Assignment,
    AtcsRun,
    Lexicographic,
    LexicographicLevel,
    MenuEntry,
    ObjectiveKind,
    Schedule,
    ScheduleIssue,
    WeightedSum,
    WeightedTerm,
)

WT = ObjectiveKind.WEIGHTED_TARDINESS
SETUP = ObjectiveKind.TOTAL_SETUP


def _schedule() -> Schedule:
    return Schedule(
        status="FEASIBLE",
        assignments=(
            Assignment(operation_id="J1-1", machine_id="M1", start=0, setup_end=1, end=5),
        ),
        primary_value=5.0,
        solver="atcs",
        elapsed_seconds=0.01,
        atcs_runs=(
            AtcsRun(
                work_center_id="WC",
                tau=0.5,
                R=0.5,
                eta=0.2,
                m=1,
                K1=5.0,
                K2=0.5,
                source="formula",
                objective_value=5.0,
            ),
        ),
    )


def _candidate_fields(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "id": "01:atcs",
        "pipeline": ("atcs",),
        "schedule": _schedule(),
        "verified": True,
        "objective_values": {WT: 3.0, SETUP: 1.0},
        "issues": (ScheduleIssue(code="note", path="solver", message="informational"),),
    }
    return fields | changes


def _result_fields(**changes: Any) -> dict[str, Any]:
    candidate = model.Candidate(**_candidate_fields())
    fields: dict[str, Any] = {
        "candidates": (candidate,),
        "menu": (MenuEntry(candidate_id="01:atcs", supported=True, values={WT: 3.0, SETUP: 1.0}),),
        "recommended": "01:atcs",
        "effective_objective": WeightedSum(
            terms=(WeightedTerm(kind=WT, weight=0.7), WeightedTerm(kind=SETUP, weight=0.3))
        ),
        "load_ratio": 0.8,
        "atcs_runs": candidate.schedule.atcs_runs,
        "issues": (),
        "elapsed_seconds": 1.5,
    }
    return fields | changes


def test_candidate_round_trips_through_json() -> None:
    candidate = model.Candidate(**_candidate_fields())
    again = model.Candidate.model_validate_json(candidate.model_dump_json())
    assert again == candidate
    assert again.pipeline == ("atcs",)
    assert again.objective_values == {WT: 3.0, SETUP: 1.0}


def test_candidate_is_frozen() -> None:
    candidate = model.Candidate(**_candidate_fields())
    with pytest.raises(ValidationError):
        candidate.verified = False  # type: ignore[misc]  # the assignment must be refused


def test_candidate_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        model.Candidate(**_candidate_fields(status="FEASIBLE"))


def test_candidate_pipeline_of_two_stages_and_pareto_id_are_accepted() -> None:
    staged = model.Candidate(**_candidate_fields(id="01:atcs->tabu", pipeline=("atcs", "tabu")))
    pareto = model.Candidate(**_candidate_fields(id="pareto:0", pipeline=()))
    assert staged.pipeline == ("atcs", "tabu")
    assert pareto.id == "pareto:0"


def test_portfolio_result_round_trips_through_json_with_weighted_sum() -> None:
    result = model.PortfolioResult(**_result_fields())
    again = model.PortfolioResult.model_validate_json(result.model_dump_json())
    assert again == result
    assert isinstance(again.effective_objective, WeightedSum)


def test_portfolio_result_round_trips_lexicographic_and_empty_recommendation() -> None:
    spec = Lexicographic(levels=(LexicographicLevel(kind=ObjectiveKind.MAKESPAN),))
    issue = ScheduleIssue(code="no_verified_candidate", path="candidates", message="none")
    result = model.PortfolioResult(
        **_result_fields(
            effective_objective=spec,
            recommended=None,
            menu=(),
            load_ratio=None,
            issues=(issue,),
        )
    )
    again = model.PortfolioResult.model_validate_json(result.model_dump_json())
    assert again == result
    assert isinstance(again.effective_objective, Lexicographic)
    assert again.recommended is None and again.load_ratio is None


def test_portfolio_result_is_frozen() -> None:
    result = model.PortfolioResult(**_result_fields())
    with pytest.raises(ValidationError):
        result.recommended = None  # type: ignore[misc]  # the assignment must be refused


def test_portfolio_result_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        model.PortfolioResult(**_result_fields(best="01:atcs"))


def test_portfolio_result_rejects_negative_elapsed() -> None:
    with pytest.raises(ValidationError):
        model.PortfolioResult(**_result_fields(elapsed_seconds=-0.1))


def test_portfolio_result_collections_are_tuples() -> None:
    result = model.PortfolioResult(**_result_fields())
    assert isinstance(result.candidates, tuple)
    assert isinstance(result.menu, tuple)
    assert isinstance(result.atcs_runs, tuple)
    assert isinstance(result.issues, tuple)


# --- bounded pipelines (OWASP LLM10: unbounded consumption) ----------------------------------


def test_profile_default_portfolio_accepts_64_pipelines_and_refuses_65() -> None:
    assert len(model.SchedulingProfile(default_portfolio=("edd",) * 64).default_portfolio) == 64
    with pytest.raises(ValidationError):
        model.SchedulingProfile(default_portfolio=("edd",) * 65)


def test_profile_pipeline_cap_matches_the_runner_cap() -> None:
    from twinflow.scheduling.portfolio import runner

    assert runner.MAX_PIPELINES == 64
    assert runner.MAX_STAGES == 8
