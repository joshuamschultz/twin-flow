"""Agent-first use cases, independent of HTTP and UI state."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from twinflow.application import scenarios
from twinflow.application.repository import WorkspaceRepository

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Workspace:
    """One trusted local workspace with immutable scenarios and bounded experiments."""

    def __init__(
        self,
        root: str | Path,
        examples_root: str | Path = "examples",
        *,
        max_active_jobs: int = 4,
        max_replications: int = 50,
        max_records_per_kind: int = 10_000,
        max_evidence_rows: int = 1_000,
        max_content_bytes: int = 5_242_880,
        deployment_mode: str = "local",
    ) -> None:
        workspace_root = Path(root).resolve()
        workspace_root.mkdir(parents=True, exist_ok=True)
        self._ownership: IO[bytes] = (workspace_root / ".owner.lock").open("a+b")
        try:
            fcntl.flock(self._ownership.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._ownership.close()
            raise RuntimeError("Workspace is already owned by another service process") from exc
        self.repository = WorkspaceRepository(workspace_root)
        self.examples_root = Path(examples_root).resolve()
        self.max_active_jobs = max_active_jobs
        self.max_replications = max_replications
        self.max_records_per_kind = max_records_per_kind
        self.max_evidence_rows = max_evidence_rows
        self.max_content_bytes = max_content_bytes
        self.deployment_mode = deployment_mode
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="twinflow-workspace")
        self._lock = threading.Lock()
        self._closed = False
        self._canceled: set[str] = set()
        self._active: set[str] = set()
        self.repository.recover_jobs(_now())

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            self._canceled.update(self._active)
        self._pool.shutdown(wait=True, cancel_futures=False)
        fcntl.flock(self._ownership.fileno(), fcntl.LOCK_UN)
        self._ownership.close()
        self._closed = True

    def now(self) -> str:
        """Return the service timestamp used for repository audit events."""
        return _now()

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "mode": self.deployment_mode,
            "actions": [
                "validate",
                "import",
                "export",
                "branch",
                "evaluate",
                "compare",
                "draft",
                "answer",
                "ingest_events",
                "build_snapshot",
                "schedule",
                "query_evidence",
            ],
            "limits": {
                "max_content_bytes": self.max_content_bytes,
                "max_replications": self.max_replications,
                "max_active_jobs": self.max_active_jobs,
                "max_wall_seconds": 120,
            },
            "supported_profiles": ["manufacturing.basic", "office.basic", "supply_chain.basic"],
            "agent_workflow": [
                "discover schema",
                "collect facts",
                "resolve questions",
                "validate",
                "import",
                "branch",
                "evaluate",
                "compare",
                "export",
            ],
            "production_writeback": False,
        }

    def examples(self) -> list[dict[str, str]]:
        legacy = [
            {
                "id": p.parent.name,
                "name": p.parent.name.replace("-", " ").title(),
                "domain": "manufacturing",
            }
            for p in sorted(self.examples_root.glob("*/model.yaml"))
            if (p.parent / "plan.csv").is_file()
        ]
        capsules = [
            {
                "id": "capsule-" + p.name.removesuffix(".twin.yaml"),
                "name": p.name.removesuffix(".twin.yaml").replace("-", " ").title(),
                "domain": "supply_chain" if p.name.startswith("supply-") else "office",
            }
            for p in sorted((self.examples_root / "capsules").glob("*.twin.yaml"))
            if p.name != "spring.twin.yaml"
        ]
        return legacy + capsules

    def load_example(self, name: str) -> dict[str, Any]:
        example = next((e for e in self.examples() if e["id"] == name), None)
        if example is None:
            raise KeyError(name)
        if name.startswith("capsule-"):
            path = self.examples_root / "capsules" / (name.removeprefix("capsule-") + ".twin.yaml")
            return self.import_scenario(path.read_text(encoding="utf-8"), example["name"])
        capsule = scenarios.example_capsule(
            self.examples_root / name / "model.yaml", self.examples_root / name / "plan.csv"
        )
        return self.import_scenario(json.dumps(capsule.to_dict()), example["name"])

    def import_scenario(
        self, content: str, name: str, parent_id: str | None = None
    ) -> dict[str, Any]:
        if self.repository.count("scenarios") >= self.max_records_per_kind:
            raise ValueError("Scenario quota reached")
        capsule = scenarios.validate_content(content)
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "name": name.strip() or "Untitled scenario",
            "digest": capsule.digest,
            "parent_id": parent_id,
            "created_at": _now(),
            "capsule": capsule.to_dict(),
            "summary": scenarios.describe(capsule),
            "validity": "provisional",
        }
        self.repository.create("scenarios", record["id"], record)
        self.repository.append_audit(_now(), "import", "scenario", record["id"], "created")
        return record

    def branch(self, scenario_id: str, name: str, content: str) -> dict[str, Any]:
        parent = self.repository.get("scenarios", scenario_id)
        # Explicit edited content is validated. Parent relationship is server-owned.
        child = scenarios.validate_content(content).to_dict()
        child["provenance"]["parent_digest"] = parent["digest"]
        return self.import_scenario(json.dumps(child), name, scenario_id)

    def submit(self, scenario_id: str, reps: int, seed: int, key: str) -> dict[str, Any]:
        if (
            not 1 <= reps <= self.max_replications
            or not 0 <= seed <= 2**32 - 1
            or not key
            or len(key) > 200
        ):
            raise ValueError("Invalid experiment settings or request key")
        scenario = self.repository.get("scenarios", scenario_id)
        fingerprint = hashlib.sha256(
            json.dumps([scenario_id, scenario["digest"], reps, seed]).encode()
        ).hexdigest()
        proposed = {
            "id": uuid.uuid4().hex,
            "scenario_id": scenario_id,
            "status": "queued",
            "created_at": _now(),
            "reps": reps,
            "seed": seed,
            "result": None,
            "error": None,
        }
        with self._lock:
            job_id, created = self.repository.claim_job(
                key, fingerprint, proposed, self.max_active_jobs
            )
            if created:
                self._active.add(job_id)
                self._pool.submit(self._execute, job_id, scenario["capsule"])
                self.repository.append_audit(_now(), "evaluate", "job", job_id, "queued")
        return self.repository.get("jobs", job_id)

    def _is_canceled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._canceled

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.repository.get("jobs", job_id)
        if job["status"] in ("queued", "running", "cancel_requested"):
            with self._lock:
                self._canceled.add(job_id)
                changed = self.repository.transition_job(
                    job_id, ("queued", "running"), {"status": "cancel_requested"}
                )
                if changed is not None:
                    job = changed
                    self.repository.append_audit(_now(), "cancel", "job", job_id, "requested")
        return self.repository.get("jobs", job_id)

    def _execute(self, job_id: str, capsule: dict[str, Any]) -> None:
        job = self.repository.get("jobs", job_id)
        try:
            if self._is_canceled(job_id):
                raise InterruptedError("Experiment canceled")
            running = self.repository.transition_job(job_id, ("queued",), {"status": "running"})
            if running is None:
                raise InterruptedError("Experiment canceled")
            job = running
            result = scenarios.evaluate_capsule(
                capsule,
                self.repository.root / "artifacts" / job_id,
                job["reps"],
                job["seed"],
                lambda: self._is_canceled(job_id),
            )
            if self._is_canceled(job_id):
                raise InterruptedError("Experiment canceled")
            changes = {"status": "completed", "result": result, "finished_at": _now()}
            if self.repository.transition_job(job_id, ("running",), changes) is None:
                self._finish_cancellation(job_id)
        except InterruptedError:
            self.repository.transition_job(
                job_id,
                ("queued", "running", "cancel_requested"),
                {"status": "canceled", "error": None, "finished_at": _now()},
            )
        except (ValueError, TimeoutError) as exc:
            failed = self.repository.transition_job(
                job_id,
                ("running",),
                {"status": "failed", "error": str(exc), "finished_at": _now()},
            )
            if failed is None:
                self._finish_cancellation(job_id)
        except Exception:
            log.exception("Workspace experiment failed: %s", job_id)
            failed = self.repository.transition_job(
                job_id,
                ("running",),
                {
                    "status": "failed",
                    "error": "Experiment failed; inspect the server log with this job ID",
                    "finished_at": _now(),
                },
            )
            if failed is None:
                self._finish_cancellation(job_id)
        finally:
            with self._lock:
                self._active.discard(job_id)
                self._canceled.discard(job_id)

    def _finish_cancellation(self, job_id: str) -> None:
        self.repository.transition_job(
            job_id,
            ("cancel_requested",),
            {"status": "canceled", "error": None, "finished_at": _now()},
        )

    def compare(self, baseline_id: str, candidate_id: str) -> dict[str, Any]:
        a, b = (self.repository.get("jobs", i) for i in (baseline_id, candidate_id))
        if any(j["status"] != "completed" for j in (a, b)):
            raise ValueError("Both experiments must be completed")
        if a["reps"] != b["reps"] or a["seed"] != b["seed"]:
            raise ValueError("Compare requires matching replication count and seed")
        sa, sb = (self.repository.get("scenarios", j["scenario_id"]) for j in (a, b))
        if sa["capsule"]["snapshot"] != sb["capsule"]["snapshot"]:
            raise ValueError("Compare requires the same operational snapshot")
        if a["result"].get("domain", "manufacturing") != b["result"].get("domain", "manufacturing"):
            raise ValueError("Compare requires the same domain")
        if a["result"]["metrics"].keys() != b["result"]["metrics"].keys():
            raise ValueError("Compare requires compatible metric contracts")
        return {
            "baseline_id": baseline_id,
            "candidate_id": candidate_id,
            "delta": {k: b["result"]["metrics"][k] - v for k, v in a["result"]["metrics"].items()},
            "interpretation": (
                "Candidate minus baseline; descriptive simulated differences, "
                "not a significance claim."
            ),
        }

    def save_draft(
        self, name: str, facts: dict[str, Any], answers: list[dict[str, str]]
    ) -> dict[str, Any]:
        facts = dict(facts)
        for answer in answers:
            key, value = answer.get("id"), answer.get("answer")
            if key and value:
                facts[key] = value
        questions = [
            {"id": key, "question": question}
            for key, question in (
                ("process", "What work flows through the operation, and in what sequence?"),
                ("resources", "Which people, machines, or suppliers limit the work?"),
                (
                    "demand",
                    "Which orders/cases, quantities, release dates and due dates must be modeled?",
                ),
                ("durations", "What is known about processing times and variability?"),
                (
                    "constraints",
                    "Which calendars, materials, approvals, and quality rules can block progress?",
                ),
            )
            if not facts.get(key)
        ]
        draft: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "name": name,
            "facts": facts,
            "answers": answers,
            "questions": questions,
            "created_at": _now(),
            "status": "draft",
            "next_step": "Map these facts to the capsule schema, validate, then import.",
        }
        self.repository.create("drafts", draft["id"], draft)
        return draft

    def answer_draft(self, draft_id: str, answers: list[dict[str, str]]) -> dict[str, Any]:
        previous = self.repository.get("drafts", draft_id)
        # Each answer creates a retained revision rather than discarding prior intake.
        revision = self.save_draft(
            previous["name"], previous["facts"], previous["answers"] + answers
        )
        revision["parent_id"] = draft_id
        self.repository.update("drafts", revision["id"], revision)
        return revision

    def evidence(self, job_id: str, *, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """Return bounded embedded evidence; callers never provide filesystem paths."""
        if offset < 0 or limit < 1 or limit > self.max_evidence_rows:
            raise ValueError("Invalid evidence page")
        job = self.repository.get("jobs", job_id)
        result = job.get("result")
        evidence: list[Any] = []
        if isinstance(result, dict):
            evidence = list(result.get("outcomes", []))
            for index, sample in enumerate(result.get("per_replication", [])):
                for key in ("trace", "gate_results", "shortages", "order_forecasts", "cases"):
                    for row in sample.get(key, []):
                        evidence.append({"replication": index, "kind": key, "record": row})
        return {
            "job_id": job_id,
            "offset": offset,
            "limit": limit,
            "items": evidence[offset : offset + limit],
            "total": len(evidence),
        }
