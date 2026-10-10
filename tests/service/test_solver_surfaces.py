"""Every surface accepts any registered solver name (TF-003 T-119, COMP-028, REQ-088).

The surfaces (REST `/schedules` and the snapshot solve route, MCP `schedule_operations` and
`solve_schedule_snapshot`, the SDK, `twinflow schedule --solver` and the capability
payloads) resolve a solver by name through `twinflow.application.solver_port`, the one
adapter to TF-003's registry. These tests replace the registry with a fake by
`monkeypatch.setattr(solver_port, "registry", lambda: fake)`. The fake knows the real
`baseline` and a fake `fifo` (a thin wrapper around `BaselineSolver` that counts its calls,
family `dispatch`), so the tests pass on this branch and after the TF-003 merge.

Pinned contract of `solver_port`:

* `SolverDescription` frozen pydantic model {name, family, features (sorted `Feature`
  values), deterministic}.
* `available_solvers()` sorted by name; `build_solver(name)` a fresh instance.
* `UnsupportedSolver(name, registered)` is a `ValueError` with class attribute
  `code = "unsupported_solver"`, attribute `registered` and a message naming every
  registered name.
* both functions resolve through the module-level `registry()`.

Refusals: REST 422 `{"code": "unsupported_solver", "detail": ..., "registered": [...]}`;
MCP tool error code `unsupported_solver`; SDK `TwinflowError` whose text contains
`unsupported_solver`; CLI exit 2 with a stderr line naming the registered solvers.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from twinflow.application.decision_tools import DecisionTools
from twinflow.application.planning import PlanningService
from twinflow.application.workspace import Workspace
from twinflow.scheduling import BaselineSolver, Feature, ObjectiveSpec, Problem, Schedule
from twinflow.scheduling.baseline import CAPABILITIES as BASELINE_CAPABILITIES
from twinflow.scheduling.model import SolveResult
from twinflow.sdk import TwinflowClient, TwinflowError
from twinflow.service.app import create_app

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
API = "/api/workspace"
BASE_URL = "http://testserver"
REGISTERED = ["baseline", "fifo"]
FEATURES = tuple(sorted(feature.value for feature in BASELINE_CAPABILITIES))

ONE_OP_REQUEST: dict[str, Any] = {
    "schema_version": "2.0",
    "problem": {
        "id": "one-op",
        "jobs": [
            {
                "id": "J",
                "operations": [
                    {
                        "id": "cut",
                        "work_center_id": "machining",
                        "quantity": 2,
                        "options": [
                            {
                                "machine_id": "m1",
                                "fixed_seconds": 1.0,
                                "unit_seconds": 2.0,
                                "preference_cost": 0.0,
                            }
                        ],
                    }
                ],
            }
        ],
        "machines": [{"id": "m1", "work_center_id": "machining"}],
        "work_centers": [{"id": "machining"}],
    },
}


# --- the fake registry ---------------------------------------------------------------------------


class FifoSolver:
    """A fake registered solver: the baseline heuristic under another name, counting calls."""

    def __init__(self, calls: list[str]) -> None:
        self._inner = BaselineSolver()
        self._calls = calls

    @property
    def name(self) -> str:
        return "fifo"

    @property
    def capabilities(self) -> frozenset[Feature]:
        return BASELINE_CAPABILITIES

    def solve(
        self, problem: Problem, *, objective: ObjectiveSpec, budget_seconds: float
    ) -> Schedule:
        self._calls.append("fifo")
        schedule = self._inner.solve(problem, objective=objective, budget_seconds=budget_seconds)
        return schedule.model_copy(update={"solver": "fifo"})


class FakeRegistry:
    """`baseline` (real) plus `fifo` (fake); records every `get` and every fifo solve."""

    def __init__(self, port: Any) -> None:
        self.port = port
        self.gets: list[str] = []
        self.fifo_calls: list[str] = []
        self.fifo_instances: list[FifoSolver] = []

    def available(self) -> tuple[Any, ...]:
        describe = self.port.SolverDescription
        return (
            describe(name="baseline", family="constructive", features=FEATURES, deterministic=True),
            describe(name="fifo", family="dispatch", features=FEATURES, deterministic=True),
        )

    def get(self, name: str) -> Any:
        self.gets.append(name)
        if name == "baseline":
            return BaselineSolver()
        if name == "fifo":
            solver = FifoSolver(self.fifo_calls)
            self.fifo_instances.append(solver)
            return solver
        raise self.port.UnsupportedSolver(name, tuple(REGISTERED))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    from twinflow.application import solver_port

    registry = FakeRegistry(solver_port)
    monkeypatch.setattr(solver_port, "registry", lambda: registry)
    return registry


def _descriptions(registry: FakeRegistry) -> list[dict[str, Any]]:
    return [description.model_dump(mode="json") for description in registry.available()]


# --- shared surface helpers ----------------------------------------------------------------------


@contextmanager
def _rest(root: Path) -> Iterator[TestClient]:
    with TestClient(create_app(EXAMPLES, workspace_root=root), base_url=BASE_URL) as client:
        yield client


def _sdk(client: TestClient) -> TwinflowClient:
    """The real SDK, with its transport replaced by the in-process test client."""

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        response = client.request(method, url, headers=dict(headers), content=body)
        return response.status_code, response.content

    return TwinflowClient(BASE_URL, transport=transport)


def _capsule() -> dict[str, Any]:
    """A one-step manufacturing capsule whose snapshot the scheduler can translate."""
    model = {
        "defaults": {"cycle_time_cv": 0.0},
        "stocks": [],
        "part_types": [
            {"name": thing, "uom": "piece", "attributes": {}} for thing in ("blank", "finished")
        ],
        "machines": [{"name": "machine_cut"}],
        "labor": {"pools": [{"name": "operators", "headcount": 1, "skills": ["operator"]}]},
        "locations": [
            {
                "name": "cut",
                "consumes": [{"thing": "blank", "qty": 1, "uom": "piece"}],
                "emits": [{"thing": "finished", "qty": 1, "uom": "piece"}],
                "setup_key": "setup_cut",
                "time_model": {"kind": "rate_based", "rate": 0.25},
                "machine": "machine_cut",
                "capacity": 1,
                "labor_skill": "operator",
            }
        ],
        "routing": [{"part": "finished", "steps": ["cut"]}],
    }
    return {
        "schema_version": "0.1",
        "model": model,
        "snapshot": {
            "id": "solver-surface-snapshot",
            "as_of": "2026-10-08T00:00:00+00:00",
            "model_revision": "1",
            "production_plan": [
                {
                    "work_order_id": "seed",
                    "part": "finished",
                    "qty": 1,
                    "start_date": "0",
                    "due_date": "100",
                }
            ],
        },
        "experiment": {"id": "solver-surface", "replications": 1, "seed": 1},
        "assumptions": [],
        "provenance": {"source": "test"},
        "required_capabilities": [],
    }


_ORDERS_CSV = b"Order,Part,Qty,Release,Due\no-1,finished,2,0,1000\no-2,finished,1,5,1000\n"
_MAPPING = {
    "order_id": "Order",
    "product": "Part",
    "quantity": "Qty",
    "release_second": "Release",
    "due_second": "Due",
}


def _snapshot_over_rest(client: TestClient) -> str:
    imported = client.post(
        f"{API}/scenarios", json={"name": "Solvers", "content": json.dumps(_capsule())}
    )
    assert imported.status_code == 201, imported.text
    upload = {
        "scenario_id": imported.json()["id"],
        "filename": "orders.csv",
        "content_base64": base64.b64encode(_ORDERS_CSV).decode(),
        "mapping": _MAPPING,
        "time_unit": "second",
        "time_origin": "scenario_snapshot_as_of",
    }
    preview = client.post(f"{API}/schedule-uploads/preview", json=upload)
    assert preview.status_code == 200, preview.text
    committed = client.post(
        f"{API}/schedule-uploads/commit",
        json={**upload, "original_file_digest": preview.json()["original_file_digest"]},
    )
    assert committed.status_code == 201, committed.text
    return str(committed.json()["id"])


def _snapshot_in_workspace(workspace: Workspace) -> str:
    scenario = workspace.import_scenario(json.dumps(_capsule()), "Solvers")
    content = base64.b64encode(_ORDERS_CSV).decode()
    preview = workspace.preview_schedule_upload(scenario["id"], "orders.csv", content, _MAPPING)
    record = workspace.commit_schedule_upload(
        scenario["id"], "orders.csv", content, preview["original_file_digest"], _MAPPING
    )
    return str(record["id"])


def _assert_unsupported_body(response: Any) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "unsupported_solver", body
    assert body["registered"] == REGISTERED, body
    for name in REGISTERED:
        assert name in body["detail"], body


# --- solver_port itself --------------------------------------------------------------------------


def test_solver_port_available_solvers_is_the_registry_sorted_by_name(fake: FakeRegistry) -> None:
    from twinflow.application import solver_port

    listed = solver_port.available_solvers()

    assert [d.name for d in listed] == REGISTERED
    assert [d.model_dump(mode="json") for d in listed] == _descriptions(fake)


def test_solver_port_build_solver_returns_a_fresh_instance_per_call(fake: FakeRegistry) -> None:
    from twinflow.application import solver_port

    first, second = solver_port.build_solver("fifo"), solver_port.build_solver("fifo")

    assert first.name == second.name == "fifo"
    assert first is not second
    assert fake.gets == ["fifo", "fifo"]


def test_solver_port_unknown_name_raises_unsupported_solver_naming_registered(
    fake: FakeRegistry,
) -> None:
    from twinflow.application import solver_port

    with pytest.raises(solver_port.UnsupportedSolver) as raised:
        solver_port.build_solver("nosuch")

    error = raised.value
    assert isinstance(error, ValueError)
    assert error.code == "unsupported_solver"
    assert solver_port.UnsupportedSolver.code == "unsupported_solver"
    assert tuple(error.registered) == tuple(REGISTERED)
    for name in REGISTERED:
        assert name in str(error)


def test_solver_port_description_is_frozen() -> None:
    from pydantic import ValidationError

    from twinflow.application import solver_port

    description = solver_port.SolverDescription(
        name="fifo", family="dispatch", features=FEATURES, deterministic=True
    )

    with pytest.raises(ValidationError):
        description.name = "other"  # type: ignore[misc] -- asserting the model is frozen


def test_solver_port_default_registry_lists_baseline_and_ortools() -> None:
    """Without the fake: the built-in registry (this branch) or TF-003's facade (merged)."""
    from twinflow.application import solver_port

    listed = {d.name: d for d in solver_port.available_solvers()}

    assert {"baseline", "ortools"} <= set(listed)
    assert listed["baseline"].features == FEATURES
    assert list(listed) == sorted(listed)
    assert solver_port.build_solver("baseline").name == "baseline"


# --- REST ----------------------------------------------------------------------------------------


def test_rest_schedules_with_registered_fifo_returns_201_record(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _rest(tmp_path) as client:
        response = client.post(
            f"{API}/schedules",
            json={"request": ONE_OP_REQUEST, "backend": "fifo", "time_limit": 5},
        )

        assert response.status_code == 201, response.text
        record = response.json()
        assert record["backend"] == "fifo"
        result = SolveResult.model_validate(record["result"])
        assert result.verified, result.issues
        assert result.schedule.solver == "fifo"
        assert fake.fifo_calls == ["fifo"]
        assert [row["id"] for row in client.get(f"{API}/schedules").json()] == [record["id"]]


def test_rest_schedules_default_backend_resolves_baseline_through_the_registry(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _rest(tmp_path) as client:
        response = client.post(f"{API}/schedules", json={"request": ONE_OP_REQUEST})

    assert response.status_code == 201, response.text
    assert response.json()["backend"] == "baseline"
    assert "baseline" in fake.gets
    assert fake.fifo_calls == []


def test_rest_schedules_unknown_backend_is_422_unsupported_solver(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _rest(tmp_path) as client:
        response = client.post(
            f"{API}/schedules",
            json={"request": ONE_OP_REQUEST, "backend": "nosuch", "time_limit": 5},
        )

        _assert_unsupported_body(response)
        assert client.get(f"{API}/schedules").json() == []
    assert fake.fifo_calls == []


@pytest.mark.parametrize("time_limit", [0, 30.5, 1e9])
def test_rest_schedules_keeps_the_time_limit_cap_for_registered_solvers(
    fake: FakeRegistry, tmp_path: Path, time_limit: float
) -> None:
    with _rest(tmp_path) as client:
        response = client.post(
            f"{API}/schedules",
            json={"request": ONE_OP_REQUEST, "backend": "fifo", "time_limit": time_limit},
        )

    assert response.status_code == 422, response.text
    assert fake.fifo_calls == []


def test_rest_schedules_oversized_backend_name_is_refused_without_solving(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _rest(tmp_path) as client:
        response = client.post(
            f"{API}/schedules",
            json={"request": ONE_OP_REQUEST, "backend": "f" * 10_000, "time_limit": 5},
        )

        assert response.status_code == 422, response.text
        assert client.get(f"{API}/schedules").json() == []
    assert fake.fifo_calls == []


def test_rest_snapshot_solve_with_registered_fifo_returns_a_plan(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _rest(tmp_path) as client:
        snapshot_id = _snapshot_over_rest(client)

        response = client.post(
            f"{API}/schedule-snapshots/{snapshot_id}/solve",
            json={"backend": "fifo", "time_limit": 5},
        )

    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["backend"] == "fifo"
    result = SolveResult.model_validate(plan["result"])
    assert result.verified, result.issues
    assert result.schedule.solver == "fifo"
    assert fake.fifo_calls == ["fifo"]


def test_rest_snapshot_solve_unknown_backend_is_422_unsupported_solver(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _rest(tmp_path) as client:
        snapshot_id = _snapshot_over_rest(client)

        response = client.post(
            f"{API}/schedule-snapshots/{snapshot_id}/solve",
            json={"backend": "nosuch", "time_limit": 5},
        )

    _assert_unsupported_body(response)
    assert fake.fifo_calls == []


# --- capabilities ------------------------------------------------------------------------------


def test_decision_contracts_list_registered_solvers_and_drop_backends(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    workspace = Workspace(tmp_path, EXAMPLES)
    try:
        scheduling = DecisionTools(workspace).contracts()["scheduling"]
    finally:
        workspace.close()

    assert scheduling["solvers"] == _descriptions(fake)
    assert "backends" not in scheduling


def test_rest_tool_contracts_list_registered_solvers(fake: FakeRegistry, tmp_path: Path) -> None:
    with _rest(tmp_path) as client:
        response = client.get(f"{API}/tool-contracts")

    assert response.status_code == 200, response.text
    assert response.json()["scheduling"]["solvers"] == _descriptions(fake)
    assert "backends" not in response.json()["scheduling"]


# --- SDK -----------------------------------------------------------------------------------------


def test_sdk_schedule_with_registered_fifo_returns_the_record(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _rest(tmp_path) as client:
        record = _sdk(client).schedule(ONE_OP_REQUEST, backend="fifo", time_limit=5)

    assert record["backend"] == "fifo"
    assert record["verified"] is True
    assert fake.fifo_calls == ["fifo"]


def test_sdk_schedule_unknown_backend_raises_unsupported_solver(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _rest(tmp_path) as client, pytest.raises(TwinflowError) as raised:
        _sdk(client).schedule(ONE_OP_REQUEST, backend="nosuch", time_limit=5)

    assert raised.value.status == 422
    assert "unsupported_solver" in str(raised.value)


def test_sdk_solve_schedule_snapshot_with_fifo_and_unknown(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _rest(tmp_path) as client:
        sdk = _sdk(client)
        snapshot_id = _snapshot_over_rest(client)

        plan = sdk.solve_schedule_snapshot(snapshot_id, backend="fifo", time_limit=5)
        with pytest.raises(TwinflowError) as raised:
            sdk.solve_schedule_snapshot(snapshot_id, backend="nosuch", time_limit=5)

    assert plan["backend"] == "fifo"
    assert fake.fifo_calls == ["fifo"]
    assert raised.value.status == 422
    assert "unsupported_solver" in str(raised.value)


# --- MCP (in process) ----------------------------------------------------------------------------


@contextmanager
def _mcp_server(root: Path) -> Iterator[tuple[Any, Workspace]]:
    pytest.importorskip("mcp")
    from twinflow.mcp.spec import ToolServices
    from twinflow.mcp.tools import build_server

    workspace = Workspace(root, EXAMPLES)
    planning = PlanningService(workspace, portfolio=None)
    services = ToolServices(
        workspace=workspace,
        decisions=DecisionTools(workspace),
        planning=planning,
        artifacts=None,
        models_root=None,
        local_mode=True,
    )
    try:
        yield build_server(services), workspace
    finally:
        planning.close()
        workspace.close()


def _run_mcp(server: Any, calls: list[tuple[str, dict[str, Any]]]) -> tuple[dict[str, Any], list]:
    """List the tools, then make each call; returns (tools by name, call results)."""
    import mcp

    async def main() -> tuple[dict[str, Any], list]:
        async with mcp.Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            results = [await client.call_tool(name, arguments) for name, arguments in calls]
            return tools, results

    return asyncio.run(main())


def _error_payload(result: Any) -> dict[str, Any]:
    """The tool-error body: structuredContent, else the first text block as JSON."""
    from mcp import types

    assert result.is_error, result
    if result.structured_content is not None:
        return dict(result.structured_content)
    texts = [block.text for block in result.content if isinstance(block, types.TextContent)]
    assert texts, result
    payload = json.loads(texts[0])
    assert isinstance(payload, dict), payload
    return payload


def _assert_unsupported_tool_error(result: Any) -> None:
    payload = _error_payload(result)
    assert payload["code"] == "unsupported_solver", payload
    for name in REGISTERED:
        assert name in payload["message"], payload


@pytest.mark.parametrize("tool", ["schedule_operations", "solve_schedule_snapshot"])
def test_mcp_backend_input_is_a_bounded_string_without_enum(
    fake: FakeRegistry, tmp_path: Path, tool: str
) -> None:
    with _mcp_server(tmp_path) as (server, _):
        tools, _ = _run_mcp(server, [])

    backend = tools[tool].input_schema["properties"]["backend"]
    assert "enum" not in backend, backend
    assert backend.get("type") == "string", backend
    assert isinstance(backend.get("maxLength"), int), backend


def test_mcp_schedule_operations_with_registered_fifo_succeeds(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    arguments = {"request": ONE_OP_REQUEST, "backend": "fifo", "time_limit": 5, "request_key": "f"}
    with _mcp_server(tmp_path) as (server, workspace):
        _, (result,) = _run_mcp(server, [("schedule_operations", arguments)])
        stored = workspace.repository.list("schedules")

    assert not result.is_error, result
    assert result.structured_content["backend"] == "fifo"
    assert [row["backend"] for row in stored] == ["fifo"]
    assert fake.fifo_calls == ["fifo"]


def test_mcp_schedule_operations_unknown_backend_is_unsupported_solver(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    arguments = {"request": ONE_OP_REQUEST, "backend": "nosuch", "request_key": "x"}
    with _mcp_server(tmp_path) as (server, workspace):
        _, (result,) = _run_mcp(server, [("schedule_operations", arguments)])
        stored = workspace.repository.list("schedules")

    _assert_unsupported_tool_error(result)
    assert stored == []
    assert fake.fifo_calls == []


def test_mcp_solve_schedule_snapshot_with_fifo_and_unknown(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _mcp_server(tmp_path) as (server, workspace):
        snapshot_id = _snapshot_in_workspace(workspace)
        good = {"snapshot_id": snapshot_id, "request_key": "ok", "backend": "fifo"}
        bad = {"snapshot_id": snapshot_id, "request_key": "bad", "backend": "nosuch"}
        _, (solved, refused) = _run_mcp(
            server, [("solve_schedule_snapshot", good), ("solve_schedule_snapshot", bad)]
        )

    assert not solved.is_error, solved
    assert solved.structured_content["backend"] == "fifo"
    assert fake.fifo_calls == ["fifo"]
    _assert_unsupported_tool_error(refused)


def test_mcp_describe_capabilities_lists_registered_solvers(
    fake: FakeRegistry, tmp_path: Path
) -> None:
    with _mcp_server(tmp_path) as (server, _):
        _, (result,) = _run_mcp(server, [("describe_capabilities", {})])

    assert not result.is_error, result
    assert result.structured_content["solvers"] == _descriptions(fake)


# --- CLI -----------------------------------------------------------------------------------------


def _cli(argv: list[str]) -> int:
    from twinflow.cli.main import main

    try:
        return main(argv)
    except SystemExit as exc:  # argparse refusals exit through SystemExit
        return int(exc.code or 0)


def _request_file(tmp_path: Path) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(ONE_OP_REQUEST), encoding="utf-8")
    return path


def test_cli_schedule_with_registered_fifo_prints_a_verified_result(
    fake: FakeRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _cli(["schedule", str(_request_file(tmp_path)), "--solver", "fifo", "--time-limit", "5"])

    captured = capsys.readouterr()
    assert code == 0, captured.err
    result = SolveResult.model_validate_json(captured.out)
    assert result.verified, result.issues
    assert result.schedule.solver == "fifo"
    assert fake.fifo_calls == ["fifo"]


def test_cli_schedule_unknown_solver_exits_two_naming_registered(
    fake: FakeRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _cli(["schedule", str(_request_file(tmp_path)), "--solver", "nosuch"])

    captured = capsys.readouterr()
    assert code == 2, captured
    assert "Traceback" not in captured.err
    lines = [line for line in captured.err.splitlines() if all(n in line for n in REGISTERED)]
    assert lines, captured.err
    assert captured.out == ""
    assert fake.fifo_calls == []
