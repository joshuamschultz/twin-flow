"""Integration tests for the twinflow.service REST API.

Exercises the real seam end to end: the fast discovery/floor/lever endpoints, and the
job lifecycle for a `run` (submit -> poll -> KPIs). Kept to tiny `reps` so the real
simulator runs quickly. Skips cleanly if the optional `api` extra (FastAPI) is absent.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from twinflow.service.app import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app("examples"))


def _await_job(client: TestClient, job_id: str, timeout: float = 120.0) -> dict[str, object]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["status"] != "running":
            return payload
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}


def test_modules_discovery_lists_every_kind(client: TestClient) -> None:
    payload = client.get("/api/modules").json()
    assert "on_time_pct" in payload["objectives"]
    assert "hill_climb" in payload["optimizers"]
    assert set(payload) == {"objectives", "costs", "optimizers", "models"}


def test_adapters_discovery_lists_surfaces(client: TestClient) -> None:
    payload = client.get("/api/adapters").json()
    assert "csv_plan" in payload["plan_sources"]
    assert set(payload["dispatch_policies"]) == {"fifo", "edd", "spt", "critical_ratio"}
    assert "poisson" in payload["demand_generators"]


def test_models_lists_examples(client: TestClient) -> None:
    names = {m["name"] for m in client.get("/api/models").json()}
    assert {"cnc-shop", "spring"} <= names


def test_floor_graph_shape(client: TestClient) -> None:
    floor = client.get("/api/models/cnc-shop/floor").json()
    assert floor["nodes"] and floor["edges"] and floor["parts"]
    kinds = {node["kind"] for node in floor["nodes"]}
    assert "location" in kinds


def test_levers_include_headcount(client: TestClient) -> None:
    paths = {lever["path"] for lever in client.get("/api/models/cnc-shop/levers").json()}
    assert any("headcount" in path for path in paths)


def test_model_name_traversal_is_rejected(client: TestClient) -> None:
    assert client.get("/api/models/nope/floor").status_code == 404


def test_run_job_returns_kpis_with_band(client: TestClient) -> None:
    submitted = client.post("/api/run", json={"model": "cnc-shop", "reps": 3}).json()
    assert submitted["status"] == "running"
    done = _await_job(client, submitted["id"])
    assert done["status"] == "done", done.get("error")
    result = done["result"]
    assert "on_time_pct" in result["kpis"]
    band = result["intervals"]["on_time_pct"]
    assert band["lo"] <= band["mean"] <= band["hi"]
    # orders/deliveries and inventory views are attached to every run
    assert "fill_rate" in result["fulfillment"]
    assert "average_level" in result["inventory"]


def test_optimize_job_returns_best_and_history(client: TestClient) -> None:
    body = {
        "model": "cnc-shop",
        "space": {"labor.pools[0].headcount": {"min": 2, "max": 4}},
        "objective": "on_time_pct",
        "optimizer": "grid",
        "budget": 3,
        "reps": 3,
    }
    submitted = client.post("/api/optimize", json=body).json()
    done = _await_job(client, submitted["id"])
    assert done["status"] == "done", done.get("error")
    result = done["result"]
    assert result["best"]["levers"]["labor.pools[0].headcount"] in (2, 3, 4)
    assert len(result["history"]) == result["evaluations_used"]
