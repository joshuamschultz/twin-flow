"""Small synchronous public client for the agent/operator application contract."""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class TwinflowError(Exception):
    """An application request failed, with its HTTP status retained for callers."""

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

    def request(self, path: str, body: dict[str, Any] | None = None) -> Any:
        """Call a workspace-relative application route."""
        if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("Invalid workspace route")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(  # noqa: S310 - scheme validated above
            self.base_url + "/api/workspace" + path,
            headers=headers,
            data=None if body is None else json.dumps(body, allow_nan=False).encode(),
        )
        try:
            # Scheme and server origin are validated configuration, not tool arguments.
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return json.load(response)
        except HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise TwinflowError(exc.code, payload) from exc

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
