"""Planning record shapes (COMP-117, TF-005 SDD Data Model "Persisted records").

Pure functions between typed TF-002/TF-003 values and the JSON bodies stored in the
workspace repository: `problems`, `portfolios` and `plans`. Every body carries
`validity = "provisional"` and an `evidence_kind` (REQ-177). Only typed TF-002/TF-003 issues
are stored; no solver or exception text is.

A `PortfolioResult` is stored as one `portfolios` record without its candidates; each
candidate is one `plans` record with id `f"{portfolio_id}:{candidate_id}"`, so the schedule
is stored once. Plan assignments carry their `job_id` so pages filter without the problem.
Every plan also carries `overtime_seconds`: per machine whose calendar has priced overtime,
the seconds its assignments spend inside that overtime (D-125, REQ-183).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from twinflow.application._planning_models import (
    BuildRecord,
    MenuRecord,
    MenuRow,
    PlanAssignment,
    PlanPage,
    PlanStatus,
    PortfolioRecord,
    RushRecord,
)
from twinflow.application.planning_artifacts import Provenance
from twinflow.application.portfolio_port import CandidateLike, PortfolioResultLike
from twinflow.erp.build import BuildResult
from twinflow.erp.profile import PlanningProfile
from twinflow.erp.schema import ErpSnapshot, Lineage
from twinflow.scheduling import (
    Assignment,
    Job,
    ObjectiveKind,
    OvertimeInterval,
    PreviousAssignment,
    PreviousPlan,
    Schedule,
    ScheduleIssue,
    SolveRequest,
    SolveResult,
)

RecordRow = tuple[str, str, dict[str, Any]]
"""`(kind, id, body)` of one record to store."""

MAX_REPORTED_ISSUES = 500


@dataclass(frozen=True)
class StoredProblem:
    """A `problems` record resolved to its typed request, profile and provenance."""

    problem_id: str
    problem_digest: str
    snapshot_digest: str
    request: SolveRequest
    profile: PlanningProfile
    lineage_index: dict[str, Any]
    provenance: Provenance

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> StoredProblem:
        """Rebuild the typed problem from its stored JSON body."""
        return cls(
            problem_id=str(record["problem_id"]),
            problem_digest=str(record["problem_digest"]),
            snapshot_digest=str(record["snapshot_digest"]),
            request=SolveRequest.model_validate(record["request"]),
            profile=PlanningProfile.model_validate(record["profile"]),
            lineage_index=dict(record["lineage_index"]),
            provenance=Provenance.from_record(dict(record)),
        )


def now() -> str:
    """The service timestamp stored on new records."""
    return datetime.now(UTC).isoformat()


def build_records(
    snapshot: ErpSnapshot,
    profile: PlanningProfile,
    result: BuildResult,
    provenance: Provenance,
) -> tuple[BuildRecord, list[RecordRow]]:
    """The build summary (first 500 issues) and its `erp_snapshots` and `problems` records.

    A blocked build has only the snapshot record, whose report names the hard issues. The
    problem record carries the signed-artifact `provenance` of its profile and K-table.
    """
    record = BuildRecord(
        status=result.status,
        problem_id=result.request.problem.id if result.request is not None else None,
        problem_digest=result.problem_digest,
        snapshot_digest=result.snapshot_digest,
        profile_digest=result.profile_digest,
        builder_version=result.builder_version,
        counts=dict(result.counts),
        issues=result.issues[:MAX_REPORTED_ISSUES],
        issue_total=len(result.issues),
    )
    summary = record.model_dump(mode="json")
    snapshot_body = {
        "id": result.snapshot_digest,
        "snapshot": snapshot.model_dump(mode="json"),
        "report": summary,
        "validity": "provisional",
        "evidence_kind": "erp_snapshot",
    }
    rows: list[RecordRow] = [("erp_snapshots", result.snapshot_digest, snapshot_body)]
    if result.request is not None and record.problem_id is not None:
        problem_body = {
            **summary,
            "id": record.problem_id,
            "request": result.request.model_dump(mode="json"),
            "lineage_index": {
                op: lineage.model_dump(mode="json") for op, lineage in result.lineage_index.items()
            },
            "profile": profile.model_dump(mode="json"),
            **provenance.record_fields(),
        }
        rows.append(("problems", record.problem_id, problem_body))
    return record, rows


def objective_values(values: Mapping[ObjectiveKind, float]) -> dict[str, float]:
    """Objective values with plain string keys, as stored."""
    return {str(kind): float(value) for kind, value in values.items()}


def job_index(request: SolveRequest, extra: Iterable[Job] = ()) -> dict[str, str]:
    """Operation id -> job id for every operation of the problem plus `extra` jobs."""
    return {op.id: job.id for job in (*request.problem.jobs, *extra) for op in job.operations}


def _issues(issues: Iterable[ScheduleIssue]) -> list[dict[str, Any]]:
    return [found.model_dump(mode="json") for found in issues]


def _assignments(schedule: Schedule, job_of: Mapping[str, str]) -> list[dict[str, Any]]:
    return [
        {**a.model_dump(mode="json"), "job_id": job_of.get(a.operation_id)}
        for a in schedule.assignments
    ]


def overtime_seconds(request: SolveRequest, schedule: Schedule) -> dict[str, float]:
    """Seconds each machine with priced overtime works inside it (D-125); 0.0 when none."""
    calendars = {calendar.id: calendar.overtime for calendar in request.problem.calendars}
    windows = {
        machine.id: calendars[machine.calendar_id]
        for machine in request.problem.machines
        if machine.calendar_id is not None and calendars.get(machine.calendar_id)
    }
    starts = {machine_id: [w.start for w in found] for machine_id, found in windows.items()}
    totals = dict.fromkeys(sorted(windows), 0.0)
    for assignment in schedule.assignments:
        if assignment.machine_id in windows:
            seconds = _overlap(
                assignment, windows[assignment.machine_id], starts[assignment.machine_id]
            )
            totals[assignment.machine_id] += seconds
    return totals


def _overlap(
    assignment: Assignment, windows: Sequence[OvertimeInterval], starts: Sequence[float]
) -> float:
    """Seconds of `[start, end)` inside sorted, disjoint `windows` (bisect, then walk)."""
    index = max(bisect_right(starts, assignment.start) - 1, 0)
    total = 0.0
    while index < len(windows) and windows[index].start < assignment.end:
        window = windows[index]
        total += max(0.0, min(assignment.end, window.end) - max(assignment.start, window.start))
        index += 1
    return total


def _plan_body(problem: StoredProblem, plan_id: str, origin: Mapping[str, Any]) -> dict[str, Any]:
    explicit = "objective" in problem.request.profile.model_fields_set
    objective = problem.request.profile.objective
    return {
        "id": plan_id,
        "problem_id": problem.problem_id,
        "problem_digest": problem.problem_digest,
        "snapshot_digest": problem.snapshot_digest,
        "source": "erp",
        "schema_version": problem.request.schema_version,
        "objective_spec": objective.model_dump(mode="json", exclude_unset=explicit),
        **origin,
        "validity": "provisional",
        "created_at": now(),
    }


def _candidate_record(
    problem: StoredProblem,
    plan_id: str,
    candidate: CandidateLike,
    origin: Mapping[str, Any],
    job_of: Mapping[str, str],
) -> dict[str, Any]:
    return {
        **_plan_body(problem, plan_id, origin),
        "evidence_kind": "portfolio_candidate",
        "candidate_id": candidate.id,
        "pipeline": list(candidate.pipeline),
        "schedule": candidate.schedule.model_dump(mode="json", exclude={"assignments"}),
        "assignments": _assignments(candidate.schedule, job_of),
        "overtime_seconds": overtime_seconds(problem.request, candidate.schedule),
        "objective_values": objective_values(candidate.objective_values),
        "verified": candidate.verified,
        "issues": _issues(candidate.issues),
    }


def portfolio_records(
    problem: StoredProblem,
    portfolio_id: str,
    result: PortfolioResultLike,
    origin: Mapping[str, Any],
) -> tuple[PortfolioRecord, list[RecordRow]]:
    """The summary and the records (one portfolio, one plan per candidate) of a result."""
    job_of = job_index(problem.request)
    plan_id = {c.id: f"{portfolio_id}:{c.id}" for c in result.candidates}
    summary = PortfolioRecord(
        portfolio_id=portfolio_id,
        problem_id=problem.problem_id,
        plan_ids=tuple(plan_id.values()),
        recommended_plan_id=plan_id.get(result.recommended) if result.recommended else None,
        menu_size=len(result.menu),
        statuses=tuple(
            PlanStatus(plan_id=plan_id[c.id], status=c.schedule.status, verified=c.verified)
            for c in result.candidates
        ),
    )
    plans = [
        ("plans", plan_id[c.id], _candidate_record(problem, plan_id[c.id], c, origin, job_of))
        for c in result.candidates
    ]
    portfolio = {
        "id": portfolio_id,
        "problem_id": problem.problem_id,
        "problem_digest": problem.problem_digest,
        "snapshot_digest": problem.snapshot_digest,
        "source": "erp",
        **origin,
        "candidate_ids": [c.id for c in result.candidates],
        "menu": [
            {
                "candidate_id": e.candidate_id,
                "supported": e.supported,
                "values": objective_values(e.values),
            }
            for e in result.menu
        ],
        "recommended": result.recommended,
        "issues": _issues(result.issues),
        "elapsed_seconds": float(result.elapsed_seconds),
        "summary": summary.model_dump(mode="json"),
        "validity": "provisional",
        "evidence_kind": "portfolio",
        "created_at": now(),
    }
    return summary, [("portfolios", portfolio_id, portfolio), *plans]


def rush_records(
    problem: StoredProblem,
    plan_id: str,
    result: SolveResult,
    rush_job: Job,
    origin: Mapping[str, Any],
) -> tuple[RushRecord, list[RecordRow]]:
    """The summary and the one `plans` record of a rush insertion."""
    schedule = result.schedule.model_dump(mode="json", exclude={"assignments"})
    summary = RushRecord(
        plan_id=plan_id,
        problem_id=problem.problem_id,
        status=result.schedule.status,
        verified=result.verified,
        relaxations=tuple(schedule.get("relaxations", ())),
    )
    body = {
        **_plan_body(problem, plan_id, origin),
        "evidence_kind": "rush_insert",
        "schedule": schedule,
        "assignments": _assignments(result.schedule, job_index(problem.request, (rush_job,))),
        "overtime_seconds": overtime_seconds(problem.request, result.schedule),
        "objective_values": objective_values(result.objective_values),
        "verified": result.verified,
        "issues": _issues(result.issues),
        "held_job_ids": list(result.held_job_ids),
        "features": list(result.features),
        "summary": summary.model_dump(mode="json"),
    }
    return summary, [("plans", plan_id, body)]


def previous_plan(plan: Mapping[str, Any]) -> PreviousPlan:
    """The TF-002 `PreviousPlan` of a stored plan: its assignments and problem digest."""
    return PreviousPlan(
        plan_id=str(plan["id"]),
        problem_digest=str(plan["problem_digest"]),
        assignments=tuple(
            PreviousAssignment(
                operation_id=a["operation_id"], machine_id=a["machine_id"], start=a["start"]
            )
            for a in plan["assignments"]
        ),
    )


def plan_page(
    plan: Mapping[str, Any],
    lineage_index: Mapping[str, Any],
    *,
    offset: int,
    limit: int,
    job_id: str | None,
) -> PlanPage:
    """One page of a stored plan's assignments, optionally of one job, with lineage."""
    rows = [a for a in plan["assignments"] if job_id is None or a.get("job_id") == job_id]
    window = rows[offset : offset + limit]
    end = offset + len(window)
    return PlanPage(
        plan_id=str(plan["id"]),
        problem_id=str(plan["problem_id"]),
        total=len(rows),
        offset=offset,
        limit=limit,
        next_offset=end if end < len(rows) else None,
        assignments=tuple(_page_row(a, lineage_index) for a in window),
    )


def _page_row(row: Mapping[str, Any], lineage_index: Mapping[str, Any]) -> PlanAssignment:
    lineage = lineage_index.get(row["operation_id"])
    return PlanAssignment(
        operation_id=row["operation_id"],
        job_id=row.get("job_id"),
        machine_id=row["machine_id"],
        start=row["start"],
        setup_end=row["setup_end"],
        end=row["end"],
        batch_id=row.get("batch_id"),
        lineage=Lineage.model_validate(lineage) if lineage is not None else None,
    )


def menu_record(portfolio: Mapping[str, Any]) -> MenuRecord:
    """The stored menu of a portfolio, each entry pointing at its plan id."""
    portfolio_id = str(portfolio["id"])
    return MenuRecord(
        portfolio_id=portfolio_id,
        entries=tuple(
            MenuRow(
                plan_id=f"{portfolio_id}:{entry['candidate_id']}",
                candidate_id=entry["candidate_id"],
                supported=entry["supported"],
                values=entry["values"],
            )
            for entry in portfolio["menu"]
        ),
    )
