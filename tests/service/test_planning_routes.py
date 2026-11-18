"""REST and SDK parity with the MCP planning tools (T-215, T-221 REST half; COMP-123).

Requirements: REQ-180 (REST and SDK expose the planning use cases with the MCP tools' own
input and output models), REQ-185 (`GET/PUT /planning-profile` and `/k-table` share the
guard).

Contract pinned here:

* Routes under `/api/workspace` (tool in brackets):
  `POST /erp/validate` [validate_snapshot], `POST /erp/problems` [build_problem_from_erp],
  `POST /plans/portfolio` [solve_portfolio], `POST /plans/{plan_id}/resolve`
  [resolve_with_overrides], `POST /plans/{plan_id}/rush` [insert_rush_order],
  `GET /plans/{plan_id}?offset&limit&job_id` [get_plan], `GET /portfolios/{portfolio_id}/menu`
  [get_pareto_menu], `POST /plans/{plan_id}/explain` [explain_plan], `POST /plans/compare`
  [compare_plans], `POST /plans/{plan_id}/simulate` [simulate_plan],
  `POST /portfolios/{portfolio_id}/simulate` [simulate_portfolio] (TF-004 REQ-140),
  `GET|PUT /planning-profile` [get_planning_profile | put_planning_profile],
  `GET|PUT /k-table` [get_k_table | put_k_table].
* Request body = exactly the tool's `input_model` class (INCLUDING any id field); the 200
  response = exactly its `output_model` class. Checked in `app.openapi()`: the component is
  named after the class, and its dereferenced schema equals the model's own JSON schema
  modulo FastAPI's representation (titles, `default: null`, `$defs` vs components refs).
* REST id rule: when a path id differs from the body's id the route answers 400
  `{"code": "id_mismatch", ...}` and runs nothing.
* Same guard as MCP: `read` token on a `plan` tool -> 403 `{"code": "forbidden"}`; flood ->
  429 with an integer `Retry-After` header and `{"code": "rate_limited"}`; every call writes
  exactly one audit row whose `tool` is the matching tool name. Error bodies carry `code`
  and `correlation_id` (equal to the response's `X-Correlation-ID`), never a traceback.
  Unknown plan -> 404 `{"code": "not_found"}`.
* `create_app(..., portfolio_api=...)`: keyword injecting the TF-003 `PortfolioApi`
  (default None = load TF-003 lazily). TF-003 is an optional external dependency, so the
  tests inject the planning tests' `FakePortfolioApi` (real TF-002 solving underneath).
* SDK: `TwinflowClient(base_url, token=..., transport=...)` where `transport` is a callable
  `(method, url, headers, body: bytes | None) -> (status, body_bytes)` used instead of
  urllib when given (stdlib-only SDK; no new dependency). Methods named like the tools:
  `validate_snapshot(snapshot, profile_digest=None)`, `build_problem_from_erp(snapshot,
  profile_digest=None)`, `solve_portfolio(problem_id, *, request_key, ...)`,
  `resolve_with_overrides(plan_id, *, overrides, request_key, ...)`,
  `insert_rush_order(plan_id, *, rush, request_key, ...)`, `get_plan(plan_id, offset=0,
  limit=100, job_id=None)`, `get_pareto_menu(portfolio_id)`, `explain_plan(plan_id,
  job_ids=None)`, `compare_plans(plan_ids, simulation_ids=None)`,
  `simulate_plan(plan_id, *, request_key, reps=None, seed=42, time_budget_seconds=None,
  baseline=False, profile_digest=None)`, `simulate_portfolio(portfolio_id, *, request_key,
  reps=None, seed=42, time_budget_seconds=None, baseline=True, profile_digest=None)`,
  `get_planning_profile()`, `put_planning_profile(*, profile_toml=None, profile=None)`,
  `get_k_table()`, `put_k_table(table)`. Each returns the decoded JSON object; a refusal
  raises `TwinflowError(status, detail)` whose `str()` is the JSON error body.
  `get_plan` builds its own query string; `request()` still refuses one.
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
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel
from starlette.testclient import TestClient

from twinflow.mcp.tools import CATALOGUE, ToolSpec
from twinflow.sdk import TwinflowClient, TwinflowError
from twinflow.service.app import create_app
from twinflow.service.auth import add_token
from twinflow.service.settings import ServiceSettings
from twinflow.service.signing import generate_signing_key

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
_PLANNING_TESTS = str(ROOT / "tests" / "planning")
if _PLANNING_TESTS not in sys.path:
    sys.path.insert(0, _PLANNING_TESTS)

from _fake_portfolio import FakePortfolioApi, as_portfolio_api, tiny_profile, tiny_shop

API = "/api/workspace"
BASE_URL = "http://testserver"
SPECS: dict[str, ToolSpec] = {spec.name: spec for spec in CATALOGUE}
PROFILE_TOML = "time_budget_seconds = 10.0\n\n[erp]\nrush_class = 2\n\n[erp.class_weights]\nA = 4\n"
HOLD_PO2 = {
    "kind": "hold",
    "id": "hold-po2",
    "fact_ref": "operator",
    "reason": "customer hold",
    "hardness": "hard",
    "job_id": "PO-2",
}

# (method, path template, tool)
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("post", "/erp/validate", "validate_snapshot"),
    ("post", "/erp/problems", "build_problem_from_erp"),
    ("post", "/plans/portfolio", "solve_portfolio"),
    ("post", "/plans/{plan_id}/resolve", "resolve_with_overrides"),
    ("post", "/plans/{plan_id}/rush", "insert_rush_order"),
    ("get", "/plans/{plan_id}", "get_plan"),
    ("get", "/portfolios/{portfolio_id}/menu", "get_pareto_menu"),
    ("post", "/plans/{plan_id}/explain", "explain_plan"),
    ("post", "/plans/compare", "compare_plans"),
    ("post", "/plans/{plan_id}/simulate", "simulate_plan"),
    ("post", "/portfolios/{portfolio_id}/simulate", "simulate_portfolio"),
    ("get", "/planning-profile", "get_planning_profile"),
    ("put", "/planning-profile", "put_planning_profile"),
    ("get", "/k-table", "get_k_table"),
    ("put", "/k-table", "put_k_table"),
)
ROUTE_IDS = [f"{method.upper()} {path}" for method, path, _ in ROUTES]


# --- the service under test -------------------------------------------------------------------


@dataclass
class Rest:
    """A running app, its tokens and handles, plus seeded planning ids."""

    client: TestClient
    tokens: dict[str, str]
    workspace: Path
    fake: FakePortfolioApi
    ids: dict[str, Any] = field(default_factory=dict)


def _secrets(base: Path) -> tuple[Path, Path, dict[str, str]]:
    """A token registry (`reader`: read; `planner`: read, plan) and a signing key, both
    outside the workspace, mode 0600."""
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


@contextmanager
def _open(base: Path, *, calls_per_minute: int = 10_000) -> Iterator[Rest]:
    tokens_file, key_file, tokens = _secrets(base)
    settings = ServiceSettings(
        tokens_file=tokens_file,
        signing_key_file=key_file,
        calls_per_minute=calls_per_minute,
        max_solve_seconds=30.0,
    )
    fake = FakePortfolioApi()
    workspace = base / "workspace"
    app = create_app(
        EXAMPLES,
        workspace_root=workspace,
        settings=settings,
        portfolio_api=as_portfolio_api(fake),
    )
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield Rest(client=client, tokens=tokens, workspace=workspace, fake=fake)


def _bearer(rest: Rest, who: str = "planner", **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {rest.tokens[who]}", **extra}


def _call(
    rest: Rest,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    who: str = "planner",
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    return rest.client.request(
        method.upper(),
        API + path,
        json=body,
        params=params,
        headers={**_bearer(rest, who), **(headers or {})},
    )


def _ok(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, (response.status_code, response.text[:800])
    payload: dict[str, Any] = response.json()
    return payload


def _seed(rest: Rest) -> None:
    profile = _ok(
        _call(rest, "put", "/planning-profile", {"profile": tiny_profile().model_dump(mode="json")})
    )
    built = _ok(_call(rest, "post", "/erp/problems", {"snapshot": _shop()}))
    assert built["status"] == "ready", built
    portfolio = _ok(
        _call(
            rest,
            "post",
            "/plans/portfolio",
            {"problem_id": built["problem_id"], "request_key": "seed-solve"},
        )
    )
    assert len(portfolio["plan_ids"]) >= 2, portfolio
    rest.ids.update(
        profile_digest=profile["digest"],
        problem=built["problem_id"],
        portfolio=portfolio["portfolio_id"],
        plans=list(portfolio["plan_ids"]),
    )


@pytest.fixture(scope="module")
def rest(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Rest]:
    with _open(tmp_path_factory.mktemp("planning-routes")) as opened:
        _seed(opened)
        yield opened


def _shop() -> dict[str, Any]:
    return tiny_shop().model_dump(mode="json")


def _rush() -> dict[str, Any]:
    return {
        "order_id": "RUSH-1",
        "item_id": "WIDGET",
        "quantity": 2.0,
        "due": tiny_shop().as_of.replace(hour=18).isoformat(),
        "customer_id": "CUST-A",
        "routing_id": None,
    }


def _audit_rows(workspace: Path) -> list[tuple[str, str, str]]:
    """(caller_id, tool, outcome) of every tool-call audit row, oldest first."""
    uri = f"file:{workspace / 'workspace.sqlite3'}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute(
            "SELECT caller_id, tool, outcome FROM audit_events "
            "WHERE operation = 'tool_call' ORDER BY sequence"
        ).fetchall()
    return [(str(c), str(t), str(o)) for c, t, o in rows]


def _assert_error_body(response: Any, code: str) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    assert body["code"] == code, body
    assert body["correlation_id"], body
    assert body["correlation_id"] == response.headers["x-correlation-id"], body
    assert "Traceback" not in response.text
    assert 'File "' not in response.text
    return body


# --- OpenAPI schema equality with the tool models (REQ-180) -----------------------------------


def _deref(node: Any, components: Mapping[str, Any]) -> Any:
    """Inline every $ref and drop representation-only differences between FastAPI's OpenAPI
    and pydantic's `model_json_schema` (titles, `default: null`, ref prefixes)."""
    if isinstance(node, dict):
        if "$ref" in node:
            return _deref(components[node["$ref"].rsplit("/", 1)[-1]], components)
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$defs", "title") or (key == "default" and value is None):
                continue
            if key == "discriminator":
                mapping = {k: v.rsplit("/", 1)[-1] for k, v in value.get("mapping", {}).items()}
                out[key] = {**value, "mapping": mapping}
                continue
            out[key] = _deref(value, components)
        return out
    if isinstance(node, list):
        return [_deref(item, components) for item in node]
    return node


def _model_schema(model: type[BaseModel], mode: str) -> Any:
    schema = model.model_json_schema(mode=mode)  # type: ignore[arg-type]  # "validation" | "serialization"
    return _deref(schema, schema.get("$defs", {}))


def _component_name(schema: Mapping[str, Any]) -> str:
    assert "$ref" in schema, f"not a named component: {schema}"
    return str(schema["$ref"]).rsplit("/", 1)[-1]


def _same_class(component: str, model: type[BaseModel]) -> bool:
    return component in (model.__name__, f"{model.__name__}-Input", f"{model.__name__}-Output")


def _operation(rest: Rest, method: str, path: str) -> dict[str, Any]:
    paths = rest.client.app.openapi()["paths"]  # type: ignore[attr-defined]  # FastAPI app
    assert API + path in paths, f"route {method.upper()} {API + path} is missing"
    operations = paths[API + path]
    assert method in operations, f"route {method.upper()} {API + path} is missing"
    operation: dict[str, Any] = operations[method]
    return operation


@pytest.mark.parametrize(("method", "path", "tool"), ROUTES, ids=ROUTE_IDS)
def test_route_response_is_the_tool_output_model(
    rest: Rest, method: str, path: str, tool: str
) -> None:
    spec = SPECS[tool]
    components = rest.client.app.openapi()["components"]["schemas"]  # type: ignore[attr-defined]
    operation = _operation(rest, method, path)

    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]

    assert _same_class(_component_name(schema), spec.output_model), schema
    assert _deref(schema, components) == _model_schema(spec.output_model, "serialization")


BODY_ROUTES = [route for route in ROUTES if route[0] in ("post", "put")]


@pytest.mark.parametrize(
    ("method", "path", "tool"),
    BODY_ROUTES,
    ids=[f"{m.upper()} {p}" for m, p, _ in BODY_ROUTES],
)
def test_route_request_body_is_the_tool_input_model_including_ids(
    rest: Rest, method: str, path: str, tool: str
) -> None:
    spec = SPECS[tool]
    components = rest.client.app.openapi()["components"]["schemas"]  # type: ignore[attr-defined]
    operation = _operation(rest, method, path)

    schema = operation["requestBody"]["content"]["application/json"]["schema"]

    assert _same_class(_component_name(schema), spec.input_model), schema
    assert _deref(schema, components) == _model_schema(spec.input_model, "validation")


def test_get_plan_route_takes_the_tool_fields_as_path_and_query(rest: Rest) -> None:
    operation = _operation(rest, "get", "/plans/{plan_id}")
    parameters = {(p["in"], p["name"]) for p in operation.get("parameters", [])}

    assert "requestBody" not in operation
    assert parameters == {
        ("path", "plan_id"),
        ("query", "offset"),
        ("query", "limit"),
        ("query", "job_id"),
    }
    assert set(SPECS["get_plan"].input_model.model_fields) == {n for _, n in parameters}


# --- REST behaviour --------------------------------------------------------------------------


def test_every_route_call_writes_exactly_one_audit_row_naming_its_tool(rest: Rest) -> None:
    plan_id, other_plan = rest.ids["plans"][:2]
    calls: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None, str]] = [
        ("post", "/erp/validate", {"snapshot": _shop()}, None, "validate_snapshot"),
        ("post", "/erp/problems", {"snapshot": _shop()}, None, "build_problem_from_erp"),
        (
            "post",
            "/plans/portfolio",
            {"problem_id": rest.ids["problem"], "request_key": "audit-solve"},
            None,
            "solve_portfolio",
        ),
        (
            "post",
            f"/plans/{plan_id}/resolve",
            {"plan_id": plan_id, "overrides": [HOLD_PO2], "request_key": "audit-resolve"},
            None,
            "resolve_with_overrides",
        ),
        (
            "post",
            f"/plans/{plan_id}/rush",
            {"plan_id": plan_id, "rush": _rush(), "request_key": "audit-rush"},
            None,
            "insert_rush_order",
        ),
        ("get", f"/plans/{plan_id}", None, {"offset": 0, "limit": 5}, "get_plan"),
        ("get", f"/portfolios/{rest.ids['portfolio']}/menu", None, None, "get_pareto_menu"),
        ("post", f"/plans/{plan_id}/explain", {"plan_id": plan_id}, None, "explain_plan"),
        ("post", "/plans/compare", {"plan_ids": [plan_id, other_plan]}, None, "compare_plans"),
        (
            "post",
            f"/plans/{plan_id}/simulate",
            {"plan_id": plan_id, "request_key": "audit-sim"},
            None,
            "simulate_plan",
        ),
        (
            "post",
            f"/portfolios/{rest.ids['portfolio']}/simulate",
            {"portfolio_id": rest.ids["portfolio"], "request_key": "audit-sim-pf"},
            None,
            "simulate_portfolio",
        ),
        ("get", "/planning-profile", None, None, "get_planning_profile"),
        ("put", "/planning-profile", {"profile_toml": PROFILE_TOML}, None, "put_planning_profile"),
        ("get", "/k-table", None, None, "get_k_table"),
        ("put", "/k-table", {"table": {}}, None, "put_k_table"),
    ]
    assert {c[4] for c in calls} == {r[2] for r in ROUTES}  # every route is exercised

    for method, path, body, params, tool in calls:
        before = len(_audit_rows(rest.workspace))
        response = _call(rest, method, path, body, params=params)
        assert response.status_code != 404 or response.json().get("code"), (
            f"{method.upper()} {path} is not routed: {response.text[:300]}"
        )
        new_rows = _audit_rows(rest.workspace)[before:]
        assert [(caller, name) for caller, name, _ in new_rows] == [("planner", tool)], (
            method,
            path,
            new_rows,
        )


def test_read_token_on_put_planning_profile_is_403_forbidden_and_audited_denied(
    rest: Rest,
) -> None:
    before = len(_audit_rows(rest.workspace))

    response = _call(
        rest,
        "put",
        "/planning-profile",
        {"profile_toml": PROFILE_TOML},
        who="reader",
        headers={"X-Correlation-ID": "corr-forbidden-1"},
    )

    assert response.status_code == 403, response.text[:500]
    body = _assert_error_body(response, "forbidden")
    assert body["correlation_id"] == "corr-forbidden-1"
    assert _audit_rows(rest.workspace)[before:] == [("reader", "put_planning_profile", "denied")]


def test_read_token_may_read_the_planning_profile(rest: Rest) -> None:
    body = _ok(_call(rest, "get", "/planning-profile", who="reader"))

    assert body["kind"] == "planning_profiles"
    assert body["digest"]
    assert isinstance(body["sequence"], int)
    assert body["key_id"]


def test_path_id_differing_from_body_id_is_400_id_mismatch_and_solves_nothing(
    rest: Rest,
) -> None:
    plan_a, plan_b = rest.ids["plans"][:2]
    solves_before = sum(rest.fake.calls.values())

    response = _call(
        rest,
        "post",
        f"/plans/{plan_a}/resolve",
        {"plan_id": plan_b, "overrides": [HOLD_PO2], "request_key": "mismatch-1"},
    )

    assert response.status_code == 400, response.text[:500]
    _assert_error_body(response, "id_mismatch")
    assert sum(rest.fake.calls.values()) == solves_before


def test_unknown_plan_is_404_not_found_with_correlation_id(rest: Rest) -> None:
    response = _call(rest, "get", "/plans/no-such-plan")

    assert response.status_code == 404, response.text[:500]
    _assert_error_body(response, "not_found")


def test_get_plan_route_pages_by_offset_and_limit(rest: Rest) -> None:
    plan_id = rest.ids["plans"][0]

    page = _ok(_call(rest, "get", f"/plans/{plan_id}", params={"offset": 1, "limit": 1}))

    assert page["plan_id"] == plan_id
    assert page["offset"] == 1
    assert page["limit"] == 1
    assert len(page["assignments"]) == 1


def test_flood_is_429_with_retry_after_and_rate_limited_code(tmp_path: Path) -> None:
    with _open(tmp_path, calls_per_minute=1) as flooded:
        responses = [_call(flooded, "get", "/planning-profile", who="reader") for _ in range(25)]

        statuses = [response.status_code for response in responses]
        assert 429 not in statuses[:20], statuses
        refused = responses[20:]
        assert {response.status_code for response in refused} == {429}, statuses
        for response in refused:
            assert int(response.headers["Retry-After"]) >= 1
            _assert_error_body(response, "rate_limited")
        outcomes = [o for c, t, o in _audit_rows(flooded.workspace) if c == "reader"]
        assert outcomes.count("rate_limited") == 5


# --- SDK parity ------------------------------------------------------------------------------


@dataclass
class _Seen:
    """Every request the SDK sent through the injected transport."""

    requests: list[tuple[str, str]] = field(default_factory=list)


def _sdk(rest: Rest, seen: _Seen, who: str = "planner") -> TwinflowClient:
    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        seen.requests.append((method, url))
        response = rest.client.request(method, url, headers=dict(headers), content=body)
        return response.status_code, response.content

    return TwinflowClient(BASE_URL, token=rest.tokens[who], transport=transport)


def _valid(tool: str, payload: Any) -> dict[str, Any]:
    """The SDK result is a JSON object that validates as the tool's output model."""
    assert isinstance(payload, dict), payload
    SPECS[tool].output_model.model_validate(payload)
    return payload


def _sdk_error_code(error: TwinflowError) -> str:
    return str(json.loads(str(error))["code"])


def test_sdk_methods_round_trip_the_planning_flow_through_the_app(rest: Rest) -> None:
    seen = _Seen()
    sdk = _sdk(rest, seen)

    ref = sdk.put_planning_profile(profile_toml=PROFILE_TOML)
    SPECS["put_planning_profile"].output_model.model_validate(ref)
    active = _valid("get_planning_profile", sdk.get_planning_profile())
    assert active["digest"] == ref["digest"]
    report = _valid("validate_snapshot", sdk.validate_snapshot(_shop()))
    assert report["hard_count"] == 0
    built = _valid("build_problem_from_erp", sdk.build_problem_from_erp(_shop()))
    assert built["status"] == "ready"
    portfolio = _valid(
        "solve_portfolio", sdk.solve_portfolio(built["problem_id"], request_key="sdk-solve")
    )
    plan_id, other = portfolio["plan_ids"][:2]
    page = _valid("get_plan", sdk.get_plan(plan_id, offset=1, limit=1))
    assert (page["offset"], page["limit"], len(page["assignments"])) == (1, 1, 1)
    menu = _valid("get_pareto_menu", sdk.get_pareto_menu(portfolio["portfolio_id"]))
    assert menu["portfolio_id"] == portfolio["portfolio_id"]
    compared = _valid("compare_plans", sdk.compare_plans([plan_id, other]))
    assert set(compared["ranking"]) == {plan_id, other}
    _valid(
        "resolve_with_overrides",
        sdk.resolve_with_overrides(plan_id, overrides=[HOLD_PO2], request_key="sdk-resolve"),
    )
    _valid("insert_rush_order", sdk.insert_rush_order(plan_id, rush=_rush(), request_key="sdk-r"))
    simulated = _valid("simulate_plan", sdk.simulate_plan(plan_id, request_key="sdk-sim"))
    assert simulated["status"] in ("completed", "truncated")
    other_sim = _valid("simulate_plan", sdk.simulate_plan(other, request_key="sdk-sim-2"))
    portfolio_sim = _valid(
        "simulate_portfolio",
        sdk.simulate_portfolio(portfolio["portfolio_id"], request_key="sdk-sim-pf"),
    )
    assert portfolio_sim["portfolio_id"] == portfolio["portfolio_id"]
    paired = _valid(
        "compare_plans",
        sdk.compare_plans(
            [plan_id, other],
            simulation_ids=[simulated["simulation_id"], other_sim["simulation_id"]],
        ),
    )
    assert paired["simulation"]["reference_plan_id"] == plan_id

    with pytest.raises(TwinflowError) as explained:
        sdk.explain_plan(plan_id)
    assert _sdk_error_code(explained.value) == "capability_unavailable"
    # TF-003 is installed, so K-tables are a capability: none was put yet (not_found), and
    # an empty table is not a valid TF-003 `KTable`.
    with pytest.raises(TwinflowError) as k_read:
        sdk.get_k_table()
    assert _sdk_error_code(k_read.value) == "not_found"
    with pytest.raises(TwinflowError) as k_write:
        sdk.put_k_table({})
    assert _sdk_error_code(k_write.value) == "invalid_input"

    assert all(urlsplit(url).netloc == "testserver" for _, url in seen.requests)


def test_sdk_get_plan_builds_its_own_query_string(rest: Rest) -> None:
    seen = _Seen()
    plan_id = rest.ids["plans"][0]

    _sdk(rest, seen).get_plan(plan_id, offset=2, limit=3, job_id="PO-1")

    method, url = seen.requests[-1]
    parts = urlsplit(url)
    assert method == "GET"
    assert parts.path == f"{API}/plans/{plan_id}"
    assert parse_qs(parts.query) == {"offset": ["2"], "limit": ["3"], "job_id": ["PO-1"]}


def test_sdk_get_plan_quotes_the_plan_id_into_one_path_segment(rest: Rest) -> None:
    seen = _Seen()

    with pytest.raises(TwinflowError) as missing:
        _sdk(rest, seen).get_plan("a/b?x=1#f", offset=0, limit=1)

    assert _sdk_error_code(missing.value) == "not_found"
    _, url = seen.requests[-1]
    assert urlsplit(url).path.startswith(f"{API}/plans/")
    assert urlsplit(url).path.count("/") == (API + "/plans/x").count("/")


def test_sdk_request_still_refuses_a_query_string() -> None:
    client = TwinflowClient(BASE_URL)

    with pytest.raises(ValueError, match="Invalid workspace route"):
        client.request("/plans/p1?offset=1")


def test_sdk_planning_call_with_read_token_raises_forbidden(rest: Rest) -> None:
    seen = _Seen()

    with pytest.raises(TwinflowError) as refused:
        _sdk(rest, seen, who="reader").put_planning_profile(profile_toml=PROFILE_TOML)

    assert refused.value.status == 403
    assert _sdk_error_code(refused.value) == "forbidden"
