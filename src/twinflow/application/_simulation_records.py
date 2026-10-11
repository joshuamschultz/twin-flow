"""Simulation record bodies, summaries, comparisons and evidence pages (COMP-139).

Pure functions between `twinflow.execution` results and the JSON stored in the workspace
(TF-004 SDD Data Model "Records", "Outputs", "Evidence rows"):

* `simulations/sim-{uuid32}`: one per simulated plan and per dispatch baseline, with the
  `CandidateSummary`, per-replication metric and objective values and the operation and
  job evidence rows. The plan record of a `simulate_plan` call also keeps the call's
  `SimulateResult` under `result`, so a keyed replay returns it unchanged (D-105).
* `evaluations/ev-{uuid32}`: one per `simulate_portfolio` call, with the
  `PortfolioSimulation` under `summary`.

Every body is `validity = "provisional"` with an `evidence_kind`. The engine's result types
are named for type checking only: this module never imports the execution engine, so the
planning service still imports when it is absent.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from twinflow.application._planning_models import (
    CandidateSummary,
    PairedDelta,
    PlanningError,
    SimulationComparison,
    SimulationEvidencePage,
)
from twinflow.application._planning_records import RecordRow, now
from twinflow.scheduling import ObjectiveKind, ObjectiveSpec, Schedule, rank

if TYPE_CHECKING:
    from twinflow.execution.evaluate import CandidateEvaluation, EvaluationResult
    from twinflow.execution.evaluate import PairedDelta as EnginePairedDelta

PairedDeltaFn = Callable[[Mapping[int, float], Mapping[int, float], float], "EnginePairedDelta"]
ENTITY_KEYS = ("plan_id", "job_id", "operation_id", "machine_id")


@dataclass(frozen=True)
class SimulationContext:
    """What every simulation record of one call shares: the problem, inputs and policy."""

    source_kind: str
    problem_id: str
    problem_digest: str
    seed: int
    policy_digest: str
    confidence_level: float
    source_name: str
    source_digest: str
    budget_seconds: float


def new_simulation_id() -> str:
    """A fresh `simulations` record id."""
    return f"sim-{uuid.uuid4().hex}"


def new_evaluation_id() -> str:
    """A fresh `evaluations` record id."""
    return f"ev-{uuid.uuid4().hex}"


def plan_digest(schedule: Schedule) -> str:
    """SHA-256 of the canonical JSON of the plan's assignments."""
    rows = [a.model_dump(mode="json") for a in schedule.assignments]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


# --- summaries and records ----------------------------------------------------------------------


def candidate_summary(
    evaluation: CandidateEvaluation,
    result: EvaluationResult,
    *,
    simulation_id: str,
    seed: int,
) -> CandidateSummary:
    """The `CandidateSummary` of one evaluated plan, or of the baseline (`plan_id` None)."""
    paired = result.paired.get(evaluation.plan_id) if evaluation.plan_id is not None else None
    return CandidateSummary.model_validate(
        {
            "plan_id": evaluation.plan_id,
            "simulation_id": simulation_id,
            "kind": "baseline" if evaluation.plan_id is None else "plan",
            "replications": result.replications,
            "deterministic": result.deterministic,
            "status": result.status,
            "seed": seed,
            "planned": {str(kind): float(v) for kind, v in evaluation.planned.items()},
            "metrics": dict(evaluation.metrics),
            "bottleneck": evaluation.bottleneck,
            "robustness": evaluation.robustness,
            "baseline_delta": dict(paired) if paired else None,
        },
        from_attributes=True,
    )


def simulation_row(
    evaluation: CandidateEvaluation,
    summary: CandidateSummary,
    result: EvaluationResult,
    context: SimulationContext,
    *,
    schedule: Schedule | None,
    baseline_rule: str | None,
    extra: Mapping[str, Any],
) -> RecordRow:
    """The `simulations` record of one evaluation; `extra` adds call-specific links."""
    body = {
        "id": summary.simulation_id,
        "plan_id": evaluation.plan_id,
        "kind": summary.kind,
        "baseline_rule": baseline_rule,
        "source_kind": context.source_kind,
        "problem_id": context.problem_id,
        "problem_digest": context.problem_digest,
        "plan_digest": plan_digest(schedule) if schedule is not None else None,
        "seed": context.seed,
        "replications": result.replications,
        "deterministic": result.deterministic,
        "status": result.status,
        "policy_digest": context.policy_digest,
        "confidence_level": context.confidence_level,
        "source": {"name": context.source_name, "digest": context.source_digest},
        "budget_seconds": context.budget_seconds,
        "elapsed_seconds": result.elapsed_seconds,
        "summary": summary.model_dump(mode="json"),
        "per_rep": _per_rep(evaluation),
        "operations": list(evaluation.operation_rows),
        "jobs": list(evaluation.job_rows),
        **extra,
        "created_at": now(),
        "validity": "provisional",
        "evidence_kind": "simulation",
    }
    return "simulations", summary.simulation_id, body


def _per_rep(evaluation: CandidateEvaluation) -> list[dict[str, Any]]:
    """Each replication's metrics, objective values and how its run ended (D-109)."""
    return [
        {
            "rep": rep,
            "termination": run.termination,
            "metrics": values.metric_values(),
            "objective_values": {str(k): float(v) for k, v in values.objective_values.items()},
        }
        for rep, (values, run) in enumerate(zip(evaluation.per_rep, evaluation.runs, strict=True))
    ]


def evaluation_row(
    evaluation_id: str,
    problem_id: str,
    summary_json: dict[str, Any],
    simulation_ids: Mapping[str, str],
    baseline_simulation_id: str | None,
) -> RecordRow:
    """The `evaluations` record of one `simulate_portfolio` call."""
    body = {
        "id": evaluation_id,
        "portfolio_id": summary_json["portfolio_id"],
        "problem_id": problem_id,
        "simulation_ids": dict(simulation_ids),
        "baseline_simulation_id": baseline_simulation_id,
        "summary": summary_json,
        "created_at": now(),
        "validity": "provisional",
        "evidence_kind": "portfolio_simulation",
    }
    return "evaluations", evaluation_id, body


# --- comparison of stored simulations ------------------------------------------------------------


def simulation_comparison(
    plan_ids: Sequence[str],
    records: Sequence[Mapping[str, Any]],
    objective: ObjectiveSpec,
    paired_delta: PairedDeltaFn,
) -> SimulationComparison:
    """Paired deltas of every plan against the first and the simulated ranking (D-103)."""
    _check_comparable(plan_ids, records)
    reference = records[0]
    confidence = float(reference["confidence_level"])
    deltas = {
        str(record["plan_id"]): {
            metric: PairedDelta.model_validate(
                paired_delta(_metric(record, metric), _metric(reference, metric), confidence)
            )
            for metric in reference["per_rep"][0]["metrics"]
        }
        for record in records[1:]
    }
    common = set.intersection(*({int(rep["rep"]) for rep in r["per_rep"]} for r in records))
    means = {str(record["plan_id"]): _mean_objectives(record, common) for record in records}
    return SimulationComparison(
        seed=int(reference["seed"]),
        replications=len(common),
        reference_plan_id=str(reference["plan_id"]),
        deltas=deltas,
        ranking=tuple(rank(means, objective)),
    )


def _check_comparable(plan_ids: Sequence[str], records: Sequence[Mapping[str, Any]]) -> None:
    """One plan simulation per plan id, all under the same seed, policy and source."""
    if len(records) != len(plan_ids):
        raise ValueError("simulation_ids needs one simulation per plan id")
    first = records[0]
    for plan_id, record in zip(plan_ids, records, strict=True):
        same_plan = record["kind"] == "plan" and record["plan_id"] == plan_id
        same_run = all(record[key] == first[key] for key in ("seed", "policy_digest", "source"))
        if not (same_plan and same_run):
            raise PlanningError(
                f"simulation {record['id']} is not comparable for plan {plan_id}",
                code="incomparable_simulations",
            )


def _metric(record: Mapping[str, Any], metric: str) -> dict[int, float]:
    return {int(rep["rep"]): float(rep["metrics"][metric]) for rep in record["per_rep"]}


def _mean_objectives(record: Mapping[str, Any], common: set[int]) -> dict[ObjectiveKind, float]:
    """Mean objective values over the replications every compared simulation has, the same
    replications the paired deltas use, so a truncated simulation cannot flip the ranking."""
    reps = [rep for rep in record["per_rep"] if int(rep["rep"]) in common]
    return {
        ObjectiveKind(kind): statistics.fmean(float(rep["objective_values"][kind]) for rep in reps)
        for kind in reps[0]["objective_values"]
    }


# --- evidence ------------------------------------------------------------------------------------


def evidence_page(
    record: Mapping[str, Any], *, entity_id: str | None, offset: int, limit: int
) -> SimulationEvidencePage:
    """Operation rows then job rows, optionally only those naming `entity_id` (REQ-141)."""
    rows = [*record["operations"], *record["jobs"]]
    if entity_id is not None:
        rows = [row for row in rows if entity_id in {row.get(key) for key in ENTITY_KEYS}]
    window = rows[offset : offset + limit]
    end = offset + len(window)
    return SimulationEvidencePage(
        simulation_id=str(record["id"]),
        total=len(rows),
        offset=offset,
        limit=limit,
        next_offset=end if end < len(rows) else None,
        rows=tuple(window),
    )
