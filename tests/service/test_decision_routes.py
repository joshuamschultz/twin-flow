"""B-31: the decision view API (DATA-CONTRACT.md 7).

Pinned contract:

* `GET /api/v1/decision/races` lists `{id, project, when, sentence, level}` per stored race.
* `GET /api/v1/decision/race/{id}` returns the race view model, valid against its schema.
* `GET /api/v1/decision/{view}/{id}/export` returns the exact bytes the CLI writes
  (`twinflow.report.decision.render`), as a download.
* The layer is a registry: a view kind plugs in with one `register_view` call.
* Unknown view, unknown id and a bad id answer 404 with a plain message.
"""

# ruff: noqa: E402 -- the api extra is optional: skip before importing it.

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
from starlette.testclient import TestClient

from twinflow.report import decision
from twinflow.service.app import create_app
from twinflow.service.decision_views import (
    DecisionStore,
    ViewSpec,
    default_registry,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "docs" / "design" / "race-view-model.example.json"
SCHEMA = ROOT / "src" / "twinflow" / "report" / "schemas" / "race.schema.json"
BASE = "/api/v1/decision"


def _model() -> dict[str, Any]:
    model = json.loads(EXAMPLE.read_text())
    model.pop("_about", None)
    return model


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
def client(workspace: Path) -> Iterator[TestClient]:
    app = create_app(ROOT / "examples", workspace_root=workspace)
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


def _publish(workspace: Path, race_id: str, model: dict[str, Any]) -> None:
    DecisionStore(workspace).publish("race", race_id, model)


def test_races_list_is_empty_before_any_race(client: TestClient) -> None:
    response = client.get(f"{BASE}/races")
    assert response.status_code == 200
    assert response.json() == []


def test_races_list_has_one_headline_row_per_race(client: TestClient, workspace: Path) -> None:
    _publish(workspace, "plant-a", _model())
    rows = client.get(f"{BASE}/races").json()
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"id", "project", "when", "sentence", "level"}
    assert row["id"] == "plant-a"
    assert row["sentence"] == _model()["recommendation"]["sentence"]
    assert row["level"] == _model()["confidence"]["level"]
    assert row["project"] == _model()["about"]["project"]
    assert row["when"] == _model()["generated_at"]


def test_races_list_puts_the_newest_race_first(client: TestClient, workspace: Path) -> None:
    older = _model() | {"generated_at": "2026-01-01T00:00:00+00:00"}
    newer = _model() | {"generated_at": "2026-06-01T00:00:00+00:00"}
    _publish(workspace, "old", older)
    _publish(workspace, "new", newer)
    assert [row["id"] for row in client.get(f"{BASE}/races").json()] == ["new", "old"]


def test_race_view_model_is_schema_valid(client: TestClient, workspace: Path) -> None:
    _publish(workspace, "plant-a", _model())
    response = client.get(f"{BASE}/race/plant-a")
    assert response.status_code == 200
    schema = json.loads(SCHEMA.read_text())
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(response.json())) == []
    assert response.json() == _model()


def test_unknown_race_is_404_with_a_plain_message(client: TestClient) -> None:
    response = client.get(f"{BASE}/race/nope")
    assert response.status_code == 404
    assert "race" in response.json()["detail"].lower()


@pytest.mark.parametrize("bad_id", ["..", "a b", "x" * 200, ".hidden"])
def test_a_bad_id_never_reaches_the_disk(client: TestClient, bad_id: str) -> None:
    assert client.get(f"{BASE}/race/{bad_id}").status_code == 404


def test_unknown_view_is_404(client: TestClient) -> None:
    assert client.get(f"{BASE}/spaceship/abc").status_code == 404
    assert client.get(f"{BASE}/spaceships").status_code == 404


def test_export_is_the_same_bytes_the_cli_writes(client: TestClient, workspace: Path) -> None:
    _publish(workspace, "plant-a", _model())
    response = client.get(f"{BASE}/race/plant-a/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'filename="race-plant-a.html"' in response.headers["content-disposition"]
    assert response.content == decision.render(_model()).encode("utf-8")


def test_export_of_unknown_race_is_404(client: TestClient) -> None:
    assert client.get(f"{BASE}/race/nope/export").status_code == 404


def test_a_new_view_plugs_in_with_one_registration(tmp_path: Path) -> None:
    registry = default_registry()
    registry.register(
        ViewSpec(
            kind="run",
            plural="runs",
            build=lambda store, run_id, params: {"view": "run", "id": run_id, "top": params},
            summarize=lambda run_id, model: {
                "id": run_id,
                "project": "p",
                "when": "w",
                "sentence": "s",
                "level": "l",
            },
        )
    )
    store = DecisionStore(tmp_path)
    store.publish("run", "r1", {"view": "run"})
    app = create_app(ROOT / "examples", workspace_root=tmp_path / "ws", decision_views=registry)
    with TestClient(app, base_url="http://testserver") as test_client:
        built = test_client.get(f"{BASE}/run/r1", params={"top": "5"})
        assert built.json() == {"view": "run", "id": "r1", "top": {"top": "5"}}
        assert test_client.get(f"{BASE}/runs").status_code == 200


def test_the_store_refuses_a_path_like_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="id"):
        DecisionStore(tmp_path).publish("race", "../escape", {"view": "race"})


def test_a_damaged_file_is_skipped_in_the_list(client: TestClient, workspace: Path) -> None:
    _publish(workspace, "good", _model())
    broken = workspace / "decisions" / "race" / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert [row["id"] for row in client.get(f"{BASE}/races").json()] == ["good"]
    assert client.get(f"{BASE}/race/broken").status_code == 404
