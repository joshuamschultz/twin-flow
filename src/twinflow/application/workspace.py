"""Agent-first use cases, independent of HTTP and UI state."""

from __future__ import annotations

import copy
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
from twinflow.application.linear_draft import LinearDraft, compile_draft
from twinflow.application.repository import WorkspaceRepository
from twinflow.application.schedule_intake import decode_upload, preview_mapping
from twinflow.application.schedule_problem import (
    Translation,
    build_request,
    firings_from_plan,
    record_fields,
)
from twinflow.plan.driver import RunDriver
from twinflow.policy import configured_runtime
from twinflow.primitives.location import ScheduledFiring
from twinflow.scenario import ScenarioCapsule
from twinflow.scheduling import SolveResult, get_solver, solve

log = logging.getLogger(__name__)

SIMULATION_HORIZON_SECONDS = 31_536_000.0
"""The DES runs at most one year of simulated time (`evaluate_capsule` max_sim_time)."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _status_explanation(result: SolveResult, backend: str) -> str:
    status = result.schedule.status
    if result.verified and status == "OPTIMAL":
        return "CP-SAT reported and the verifier checked an optimal assignment"
    if result.verified:
        if backend == "baseline":
            return "Verified baseline schedule"
        return "Best verified schedule found within the time limit; optimality unproved"
    if status == "INFEASIBLE":
        return "CP-SAT reported infeasibility for the translated finite problem"
    return "No verified feasibility or infeasibility conclusion is available"


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
        self.repository = WorkspaceRepository(
            workspace_root, max_records_per_kind=max_records_per_kind
        )
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
                "model_draft_compile",
                "model_draft_import",
                "schedule_upload_preview",
                "schedule_snapshot_commit",
                "schedule_snapshot_solve",
                "schedule_plan_simulate",
                "schedule_snapshot_baseline",
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
        self,
        content: str,
        name: str,
        parent_id: str | None = None,
        draft_link: dict[str, Any] | None = None,
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
        if draft_link is not None:
            record["draft_link"] = draft_link
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

    def submit_schedule_plan(self, plan_id: str, reps: int, seed: int, key: str) -> dict[str, Any]:
        if (
            not 1 <= reps <= min(self.max_replications, 10)
            or not 0 <= seed <= 2**32 - 1
            or not key
            or len(key) > 200
        ):
            raise ValueError("Invalid experiment settings or request key")
        plan_record = self.repository.get("schedule_plans", plan_id)
        simulation = plan_record["simulation"]
        if simulation["status"] != "available":
            raise ValueError(simulation["blocker"])
        scenario = self.repository.get("scenarios", plan_record["scenario_id"])
        snapshot = self.repository.get("schedule_snapshots", plan_record["schedule_snapshot_id"])
        self._plan_firings(plan_record, scenario, snapshot)
        simulation_capsule = self._schedule_capsule(scenario, snapshot, plan_record)
        scenarios.validate_content(json.dumps(simulation_capsule, allow_nan=False))
        fingerprint = hashlib.sha256(
            json.dumps([plan_id, plan_record["problem_digest"], reps, seed]).encode()
        ).hexdigest()
        proposed = {
            "id": uuid.uuid4().hex,
            "scenario_id": scenario["id"],
            "schedule_plan_id": plan_id,
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
                self.repository.append_audit(_now(), "simulate_schedule", "job", job_id, "queued")
        return self.repository.get("jobs", job_id)

    def submit_schedule_baseline(
        self, snapshot_id: str, reps: int, seed: int, key: str
    ) -> dict[str, Any]:
        if (
            not 1 <= reps <= min(self.max_replications, 10)
            or not 0 <= seed <= 2**32 - 1
            or not key
            or len(key) > 200
        ):
            raise ValueError("Invalid experiment settings or request key")
        snapshot = self.repository.get("schedule_snapshots", snapshot_id)
        scenario = self.repository.get("scenarios", snapshot["scenario_id"])
        if scenario["digest"] != snapshot["scenario_digest"]:
            raise ValueError("Schedule snapshot scenario digest has changed")
        translation = build_request(scenario, snapshot)
        if translation.status != "ready":
            issues = [issue.model_dump(mode="json") for issue in translation.issues]
            raise ValueError(
                "Schedule baseline requires a scenario the scheduler can translate: "
                + json.dumps(issues)
            )
        capsule = self._schedule_capsule(scenario, snapshot)
        scenarios.validate_content(json.dumps(capsule, allow_nan=False))
        fingerprint = hashlib.sha256(
            json.dumps(
                ["schedule_baseline", snapshot_id, snapshot["original_file_digest"], reps, seed]
            ).encode()
        ).hexdigest()
        proposed = {
            "id": uuid.uuid4().hex,
            "scenario_id": scenario["id"],
            "schedule_snapshot_id": snapshot_id,
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
                self.repository.append_audit(
                    _now(), "simulate_schedule_baseline", "job", job_id, "queued"
                )
        return self.repository.get("jobs", job_id)

    @staticmethod
    def _schedule_capsule(
        scenario: dict[str, Any],
        snapshot: dict[str, Any],
        plan_record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        capsule: dict[str, Any] = copy.deepcopy(scenario["capsule"])
        capsule["snapshot"]["id"] = snapshot["id"]
        capsule["snapshot"]["production_plan"] = [
            {
                "work_order_id": order["order_id"],
                "part": order["product"],
                "qty": order["quantity"],
                "start_date": str(order["release_second"]),
                "due_date": str(order["due_second"]),
            }
            for order in snapshot["orders"]
        ]
        capsule["experiment"]["id"] = (
            plan_record["id"] if plan_record is not None else snapshot["id"]
        )
        if plan_record is not None:
            capsule["provenance"]["schedule_plan_id"] = plan_record["id"]
        capsule["provenance"]["source_scenario_digest"] = scenario["digest"]
        capsule["provenance"]["schedule_snapshot_digest"] = snapshot["original_file_digest"]
        return capsule

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
            plan_id = job.get("schedule_plan_id")
            if plan_id is not None:
                plan_record = self.repository.get("schedule_plans", plan_id)
                scenario_record = self.repository.get("scenarios", job["scenario_id"])
                schedule_snapshot = self.repository.get(
                    "schedule_snapshots", plan_record["schedule_snapshot_id"]
                )
                firings = self._plan_firings(plan_record, scenario_record, schedule_snapshot)
                capsule = self._schedule_capsule(scenario_record, schedule_snapshot, plan_record)
                result = scenarios.evaluate_capsule(
                    capsule,
                    self.repository.root / "artifacts" / job_id,
                    job["reps"],
                    job["seed"],
                    lambda: self._is_canceled(job_id),
                    execution_schedule=firings,
                )
                result["schedule_plan"] = {
                    "id": plan_id,
                    "schedule_snapshot_id": schedule_snapshot["id"],
                    "problem_digest": plan_record["problem_digest"],
                    "solver_status": plan_record["result"]["schedule"]["status"],
                    "objective_spec": plan_record["objective_spec"],
                }
                result["simulation_input_digest"] = scenarios.validate_content(
                    json.dumps(capsule, allow_nan=False)
                ).digest
            elif job.get("schedule_snapshot_id") is not None:
                scenario_record = self.repository.get("scenarios", job["scenario_id"])
                schedule_snapshot = self.repository.get(
                    "schedule_snapshots", job["schedule_snapshot_id"]
                )
                capsule = self._schedule_capsule(scenario_record, schedule_snapshot)
                result = scenarios.evaluate_capsule(
                    capsule,
                    self.repository.root / "artifacts" / job_id,
                    job["reps"],
                    job["seed"],
                    lambda: self._is_canceled(job_id),
                    piece_release=True,
                )
                result["schedule_baseline"] = {
                    "schedule_snapshot_id": schedule_snapshot["id"],
                    "mode": "piece_dispatch_without_enforced_plan",
                }
                result["simulation_input_digest"] = scenarios.validate_content(
                    json.dumps(capsule, allow_nan=False)
                ).digest
            else:
                result = scenarios.evaluate_capsule(
                    capsule,
                    self.repository.root / "artifacts" / job_id,
                    job["reps"],
                    job["seed"],
                    lambda: self._is_canceled(job_id),
                )
            scenario = self.repository.get("scenarios", job["scenario_id"])
            snapshot = result["snapshot"]
            result.update(
                {
                    "schema_version": "1.1",
                    "scenario": {
                        "id": scenario["id"],
                        "digest": scenario["digest"],
                        "parent_id": scenario["parent_id"],
                    },
                    "snapshot_ref": {"id": snapshot.get("id"), "as_of": snapshot.get("as_of")},
                    "job": {
                        "id": job_id,
                        "status": "completed",
                        "seed": job["seed"],
                        "replications": job["reps"],
                    },
                    "model_validity": result.get("validity", "provisional"),
                    "replication_outcomes": result.get("outcomes", []),
                    "termination": {
                        "status": "completed",
                        "outcomes": result.get("outcomes", []),
                    },
                    "evidence": {
                        "routes": [
                            f"/api/workspace/jobs/{job_id}/query",
                            f"/api/workspace/jobs/{job_id}/evidence",
                            f"/api/workspace/jobs/{job_id}/export",
                        ]
                    },
                }
            )
            (self.repository.root / "artifacts" / job_id / "result.json").write_text(
                json.dumps(result, indent=2, allow_nan=False), encoding="utf-8"
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
        snapshot_a = a["result"].get("snapshot", sa["capsule"]["snapshot"])
        snapshot_b = b["result"].get("snapshot", sb["capsule"]["snapshot"])
        if snapshot_a != snapshot_b:
            raise ValueError("Compare requires the same operational snapshot")
        if a["result"].get("domain", "manufacturing") != b["result"].get("domain", "manufacturing"):
            raise ValueError("Compare requires the same domain")
        if a["result"]["metrics"].keys() != b["result"]["metrics"].keys():
            raise ValueError("Compare requires compatible metric contracts")
        return {
            "baseline_id": baseline_id,
            "candidate_id": candidate_id,
            "compared": {
                "baseline_scenario": {"id": sa["id"], "digest": sa["digest"]},
                "candidate_scenario": {"id": sb["id"], "digest": sb["digest"]},
                "snapshot_id": snapshot_a.get("id"),
                "snapshot_as_of": snapshot_a.get("as_of"),
                "seed": a["seed"],
                "replications": a["reps"],
                "domain": a["result"].get("domain", "manufacturing"),
                "metrics": sorted(a["result"]["metrics"]),
                "baseline_mode": (
                    "enforced_verified_plan"
                    if "schedule_execution" in a["result"]
                    else "piece_dispatch"
                    if "schedule_baseline" in a["result"]
                    else "scenario_dispatch"
                ),
                "candidate_mode": (
                    "enforced_verified_plan"
                    if "schedule_execution" in b["result"]
                    else "piece_dispatch"
                    if "schedule_baseline" in b["result"]
                    else "scenario_dispatch"
                ),
            },
            "delta": {k: b["result"]["metrics"][k] - v for k, v in a["result"]["metrics"].items()},
            "interpretation": (
                "Candidate minus baseline; descriptive simulated differences, "
                "not a significance claim."
            ),
        }

    def save_draft(
        self, name: str, facts: dict[str, Any], answers: list[dict[str, str]]
    ) -> dict[str, Any]:
        json.dumps(facts, allow_nan=False)
        if len(answers) > 1000:
            raise ValueError("A building brief supports at most 1000 retained answers")
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

    def save_model_draft(self, name: str, draft: LinearDraft) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "revision": 1,
            "name": name,
            "draft": draft.model_dump(mode="json"),
            "created_at": _now(),
            "status": "draft",
        }
        self.repository.create("model_drafts", record["id"], record)
        return record

    def compile_model_draft(self, draft_id: str) -> dict[str, Any]:
        record = self.repository.get("model_drafts", draft_id)
        return compile_draft(
            LinearDraft.model_validate(record["draft"]), draft_id, record["revision"]
        )

    def import_model_draft(self, draft_id: str) -> dict[str, Any]:
        record = self.repository.get("model_drafts", draft_id)
        compiled = self.compile_model_draft(draft_id)
        if compiled["status"] != "compiled":
            raise ValueError("Draft cannot be imported: " + json.dumps(compiled["issues"]))
        return self.import_scenario(
            json.dumps(compiled["capsule_preview"], allow_nan=False),
            record["name"],
            draft_link={
                "id": draft_id,
                "revision": record["revision"],
                "compiler_version": compiled["compiler_version"],
            },
        )

    def preview_schedule_upload(
        self,
        scenario_id: str,
        filename: str,
        content_base64: str,
        mapping: dict[str, str] | None,
    ) -> dict[str, Any]:
        scenario = self.repository.get("scenarios", scenario_id)
        if scenario["summary"]["domain"] != "manufacturing":
            raise ValueError("Schedule intake supports manufacturing scenarios only")
        as_of = scenario["capsule"]["snapshot"].get("as_of")
        try:
            if not isinstance(as_of, str) or datetime.fromisoformat(as_of).tzinfo is None:
                raise ValueError("timezone required")
        except ValueError as exc:
            raise ValueError("Scenario snapshot as_of requires an explicit timezone") from exc
        headers, rows, file_digest = decode_upload(filename, content_base64)
        products = {route["part"] for route in scenario["capsule"]["model"].get("routing", [])}
        validation = preview_mapping(headers, rows, mapping, products)
        return {
            "schema_version": "1.0",
            "scenario_id": scenario_id,
            "scenario_digest": scenario["digest"],
            "original_file_digest": file_digest,
            "headers": headers,
            "row_count": len(rows),
            "sample_rows": rows[:10],
            "time_unit": "second",
            "time_origin": "scenario_snapshot_as_of",
            "original_file_retention": "discarded_after_parse",
            **validation,
        }

    def commit_schedule_upload(
        self,
        scenario_id: str,
        filename: str,
        content_base64: str,
        original_file_digest: str,
        mapping: dict[str, str],
    ) -> dict[str, Any]:
        preview = self.preview_schedule_upload(scenario_id, filename, content_base64, mapping)
        if preview["original_file_digest"] != original_file_digest:
            raise ValueError("File digest differs from reviewed preview")
        if not preview["committable"]:
            raise ValueError(
                "Schedule mapping has unresolved row issues: " + json.dumps(preview["issues"])
            )
        scenario = self.repository.get("scenarios", scenario_id)
        snapshot = scenario["capsule"]["snapshot"]
        snapshot_digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "schema_version": "1.0",
            "scenario_id": scenario_id,
            "scenario_digest": scenario["digest"],
            "source_snapshot_id": snapshot.get("id"),
            "source_snapshot_digest": snapshot_digest,
            "original_file_digest": original_file_digest,
            "original_file_retention": "discarded_after_parse",
            "mapping": mapping,
            "time_unit": "second",
            "time_origin": "scenario_snapshot_as_of",
            "orders": preview["orders"],
            "created_at": _now(),
        }
        self.repository.create("schedule_snapshots", record["id"], record)
        return record

    def solve_schedule_snapshot(
        self, snapshot_id: str, backend: str, time_limit: float
    ) -> dict[str, Any]:
        """Translate the snapshot to a v2 request, solve, verify and keep the plan record.

        A scenario the translator refuses returns `status: blocked` with its named issues
        and keeps no record (COMP-013, COMP-014, REQ-083).
        """
        if backend not in ("baseline", "ortools") or not 0 < time_limit <= 30:
            raise ValueError("Choose baseline/ortools and a time limit from 0 to 30 seconds")
        schedule_snapshot = self.repository.get("schedule_snapshots", snapshot_id)
        scenario = self.repository.get("scenarios", schedule_snapshot["scenario_id"])
        translation = build_request(scenario, schedule_snapshot)
        if translation.request is None:
            return {
                "status": translation.status,
                "translator_version": translation.translator_version,
                "issues": [issue.model_dump(mode="json") for issue in translation.issues],
            }
        result = solve(
            translation.request,
            solver=get_solver("ortools") if backend == "ortools" else None,
            budget_seconds=time_limit,
        )
        plan_record_id = uuid.uuid4().hex
        blocker = self._simulation_blocker(scenario, schedule_snapshot, translation, result)
        record: dict[str, Any] = {
            "id": plan_record_id,
            **record_fields(translation.request, result),
            "scenario_id": scenario["id"],
            "scenario_digest": scenario["digest"],
            "schedule_snapshot_id": snapshot_id,
            "source_snapshot_digest": schedule_snapshot["source_snapshot_digest"],
            "translator_version": translation.translator_version,
            "problem_digest": translation.problem_digest,
            "backend": backend,
            "time_limit_seconds": time_limit,
            "time_limit_applied": backend == "ortools",
            "result": result.model_dump(mode="json"),
            "status_explanation": _status_explanation(result, backend),
            "simulation": {
                "status": "available" if blocker is None else "blocked",
                "submit_route": f"/api/workspace/schedule-plans/{plan_record_id}/simulate",
                "blocker": blocker,
                "meaning": (
                    "Enforces verified machine assignments and planned starts as earliest starts; "
                    "later work may slip if upstream execution is late."
                ),
            },
            "interpretation": (
                "Verified feasible deterministic schedule for the profile's objective; "
                "stochastic delivery confidence has not been simulated."
                if result.verified
                else "No independently verified feasible schedule is available."
            ),
            "created_at": _now(),
        }
        self.repository.create("schedule_plans", record["id"], record)
        return record

    def _simulation_blocker(
        self,
        scenario: dict[str, Any],
        snapshot: dict[str, Any],
        translation: Translation,
        result: SolveResult,
    ) -> str | None:
        """Why the DES cannot replay this plan, or None when it can (REQ-082).

        Runs the runner's own replay guards on the capsule the simulation would use, so
        the record carries the guard's message instead of a later failed job.
        """
        if not result.verified:
            return "A verified feasible plan is required"
        firings = firings_from_plan(translation, result)
        if max((f.planned_end for f in firings), default=0.0) > SIMULATION_HORIZON_SECONDS:
            return "Verified plan exceeds the one-year simulation horizon"
        capsule = ScenarioCapsule.from_dict(self._schedule_capsule(scenario, snapshot))
        compiled = capsule.compile()
        policy = capsule.experiment.get("dispatch_policy")
        runtime = (
            configured_runtime(policy, {spec.location_id for spec in compiled.model.locations})
            if policy is not None
            else None
        )
        try:
            RunDriver(compiled.model).check_execution_schedule(compiled.plan, firings, runtime)
        except ValueError as exc:
            return str(exc)
        return None

    def _plan_firings(
        self, plan_record: dict[str, Any], scenario: dict[str, Any], snapshot: dict[str, Any]
    ) -> list[ScheduledFiring]:
        """The plan's DES firings, refused when its source no longer translates the same."""
        translation = build_request(scenario, snapshot)
        if translation.problem_digest != plan_record["problem_digest"]:
            raise ValueError("Schedule source changed since the plan was solved")
        return firings_from_plan(translation, SolveResult.model_validate(plan_record["result"]))

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
            for sample in result.get("schedule_execution", {}).get("replications", []):
                for row in sample.get("trace", []):
                    evidence.append(
                        {
                            "replication": sample["replication"],
                            "kind": "schedule_operation",
                            "record": row,
                        }
                    )
        return {
            "job_id": job_id,
            "offset": offset,
            "limit": limit,
            "items": evidence[offset : offset + limit],
            "total": len(evidence),
        }
