"""Validated SimPy execution for the office profile."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import simpy

from twinflow.domain.office.contracts import (
    Case,
    Task,
    parse_model,
    parse_snapshot,
)
from twinflow.domain.registry import DomainAdapter, DomainIssue


class OfficeAdapter(DomainAdapter):
    domain = "office"
    capabilities = frozenset({"office.basic"})

    def validate(self, model: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[DomainIssue]:
        issues: list[DomainIssue] = []
        try:
            parsed_model = parse_model(dict(model))
            parsed_snapshot = parse_snapshot(dict(snapshot))
        except (TypeError, ValueError) as exc:
            return [DomainIssue("$", str(exc), suggestion="Repair the office model or snapshot")]
        task_ids = [task.id for task in parsed_model.tasks]
        resource_ids = [resource.id for resource in parsed_model.resources]
        if len(task_ids) != len(set(task_ids)):
            issues.append(DomainIssue("$.tasks", "task IDs must be unique"))
        if len(resource_ids) != len(set(resource_ids)):
            issues.append(DomainIssue("$.resources", "resource IDs must be unique"))
        tasks = {task.id: task for task in parsed_model.tasks}
        resources = {role for resource in parsed_model.resources for role in resource.roles}
        docs = {document.id: document.revision for document in parsed_model.documents}
        for task in parsed_model.tasks:
            if not task.role:
                issues.append(
                    DomainIssue(
                        f"$.tasks[{task.id}].role", "task must name a finite resource role"
                    )
                )
            for prerequisite in task.prerequisites:
                if prerequisite not in tasks:
                    issues.append(
                        DomainIssue(
                            f"$.tasks[{task.id}].prerequisites",
                            f"unknown prerequisite {prerequisite!r}",
                        )
                    )
            if task.role and task.role not in resources:
                issues.append(
                    DomainIssue(
                        f"$.tasks[{task.id}].role", f"no resource provides role {task.role!r}"
                    )
                )
            if task.document_id and task.document_id not in docs:
                issues.append(
                    DomainIssue(
                        f"$.tasks[{task.id}].approval.document_id",
                        f"unknown document {task.document_id!r}",
                    )
                )
            if task.max_rework < 0:
                issues.append(DomainIssue(f"$.tasks[{task.id}].max_rework", "must be non-negative"))
        issues.extend(_cycle_issues(tasks))
        case_ids = [case.id for case in parsed_snapshot.cases]
        if len(case_ids) != len(set(case_ids)):
            issues.append(DomainIssue("$.cases", "case IDs must be unique"))
        for case in parsed_snapshot.cases:
            case_docs = {document.id: document.revision for document in case.documents}
            for task in parsed_model.tasks:
                for field in task.required_data:
                    if field not in case.data:
                        issues.append(
                            DomainIssue(
                                f"$.cases[{case.id}].data.{field}",
                                f"missing required data for task {task.id!r}",
                                suggestion="Supply the fact or leave the case incomplete",
                            )
                        )
                if task.document_id and case_docs.get(task.document_id) != task.document_revision:
                    issues.append(
                        DomainIssue(
                            f"$.cases[{case.id}].documents",
                            f"stale or missing revision for {task.document_id!r} "
                            f"required by task {task.id!r}",
                        )
                    )
                if task.approval_role and not any(
                    a.task_id == task.id
                    and a.document_id == task.document_id
                    and a.revision == task.document_revision
                    and a.role == task.approval_role
                    for a in case.approvals
                ):
                    issues.append(
                        DomainIssue(
                            f"$.cases[{case.id}].approvals",
                            f"missing approval for task {task.id!r}",
                            suggestion=(
                                "Record an explicit approval; the simulator never invents one"
                            ),
                        )
                    )
        return issues

    def describe(self, model: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, object]:
        parsed = parse_model(dict(model))
        return {
            "domain": self.domain,
            "revision": parsed.revision,
            "nodes": [
                {"id": task.id, "duration": task.duration, "role": task.role}
                for task in parsed.tasks
            ],
            "edges": [
                {"from": prerequisite, "to": task.id}
                for task in parsed.tasks
                for prerequisite in task.prerequisites
            ],
            "case_count": len(parse_snapshot(dict(snapshot)).cases),
        }

    def evaluate(
        self,
        model: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        *,
        seed: int = 0,
        artifact_dir: str | None = None,
        limits: Mapping[str, int | float] | None = None,
    ) -> dict[str, object]:
        del seed  # Deterministic office durations have no random draw yet.
        issues = self.validate(model, snapshot)
        structural = [
            issue
            for issue in issues
            if not (
                issue.path.startswith("$.cases[")
                and any(
                    marker in issue.message
                    for marker in (
                        "missing required data",
                        "stale or missing revision",
                        "missing approval",
                    )
                )
            )
        ]
        if structural:
            return {"status": "invalid", "issues": [issue.__dict__ for issue in issues]}
        parsed_model = parse_model(dict(model))
        parsed_snapshot = parse_snapshot(dict(snapshot))
        max_events = int((limits or {}).get("max_events", 100_000))
        env = simpy.Environment()
        resources = {
            resource.id: simpy.Resource(env, capacity=resource.capacity)
            for resource in parsed_model.resources
        }
        statuses: dict[str, dict[str, bool]] = {case.id: {} for case in parsed_snapshot.cases}
        traces: list[dict[str, object]] = []
        event_count = 0

        def process_case(case: Case) -> Any:
            nonlocal event_count
            events = {task.id: env.event() for task in parsed_model.tasks}
            ordered = parsed_model.tasks
            for task in ordered:
                env.process(run_task(case, task, events, statuses[case.id]))
            yield env.timeout(0)

        def run_task(
            case: Case, task: Task, events: dict[str, simpy.Event], case_status: dict[str, bool]
        ) -> Any:
            nonlocal event_count
            if case.start_time:
                yield env.timeout(max(0.0, case.start_time - env.now))
            for prerequisite in task.prerequisites:
                yield events[prerequisite]
                if not case_status.get(prerequisite, False):
                    case_status[task.id] = False
                    traces.append(
                        {
                            "case_id": case.id,
                            "task_id": task.id,
                            "event": "blocked",
                            "reason": "prerequisite_failed",
                            "time": env.now,
                        }
                    )
                    events[task.id].succeed()
                    return
            if task.required_data and any(field not in case.data for field in task.required_data):
                case_status[task.id] = False
                traces.append(
                    {
                        "case_id": case.id,
                        "task_id": task.id,
                        "event": "blocked",
                        "reason": "missing_data",
                        "time": env.now,
                    }
                )
                events[task.id].succeed()
                return
            if task.document_id and not any(
                document.id == task.document_id and document.revision == task.document_revision
                for document in case.documents
            ):
                case_status[task.id] = False
                traces.append(
                    {
                        "case_id": case.id,
                        "task_id": task.id,
                        "event": "blocked",
                        "reason": "stale_document",
                        "time": env.now,
                    }
                )
                events[task.id].succeed()
                return
            if task.approval_role and not any(
                approval.task_id == task.id and approval.role == task.approval_role
                for approval in case.approvals
            ):
                case_status[task.id] = False
                traces.append(
                    {
                        "case_id": case.id,
                        "task_id": task.id,
                        "event": "blocked",
                        "reason": "missing_approval",
                        "time": env.now,
                    }
                )
                events[task.id].succeed()
                return
            resource = next(
                (item for item in parsed_model.resources if task.role in item.roles), None
            )
            if resource is not None:
                request = resources[resource.id].request()
                yield request
                try:
                    start = _calendar_start(env.now, resource.calendar)
                    if start > env.now:
                        traces.append(
                            {
                                "case_id": case.id,
                                "task_id": task.id,
                                "event": "waiting",
                                "reason": "calendar",
                                "time": env.now,
                            }
                        )
                        yield env.timeout(start - env.now)
                    traces.append(
                        {
                            "case_id": case.id,
                            "task_id": task.id,
                            "event": "started",
                            "time": env.now,
                            "resource_id": resource.id,
                        }
                    )
                    attempts = 1 + min(task.max_rework, int((case.rework or {}).get(task.id, 0)))
                    for attempt in range(attempts):
                        if attempt:
                            traces.append(
                                {
                                    "case_id": case.id,
                                    "task_id": task.id,
                                    "event": "rework",
                                    "attempt": attempt + 1,
                                    "time": env.now,
                                }
                            )
                        yield env.timeout(task.duration)
                        event_count += 1
                        if event_count > max_events:
                            case_status[task.id] = False
                            traces.append(
                                {
                                    "case_id": case.id,
                                    "task_id": task.id,
                                    "event": "blocked",
                                    "reason": "event_budget",
                                    "time": env.now,
                                }
                            )
                            events[task.id].succeed()
                            return
                    case_status[task.id] = True
                    traces.append(
                        {
                            "case_id": case.id,
                            "task_id": task.id,
                            "event": "completed",
                            "time": env.now,
                        }
                    )
                finally:
                    resources[resource.id].release(request)
            else:
                case_status[task.id] = True
            events[task.id].succeed()

        for case in parsed_snapshot.cases:
            env.process(process_case(case))
        env.run()
        per_case = []
        for case in parsed_snapshot.cases:
            completed = [task.id for task in parsed_model.tasks if statuses[case.id].get(task.id)]
            per_case.append(
                {
                    "case_id": case.id,
                    "outcome": "completed"
                    if len(completed) == len(parsed_model.tasks)
                    else "incomplete",
                    "completed_tasks": completed,
                }
            )
        result: dict[str, object] = {
            "status": "completed",
            "horizon": env.now,
            "metrics": {
                "case_count": len(per_case),
                "completed_cases": sum(item["outcome"] == "completed" for item in per_case),
                "incomplete_cases": sum(item["outcome"] != "completed" for item in per_case),
                "event_count": event_count,
            },
            "cases": per_case,
            "trace": traces,
        }
        if artifact_dir is not None:
            output = Path(artifact_dir)
            output.mkdir(parents=True, exist_ok=True)
            (output / "office-result.json").write_text(
                json.dumps(result, sort_keys=True), encoding="utf-8"
            )
            result["artifact"] = str(output / "office-result.json")
        return result


def _calendar_start(now: float, windows: tuple[tuple[float, float], ...]) -> float:
    for start, end in windows:
        if start <= now < end:
            return now
        if now < start:
            return start
    return now


def _cycle_issues(tasks: dict[str, Task]) -> list[DomainIssue]:
    visiting: set[str] = set()
    visited: set[str] = set()
    issues: list[DomainIssue] = []

    def visit(task_id: str) -> None:
        if task_id in visiting:
            issues.append(
                DomainIssue(f"$.tasks[{task_id}].prerequisites", "prerequisites must be acyclic")
            )
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for parent in tasks[task_id].prerequisites:
            if parent in tasks:
                visit(parent)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)
    return issues
