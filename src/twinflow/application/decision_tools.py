"""Framework-independent tools for building twins and retrieving decision evidence."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any

from twinflow.application import scenarios
from twinflow.application.workspace import Workspace
from twinflow.data import (
    FreshnessRule,
    SnapshotBuilder,
    SQLiteEventStore,
    parse_event,
    snapshot_to_dict,
)


class DecisionTools:
    """Thin use cases over explicit domain contracts; no language-model dependency."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def validate(self, content: str) -> dict[str, Any]:
        from twinflow.scenario import CapsuleValidationError

        try:
            capsule = scenarios.validate_content(content)
        except CapsuleValidationError as exc:
            return {"valid": False, "issues": [asdict(issue) for issue in exc.issues]}
        except ValueError as exc:
            return {"valid": False, "issues": [{"path": "$", "message": str(exc)}]}
        return {
            "valid": True,
            "digest": capsule.digest,
            "summary": scenarios.describe(capsule),
            "issues": [],
        }

    def ingest(self, records: list[dict[str, Any]]) -> dict[str, int]:
        if not 1 <= len(records) <= 1000:
            raise ValueError("Import between 1 and 1000 events per batch")
        # Parse the complete batch before persistence; malformed batches cannot partially apply.
        events = [parse_event(record) for record in records]
        store = SQLiteEventStore(self.workspace.repository.root / "operational-data.sqlite")
        result = store.ingest(events)
        return {"inserted": result.inserted, "duplicates": result.duplicates}

    def snapshot(self, known_at: str, freshness_rules: list[dict[str, Any]]) -> dict[str, Any]:
        if len(freshness_rules) > 100:
            raise ValueError("At most 100 freshness rules are supported")
        rules = tuple(FreshnessRule(**rule) for rule in freshness_rules)
        store = SQLiteEventStore(self.workspace.repository.root / "operational-data.sqlite")
        snapshot = SnapshotBuilder(store, freshness_rules=rules).as_known_at(
            datetime.fromisoformat(known_at), save=True
        )
        document = snapshot_to_dict(snapshot)
        # Preserve inspectable snapshots in the common workspace catalog as well.
        identity = snapshot.snapshot_id
        try:
            self.workspace.repository.get("snapshots", identity)
        except KeyError:
            self.workspace.repository.create("snapshots", identity, {"id": identity, **document})
        return document

    def query(
        self,
        job_id: str,
        topic: str,
        entity_id: str | None = None,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Retrieve bounded evidence for an agent to explain; never invent narrative answers."""
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("Evidence pages require offset >= 0 and limit 1..1000")
        job = self.workspace.repository.get("jobs", job_id)
        if job["status"] != "completed":
            raise ValueError("Wait for a completed experiment before querying its evidence")
        scenario = self.workspace.repository.get("scenarios", job["scenario_id"])
        result = job["result"]
        domain = result.get("domain", "manufacturing")
        if topic == "metrics":
            evidence = result["metrics"]
        elif topic == "assumptions":
            evidence = {
                "assumptions": result["assumptions"],
                "provenance": scenario["capsule"]["provenance"],
            }
        elif topic == "process":
            evidence = scenario["summary"]
        elif topic in ("dates", "blockers"):
            if domain == "manufacturing":
                evidence = (
                    result["intervals"].get("completion_distributions", {})
                    if topic == "dates"
                    else result["outcomes"]
                )
                if entity_id and isinstance(evidence, dict):
                    evidence = {entity_id: evidence[entity_id]} if entity_id in evidence else {}
            else:
                evidence = []
                for index, sample in enumerate(result.get("per_replication", [])):
                    key = "order_forecasts" if domain == "supply_chain" else "cases"
                    values = (
                        sample.get(key, [])
                        if topic == "dates"
                        else (
                            sample.get("shortages", []) + sample.get("gate_results", [])
                            if domain == "supply_chain"
                            else sample.get("trace", [])
                        )
                    )
                    if entity_id:
                        values = [
                            row
                            for row in values
                            if entity_id in (row.get("order_id"), row.get("case_id"))
                            or entity_id in row.get("affected_order_ids", [])
                        ]
                    evidence.extend({"replication": index, "record": value} for value in values)
        else:
            raise ValueError("Topic must be metrics, dates, blockers, assumptions, or process")
        rows: list[Any] = []
        if isinstance(evidence, dict):
            for key, value in evidence.items():
                if isinstance(value, list):
                    rows.extend({"field": key, "record": item} for item in value)
                else:
                    rows.append({"field": key, "record": value})
        else:
            rows = evidence
        page = []
        byte_count = 0
        for row in rows[offset : offset + limit]:
            size = len(json.dumps(row).encode())
            if size > 65_536:
                row = {"omitted": True, "reason": "Record exceeds inline evidence size; use export"}
                size = 100
            if byte_count + size > 262_144:
                break
            byte_count += size
            page.append(row)
        next_offset = offset + len(page)
        answer = {
            "job_id": job_id,
            "scenario_id": scenario["id"],
            "scenario_digest": scenario["digest"],
            "snapshot_id": scenario["capsule"]["snapshot"].get("id"),
            "topic": topic,
            "domain": domain,
            "evidence": page,
            "total": len(rows),
            "offset": offset,
            "next_offset": next_offset if next_offset < len(rows) else None,
            "export_route": f"/api/workspace/jobs/{job_id}/export",
            "validity": "provisional",
            "interpretation": result["interpretation"],
        }
        return answer

    def schedule(self, problem: dict[str, Any], backend: str, time_limit: float) -> dict[str, Any]:
        from twinflow.scheduling import problem_from_dict, solve
        from twinflow.scheduling.ortools import ORToolsSolver

        if backend not in ("baseline", "ortools") or not 0 < time_limit <= 30:
            raise ValueError("Choose baseline/ortools and a time limit from 0 to 30 seconds")
        parsed = problem_from_dict(problem)
        result = solve(
            parsed,
            solver=ORToolsSolver() if backend == "ortools" else None,
            time_limit_seconds=time_limit,
        )
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "problem": problem,
            "backend": backend,
            "result": asdict(result),
        }
        # Ensure portable evidence before storing it.
        json.dumps(record, allow_nan=False)
        self.workspace.repository.create("schedules", record["id"], record)
        return record
