"""Small synchronous public client for the agent/operator application contract.

Stdlib only: requests go through urllib unless the host passes its own `transport`, a
callable `(method, url, headers, body) -> (status, body_bytes)` (for tests or a host HTTP
stack). The planning methods are named like the MCP tools and send the tools' own input
models (REQ-180).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

WORKSPACE_PREFIX = "/api/workspace"

# (method, url, headers, body) -> (HTTP status, response body bytes)
Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, bytes]]


class TwinflowError(Exception):
    """An application request failed, with its HTTP status retained for callers.

    `str(error)` is the service's response body (a JSON error object for refusals).
    """

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        super().__init__(detail)


class TwinflowClient:
    """Connect to a configured Twinflow service; no model-provider dependency.

    The base URL and optional token come from the host application, never from
    imported scenario content. Long operations return job IDs for explicit polling.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        token: str | None = None,
        timeout: float = 30,
        transport: Transport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("A valid http(s) service URL is required")
        if parsed.username or parsed.password:
            raise ValueError("Credentials must be supplied separately")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._transport: Transport = transport or self._urllib_transport

    def request(
        self, path: str, body: dict[str, Any] | None = None, *, method: str | None = None
    ) -> Any:
        """Call a workspace-relative application route (GET without a body, else POST).

        `path` never carries a query string; methods that need one build it themselves.
        """
        _check_route(path)
        return self._send(method or ("GET" if body is None else "POST"), path, body)

    def _send(self, method: str, path_and_query: str, body: dict[str, Any] | None) -> Any:
        """Send one request through the transport; a status of 400 or more raises."""
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, allow_nan=False).encode()
        url = self.base_url + WORKSPACE_PREFIX + path_and_query
        status, payload = self._transport(method, url, headers, data)
        if status >= 400:
            raise TwinflowError(status, payload.decode("utf-8", errors="replace"))
        return json.loads(payload)

    def _urllib_transport(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        """The default transport: urllib, with the client's timeout."""
        request = Request(url, headers=dict(headers), data=body, method=method)  # noqa: S310 - scheme validated in __init__
        try:
            # Scheme and server origin are validated configuration, not tool arguments.
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return int(response.status), response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def capabilities(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("/capabilities"))

    def schema(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("/schema"))

    def scenarios(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.request("/scenarios"))

    def import_scenario(self, name: str, content: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("/scenarios", {"name": name, "content": content}))

    def load_example(self, name: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("/examples/load", {"example_id": name}))

    def branch(self, scenario_id: str, name: str, content: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(
                f"/scenarios/{quote(scenario_id, safe='')}/branch",
                {"name": name, "content": content},
            ),
        )

    def evaluate(
        self, scenario_id: str, request_key: str, *, reps: int = 10, seed: int = 42
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(
                f"/scenarios/{quote(scenario_id, safe='')}/evaluate",
                {"request_key": request_key, "reps": reps, "seed": seed},
            ),
        )

    def job(self, job_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.request(f"/jobs/{quote(job_id, safe='')}"))

    def compare(self, baseline_id: str, candidate_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request("/compare", {"baseline_id": baseline_id, "candidate_id": candidate_id}),
        )

    def validate(self, content: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("/validate", {"content": content}))

    def save_model_draft(self, name: str, draft: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("/model-drafts", {"name": name, "draft": draft}))

    def compile_model_draft(self, draft_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(f"/model-drafts/{quote(draft_id, safe='')}/compile", {}),
        )

    def import_model_draft(self, draft_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(f"/model-drafts/{quote(draft_id, safe='')}/import", {}),
        )

    def preview_schedule_upload(
        self,
        scenario_id: str,
        filename: str,
        content_base64: str,
        mapping: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(
                "/schedule-uploads/preview",
                {
                    "scenario_id": scenario_id,
                    "filename": filename,
                    "content_base64": content_base64,
                    "mapping": mapping,
                    "time_unit": "second",
                    "time_origin": "scenario_snapshot_as_of",
                },
            ),
        )

    def commit_schedule_upload(
        self,
        scenario_id: str,
        filename: str,
        content_base64: str,
        original_file_digest: str,
        mapping: dict[str, str],
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(
                "/schedule-uploads/commit",
                {
                    "scenario_id": scenario_id,
                    "filename": filename,
                    "content_base64": content_base64,
                    "original_file_digest": original_file_digest,
                    "mapping": mapping,
                    "time_unit": "second",
                    "time_origin": "scenario_snapshot_as_of",
                },
            ),
        )

    def solve_schedule_snapshot(
        self, snapshot_id: str, *, backend: str = "baseline", time_limit: float = 10
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(
                f"/schedule-snapshots/{quote(snapshot_id, safe='')}/solve",
                {"backend": backend, "time_limit": time_limit},
            ),
        )

    def simulate_schedule_baseline(
        self, snapshot_id: str, request_key: str, *, reps: int = 10, seed: int = 42
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(
                f"/schedule-snapshots/{quote(snapshot_id, safe='')}/simulate-baseline",
                {"request_key": request_key, "reps": reps, "seed": seed},
            ),
        )

    def query(
        self,
        job_id: str,
        topic: str,
        entity_id: str | None = None,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(
                f"/jobs/{quote(job_id, safe='')}/query", {"topic": topic, "entity_id": entity_id}
            ),
        )

    def save_brief(self, name: str, facts: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("/drafts", {"name": name, "facts": facts}))

    def answer_brief(self, draft_id: str, answers: list[dict[str, str]]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(f"/drafts/{quote(draft_id, safe='')}/answers", {"answers": answers}),
        )

    def ingest_events(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("/data/events", {"records": records}))

    def build_snapshot(
        self, known_at: str, freshness_rules: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.request(
                "/data/snapshots", {"known_at": known_at, "freshness_rules": freshness_rules or []}
            ),
        )

    def schedule(
        self, request: dict[str, Any], *, backend: str = "baseline", time_limit: float = 10
    ) -> dict[str, Any]:
        """Solve and verify a v2 `SolveRequest` (schema_version 2.0); returns the record."""
        return cast(
            dict[str, Any],
            self.request(
                "/schedules", {"request": request, "backend": backend, "time_limit": time_limit}
            ),
        )

    def propose(self, job_id: str, action: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.request("/proposals", {"job_id": job_id, "action": action})
        )

    # --- planning (the MCP tools' names and input models) -----------------------------------

    def validate_snapshot(
        self, snapshot: dict[str, Any], profile_digest: str | None = None
    ) -> dict[str, Any]:
        """Validate an ERP snapshot against the active (or named) planning profile."""
        body = {"snapshot": snapshot, "profile_digest": profile_digest}
        return cast(dict[str, Any], self.request("/erp/validate", body))

    def build_problem_from_erp(
        self, snapshot: dict[str, Any], profile_digest: str | None = None
    ) -> dict[str, Any]:
        """Build (or return the stored) scheduling problem from an ERP snapshot."""
        body = {"snapshot": snapshot, "profile_digest": profile_digest}
        return cast(dict[str, Any], self.request("/erp/problems", body))

    def solve_portfolio(
        self,
        problem_id: str,
        *,
        request_key: str,
        time_budget_seconds: float | None = None,
        portfolio: list[str] | None = None,
        seed: int = 0,
    ) -> dict[str, Any]:
        """Solve the portfolio of plans for a built problem (idempotent per `request_key`)."""
        body = {
            "problem_id": problem_id,
            "request_key": request_key,
            "time_budget_seconds": time_budget_seconds,
            "portfolio": portfolio,
            "seed": seed,
        }
        return cast(dict[str, Any], self.request("/plans/portfolio", body))

    def resolve_with_overrides(
        self,
        plan_id: str,
        *,
        overrides: list[dict[str, Any]],
        request_key: str,
        time_budget_seconds: float | None = None,
        seed: int = 0,
    ) -> dict[str, Any]:
        """Re-solve a plan's problem with planner overrides."""
        body = {
            "plan_id": plan_id,
            "overrides": overrides,
            "request_key": request_key,
            "time_budget_seconds": time_budget_seconds,
            "seed": seed,
        }
        return cast(dict[str, Any], self.request(f"{_plan_path(plan_id)}/resolve", body))

    def insert_rush_order(
        self,
        plan_id: str,
        *,
        rush: dict[str, Any],
        request_key: str,
        time_budget_seconds: float | None = None,
        seed: int = 0,
    ) -> dict[str, Any]:
        """Insert a rush order into a plan and re-solve."""
        body = {
            "plan_id": plan_id,
            "rush": rush,
            "request_key": request_key,
            "time_budget_seconds": time_budget_seconds,
            "seed": seed,
        }
        return cast(dict[str, Any], self.request(f"{_plan_path(plan_id)}/rush", body))

    def get_plan(
        self, plan_id: str, offset: int = 0, limit: int = 100, job_id: str | None = None
    ) -> dict[str, Any]:
        """One page of a plan's assignments; builds its own query string."""
        query: dict[str, str | int] = {"offset": offset, "limit": limit}
        if job_id is not None:
            query["job_id"] = job_id
        return cast(
            dict[str, Any], self._send("GET", f"{_plan_path(plan_id)}?{urlencode(query)}", None)
        )

    def get_pareto_menu(self, portfolio_id: str) -> dict[str, Any]:
        """The Pareto menu of a solved portfolio."""
        return cast(dict[str, Any], self.request(f"/portfolios/{_segment(portfolio_id)}/menu"))

    def explain_plan(self, plan_id: str, job_ids: list[str] | None = None) -> dict[str, Any]:
        """Explain a plan, optionally for some jobs only."""
        body = {"plan_id": plan_id, "job_ids": job_ids}
        return cast(dict[str, Any], self.request(f"{_plan_path(plan_id)}/explain", body))

    def compare_plans(self, plan_ids: list[str]) -> dict[str, Any]:
        """Compare plans of one problem side by side."""
        return cast(dict[str, Any], self.request("/plans/compare", {"plan_ids": plan_ids}))

    def simulate_plan(
        self,
        plan_id: str,
        *,
        request_key: str,
        reps: int = 10,
        seed: int = 42,
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        """Simulate a plan (idempotent per `request_key`)."""
        body = {
            "plan_id": plan_id,
            "request_key": request_key,
            "reps": reps,
            "seed": seed,
            "wait_seconds": wait_seconds,
        }
        return cast(dict[str, Any], self.request(f"{_plan_path(plan_id)}/simulate", body))

    def get_planning_profile(self) -> dict[str, Any]:
        """The active signed planning profile."""
        return cast(dict[str, Any], self.request("/planning-profile"))

    def put_planning_profile(
        self, *, profile_toml: str | None = None, profile: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Store, sign and activate a planning profile (exactly one of TOML or typed)."""
        body = {"profile_toml": profile_toml, "profile": profile}
        return cast(dict[str, Any], self.request("/planning-profile", body, method="PUT"))

    def get_k_table(self) -> dict[str, Any]:
        """The active signed K-table."""
        return cast(dict[str, Any], self.request("/k-table"))

    def put_k_table(self, table: dict[str, Any]) -> dict[str, Any]:
        """Store, sign and activate a K-table."""
        return cast(dict[str, Any], self.request("/k-table", {"table": table}, method="PUT"))


def _check_route(path: str) -> None:
    """Refuse a route that is not workspace-relative or carries a query or fragment."""
    if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
        raise ValueError("Invalid workspace route")


def _segment(value: str) -> str:
    """`value` quoted into one path segment; `:` (in plan ids) is a legal segment character."""
    return quote(value, safe=":")


def _plan_path(plan_id: str) -> str:
    """`/plans/<id>` with the id quoted into one path segment."""
    return f"/plans/{_segment(plan_id)}"
