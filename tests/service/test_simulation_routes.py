"""REST routes and SDK methods for the simulation tools (T-188, COMP-140; TF-005 COMP-123).

Requirements: REQ-140 (REST serves `simulate_plan`, `simulate_portfolio` and
`compare_plans` with `simulation_ids` through the same guard and models as MCP; the old
scenario-only replay route is deleted), REQ-143 (named errors). Decision: D-105.

Contract pinned here:

* Routes under the existing planning router prefix `/api/workspace` (TF-005 COMP-123):
  `POST /plans/{plan_id}/simulate` [simulate_plan], `POST /portfolios/{portfolio_id}/simulate`
  [simulate_portfolio], `POST /plans/compare` [compare_plans, now with `simulation_ids`].
  OpenAPI parity with the tool models is pinned in `test_planning_routes.py` (ROUTES).
* `POST /api/workspace/schedule-plans/{plan_id}/simulate` is gone: no OpenAPI path, and
  the router answers its own 404 (`{"detail": "Not Found"}`), not a handler's.
* `STATUS_BY_CODE` (service/planning.py) maps the TF-004 codes like their nearest existing
  kin: `plan_unverified` and `schedule_source_changed` as `blocked` (422),
  `incomparable_simulations` as `incomparable` (422), `budget_exhausted` as `timeout` (504),
  `capability_unavailable` 503; `simulation_link_pending` is removed.
* SDK: `simulate_plan(plan_id, *, request_key, reps=None, seed=42, time_budget_seconds=None,
  baseline=False, profile_digest=None)`, `simulate_portfolio(portfolio_id, *, request_key,
  reps=None, seed=42, time_budget_seconds=None, baseline=True, profile_digest=None)` and
  `compare_plans(plan_ids, simulation_ids=None)`; each body validates as the tool's input
  model.
"""

# ruff: noqa: E402 -- the api and agent extras are optional: skip before importing them.

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import json
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.testclient import TestClient

from twinflow.mcp.catalogue import SPECS
from twinflow.sdk import TwinflowClient
from twinflow.service.app import create_app
from twinflow.service.auth import add_token
from twinflow.service.planning import STATUS_BY_CODE
from twinflow.service.settings import ServiceSettings
from twinflow.service.signing import generate_signing_key

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
for _tests_dir in ("planning", "erp"):  # the TF-003 double and the small_shop generator
    _path = str(ROOT / "tests" / _tests_dir)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _fake_portfolio import FakePortfolioApi, as_portfolio_api
from fixtures.generate import small_shop

API = "/api/workspace"
BASE_URL = "http://testserver"
PROFILE_TOML = '[erp.class_weights]\n"A-KEY" = 4\n"B-CORE" = 2\n"C-SPOT" = 1\n'
OLD_ROUTE = API + "/schedule-plans/{plan_id}/simulate"


# --- the error map ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("plan_unverified", 422),
        ("schedule_source_changed", 422),
        ("incomparable_simulations", 422),
        ("budget_exhausted", 504),
        ("capability_unavailable", 503),
    ],
)
def test_status_by_code_maps_each_simulation_error(code: str, status: int) -> None:
    assert STATUS_BY_CODE.get(code) == status


def test_status_by_code_no_longer_knows_simulation_link_pending() -> None:
    assert "simulation_link_pending" not in STATUS_BY_CODE


def test_simulation_codes_follow_their_existing_kin() -> None:
    assert STATUS_BY_CODE["plan_unverified"] == STATUS_BY_CODE["blocked"]
    assert STATUS_BY_CODE["incomparable_simulations"] == STATUS_BY_CODE["incomparable"]
    assert STATUS_BY_CODE["budget_exhausted"] == STATUS_BY_CODE["timeout"]


# --- the service under test ---------------------------------------------------------------------


@dataclass
class Rest:
    """A running app, its tokens and workspace, plus seeded planning ids."""

    client: TestClient
    tokens: dict[str, str]
    workspace: Path
    ids: dict[str, Any] = field(default_factory=dict)


def _secrets(base: Path) -> tuple[Path, Path, dict[str, str]]:
    secrets = base / "secrets"
    secrets.mkdir(mode=0o700)
    tokens_file = secrets / "tokens.json"
    tokens = {
        "reader": add_token(tokens_file, caller_id="reader", scopes=["read"], days=None),
        "planner": add_token(
            tokens_file, caller_id="planner", scopes=["read", "plan", "admin"], days=None
        ),
    }
    key_file = secrets / "signing-key.pem"
    generate_signing_key(key_file)
    return tokens_file, key_file, tokens


def _call(
    rest: Rest, method: str, path: str, body: dict[str, Any] | None = None, who: str = "planner"
) -> Any:
    headers = {"Authorization": f"Bearer {rest.tokens[who]}"}
    return rest.client.request(method.upper(), API + path, json=body, headers=headers)


def _ok(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, (response.status_code, response.text[:800])
    payload: dict[str, Any] = response.json()
    return payload


def _seed(rest: Rest) -> None:
    _ok(_call(rest, "put", "/planning-profile", {"profile_toml": PROFILE_TOML}))
    built = _ok(_call(rest, "post", "/erp/problems", {"snapshot": _shop()}))
    assert built["status"] == "ready", built
    portfolio = _ok(
        _call(
            rest,
            "post",
            "/plans/portfolio",
            {"problem_id": built["problem_id"], "request_key": "rest-seed-solve"},
        )
    )
    # Only verified plans simulate (D-087); the fake's delayed candidate does not verify on
    # small_shop, so a second solve of the same problem supplies the second verified plan.
    again = _ok(
        _call(
            rest,
            "post",
            "/plans/portfolio",
            {"problem_id": built["problem_id"], "request_key": "rest-seed-solve-2", "seed": 1},
        )
    )
    verified = [
        row["plan_id"]
        for record in (portfolio, again)
        for row in record["statuses"]
        if row["verified"]
    ]
    assert len(verified) >= 2, f"need two verified plans of one problem, got {verified}"
    rest.ids.update(portfolio=portfolio["portfolio_id"], plans=verified)


@pytest.fixture(scope="module")
def rest(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Rest]:
    base = tmp_path_factory.mktemp("simulation-routes")
    tokens_file, key_file, tokens = _secrets(base)
    settings = ServiceSettings(
        tokens_file=tokens_file, signing_key_file=key_file, calls_per_minute=10_000
    )
    workspace = base / "workspace"
    app = create_app(
        EXAMPLES,
        workspace_root=workspace,
        settings=settings,
        portfolio_api=as_portfolio_api(FakePortfolioApi()),
    )
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        opened = Rest(client=client, tokens=tokens, workspace=workspace)
        _seed(opened)
        yield opened


def _shop() -> dict[str, Any]:
    return small_shop().model_dump(mode="json")


def _records(workspace: Path, kind: str) -> int:
    uri = f"file:{workspace / 'workspace.sqlite3'}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute("SELECT COUNT(*) FROM records WHERE kind = ?", (kind,)).fetchone()
    return int(row[0])


def _valid(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    SPECS[tool].output_model.model_validate(payload)
    return payload


# --- the old route is deleted (COMP-140) ---------------------------------------------------------


def test_old_schedule_plan_simulate_route_is_not_in_openapi(rest: Rest) -> None:
    paths = rest.client.app.openapi()["paths"]  # type: ignore[attr-defined]  # FastAPI app

    assert OLD_ROUTE not in paths
    assert not [
        path
        for path in paths
        if path.startswith(API + "/schedule-plans/") and path.endswith("/simulate")
    ]


def test_old_schedule_plan_simulate_route_is_not_routed(rest: Rest) -> None:
    body = {"reps": 1, "seed": 42, "request_key": "old-route"}

    response = _call(rest, "post", "/schedule-plans/any-plan/simulate", body)

    assert response.status_code in (404, 405), response.text[:300]
    assert response.json() in ({"detail": "Not Found"}, {"detail": "Method Not Allowed"})


# --- REST simulate routes (REQ-140) ----------------------------------------------------------


def test_post_plan_simulate_runs_simulate_plan_and_returns_its_output_model(rest: Rest) -> None:
    plan_id = rest.ids["plans"][0]
    before = _records(rest.workspace, "simulations")

    result = _valid(
        "simulate_plan",
        _ok(
            _call(
                rest,
                "post",
                f"/plans/{plan_id}/simulate",
                {"plan_id": plan_id, "request_key": "rest-sim-1"},
            )
        ),
    )

    assert result["status"] in ("completed", "truncated")
    assert result["plan_id"] == plan_id
    assert result["simulation_id"]
    assert result["evidence_kind"] == "simulation"
    assert _records(rest.workspace, "simulations") == before + 1


def test_post_portfolio_simulate_runs_simulate_portfolio(rest: Rest) -> None:
    portfolio_id = rest.ids["portfolio"]

    result = _valid(
        "simulate_portfolio",
        _ok(
            _call(
                rest,
                "post",
                f"/portfolios/{portfolio_id}/simulate",
                {"portfolio_id": portfolio_id, "request_key": "rest-sim-pf"},
            )
        ),
    )

    assert result["portfolio_id"] == portfolio_id
    assert result["status"] in ("completed", "truncated")
    assert result["evaluation_id"]
    assert result["evidence_kind"] == "portfolio_simulation"
    assert _records(rest.workspace, "evaluations") >= 1


def test_portfolio_simulate_path_id_differing_from_body_is_400_and_stores_nothing(
    rest: Rest,
) -> None:
    before = _records(rest.workspace, "simulations")

    response = _call(
        rest,
        "post",
        f"/portfolios/{rest.ids['portfolio']}/simulate",
        {"portfolio_id": "pf-other", "request_key": "rest-mismatch"},
    )

    assert response.status_code == 400, response.text[:300]
    assert response.json()["code"] == "id_mismatch"
    assert _records(rest.workspace, "simulations") == before


def test_post_plans_compare_takes_simulation_ids(rest: Rest) -> None:
    first, second = rest.ids["plans"][:2]
    sims = [
        _ok(
            _call(
                rest,
                "post",
                f"/plans/{plan_id}/simulate",
                {"plan_id": plan_id, "request_key": f"rest-cmp-{index}"},
            )
        )["simulation_id"]
        for index, plan_id in enumerate((first, second))
    ]

    compared = _valid(
        "compare_plans",
        _ok(
            _call(
                rest,
                "post",
                "/plans/compare",
                {"plan_ids": [first, second], "simulation_ids": sims},
            )
        ),
    )

    assert compared["simulation"]["reference_plan_id"] == first
    assert sorted(compared["simulation"]["ranking"]) == sorted([first, second])


def test_rest_simulate_with_wait_seconds_is_422_invalid_input(rest: Rest) -> None:
    plan_id = rest.ids["plans"][0]
    accepted = _call(
        rest,
        "post",
        f"/plans/{plan_id}/simulate",
        {"plan_id": plan_id, "request_key": "rest-budget", "time_budget_seconds": 5.0},
    )

    refused = _call(
        rest,
        "post",
        f"/plans/{plan_id}/simulate",
        {"plan_id": plan_id, "request_key": "rest-wait", "wait_seconds": 5},
    )

    assert accepted.status_code == 200, accepted.text[:300]  # the new budget field is accepted
    assert refused.status_code == 422, refused.text[:300]
    assert refused.json()["code"] == "invalid_input"


def test_read_token_on_portfolio_simulate_is_403_forbidden(rest: Rest) -> None:
    portfolio_id = rest.ids["portfolio"]

    response = _call(
        rest,
        "post",
        f"/portfolios/{portfolio_id}/simulate",
        {"portfolio_id": portfolio_id, "request_key": "rest-reader"},
        who="reader",
    )

    assert response.status_code == 403, response.text[:300]
    assert response.json()["code"] == "forbidden"


# --- SDK methods post the tool input models to the routes ----------------------------------------


@dataclass
class _Sent:
    """Every request the SDK handed to the recording transport."""

    requests: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)


def _recording_client(sent: _Sent) -> TwinflowClient:
    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        sent.requests.append((method, url, json.loads(body) if body is not None else None))
        return 200, b"{}"

    return TwinflowClient(BASE_URL, token=None, transport=transport)


def _only(sent: _Sent) -> tuple[str, str, dict[str, Any]]:
    assert len(sent.requests) == 1, sent.requests
    method, url, body = sent.requests[0]
    assert body is not None
    return method, urlsplit(url).path, body


def test_sdk_simulate_plan_posts_the_simulate_input_to_the_plan_route() -> None:
    sent = _Sent()

    _recording_client(sent).simulate_plan(
        "plan:1",
        request_key="k",
        reps=30,
        seed=7,
        time_budget_seconds=12.5,
        baseline=True,
        profile_digest=None,
    )

    method, path, body = _only(sent)
    assert (method, path) == ("POST", f"{API}/plans/plan:1/simulate")
    assert "wait_seconds" not in body
    parsed = SPECS["simulate_plan"].input_model.model_validate(body)
    assert parsed.model_dump(include={"plan_id", "reps", "seed", "baseline"}) == {
        "plan_id": "plan:1",
        "reps": 30,
        "seed": 7,
        "baseline": True,
    }


def test_sdk_simulate_plan_defaults_validate_as_the_tool_input() -> None:
    sent = _Sent()

    _recording_client(sent).simulate_plan("p1", request_key="k")

    _, _, body = _only(sent)
    parsed = SPECS["simulate_plan"].input_model.model_validate(body)
    assert parsed.model_dump(include={"reps", "seed", "baseline"}) == {
        "reps": None,
        "seed": 42,
        "baseline": False,
    }


def test_sdk_simulate_portfolio_posts_to_the_portfolio_route() -> None:
    sent = _Sent()

    _recording_client(sent).simulate_portfolio("pf-1", request_key="k", reps=5)

    method, path, body = _only(sent)
    assert (method, path) == ("POST", f"{API}/portfolios/pf-1/simulate")
    parsed = SPECS["simulate_portfolio"].input_model.model_validate(body)
    assert parsed.model_dump(include={"portfolio_id", "reps", "seed", "baseline"}) == {
        "portfolio_id": "pf-1",
        "reps": 5,
        "seed": 42,
        "baseline": True,
    }


def test_sdk_compare_plans_sends_simulation_ids() -> None:
    sent = _Sent()

    _recording_client(sent).compare_plans(["a", "b"], simulation_ids=["s-a", "s-b"])

    method, path, body = _only(sent)
    assert (method, path) == ("POST", f"{API}/plans/compare")
    parsed = SPECS["compare_plans"].input_model.model_validate(body)
    assert parsed.model_dump(mode="json") == {
        "plan_ids": ["a", "b"],
        "simulation_ids": ["s-a", "s-b"],
    }
