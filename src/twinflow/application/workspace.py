"""Agent-first use cases, independent of HTTP and UI state."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from twinflow.application import scenarios
from twinflow.application.repository import WorkspaceRepository

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Workspace:
    """One trusted local workspace with immutable scenarios and bounded experiments."""

    def __init__(self, root: str | Path, examples_root: str | Path = "examples") -> None:
        self.repository = WorkspaceRepository(root)
        self.examples_root = Path(examples_root).resolve()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="twinflow-workspace")
        self._lock = threading.Lock()
        self._canceled: set[str] = set()
        self._active: set[str] = set()
        for job in self.repository.list("jobs", 1000):
            if job["status"] in ("queued", "running"):
                job.update(
                    status="interrupted", error="Service restarted before result publication"
                )
                self.repository.update("jobs", job["id"], job)

    def close(self) -> None:
        with self._lock:
            self._canceled.update(self._active)
        self._pool.shutdown(wait=True, cancel_futures=False)

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "mode": "local",
            "actions": ["import", "export", "branch", "evaluate", "compare", "draft", "answer"],
            "limits": {
                "max_content_bytes": 5_242_880,
                "max_replications": 50,
                "max_active_jobs": 4,
                "max_wall_seconds": 120,
            },
            "supported_profiles": ["manufacturing.basic"],
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
        return [
            {"id": p.parent.name, "name": p.parent.name.replace("-", " ").title()}
            for p in sorted(self.examples_root.glob("*/model.yaml"))
            if (p.parent / "plan.csv").is_file()
        ]

    def load_example(self, name: str) -> dict[str, Any]:
        if name not in {e["id"] for e in self.examples()}:
            raise KeyError(name)
        capsule = scenarios.example_capsule(
            self.examples_root / name / "model.yaml", self.examples_root / name / "plan.csv"
        )
        return self.import_scenario(json.dumps(capsule.to_dict()), name.replace("-", " ").title())

    def import_scenario(
        self, content: str, name: str, parent_id: str | None = None
    ) -> dict[str, Any]:
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
        return record

    def branch(self, scenario_id: str, name: str, content: str) -> dict[str, Any]:
        parent = self.repository.get("scenarios", scenario_id)
        # Explicit edited content is validated. Parent relationship is server-owned.
        child = scenarios.validate_content(content).to_dict()
        child["provenance"]["parent_digest"] = parent["digest"]
        return self.import_scenario(json.dumps(child), name, scenario_id)

    def submit(self, scenario_id: str, reps: int, seed: int, key: str) -> dict[str, Any]:
        if not 1 <= reps <= 50 or not 0 <= seed <= 2**32 - 1 or not key or len(key) > 200:
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
            if len(self._active) >= 4:
                raise ValueError("Workspace compute queue is full; wait for an existing job")
            job_id, created = self.repository.claim_job(key, fingerprint, proposed)
            if created:
                self._active.add(job_id)
                self._pool.submit(self._execute, job_id, scenario["capsule"])
        return self.repository.get("jobs", job_id)

    def _is_canceled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._canceled

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.repository.get("jobs", job_id)
        if job["status"] in ("queued", "running", "cancel_requested"):
            with self._lock:
                self._canceled.add(job_id)
                job["status"] = "cancel_requested"
                self.repository.update("jobs", job_id, job)
        return job

    def _execute(self, job_id: str, capsule: dict[str, Any]) -> None:
        job = self.repository.get("jobs", job_id)
        try:
            if self._is_canceled(job_id):
                raise InterruptedError("Experiment canceled")
            job["status"] = "running"
            self.repository.update("jobs", job_id, job)
            result = scenarios.evaluate_capsule(
                capsule,
                self.repository.root / "artifacts" / job_id,
                job["reps"],
                job["seed"],
                lambda: self._is_canceled(job_id),
            )
            if self._is_canceled(job_id):
                raise InterruptedError("Experiment canceled")
            job.update(status="completed", result=result, finished_at=_now())
        except InterruptedError:
            job.update(status="canceled", error=None, finished_at=_now())
        except (ValueError, TimeoutError) as exc:
            job.update(status="failed", error=str(exc), finished_at=_now())
        except Exception:
            log.exception("Workspace experiment failed: %s", job_id)
            job.update(
                status="failed",
                error="Experiment failed; inspect the server log with this job ID",
                finished_at=_now(),
            )
        finally:
            self.repository.update("jobs", job_id, job)
            with self._lock:
                self._active.discard(job_id)
                self._canceled.discard(job_id)

    def compare(self, baseline_id: str, candidate_id: str) -> dict[str, Any]:
        a, b = (self.repository.get("jobs", i) for i in (baseline_id, candidate_id))
        if any(j["status"] != "completed" for j in (a, b)):
            raise ValueError("Both experiments must be completed")
        if a["reps"] != b["reps"] or a["seed"] != b["seed"]:
            raise ValueError("Compare requires matching replication count and seed")
        sa, sb = (self.repository.get("scenarios", j["scenario_id"]) for j in (a, b))
        if sa["capsule"]["snapshot"] != sb["capsule"]["snapshot"]:
            raise ValueError("Compare requires the same operational snapshot")
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
