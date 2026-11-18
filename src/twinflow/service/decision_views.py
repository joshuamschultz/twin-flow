"""B-31: the decision view API, one generic layer (DATA-CONTRACT.md 7, DESIGN.md 11.3).

Every view kind (race, run, states, improve, project) is one `ViewSpec` in a registry:
a builder that returns the view model, and a summary that makes one row for the list page.
A new view plugs in with one `registry.register(...)` call. The routes never change.

View models are stored as JSON under `<workspace>/decisions/<kind>/<id>.json`.
`DecisionStore.publish` writes one. The routes only read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from twinflow.report import decision

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class DecisionStore:
    """View models on disk, one JSON file per view and id."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root) / "decisions"

    def _path(self, kind: str, item_id: str) -> Path:
        if not _ID.fullmatch(item_id) or not _ID.fullmatch(kind):
            raise ValueError("The id may hold letters, digits, dots, dashes and underscores.")
        return self.root / kind / f"{item_id}.json"

    def publish(self, kind: str, item_id: str, view_model: Mapping[str, Any]) -> Path:
        path = self._path(kind, item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(view_model, ensure_ascii=False), encoding="utf-8")
        return path

    def load(self, kind: str, item_id: str) -> dict[str, Any]:
        """The stored view model. Raises `KeyError` when it is missing or unreadable."""
        try:
            path = self._path(kind, item_id)
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise KeyError(item_id) from exc
        if not isinstance(data, dict):
            raise KeyError(item_id)
        return data

    def ids(self, kind: str) -> list[str]:
        folder = self.root / kind
        if not folder.is_dir():
            return []
        return sorted(p.stem for p in folder.glob("*.json") if _ID.fullmatch(p.stem))


Params = Mapping[str, str]
Builder = Callable[[DecisionStore, str, Params], dict[str, Any]]
Summarizer = Callable[[str, Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ViewSpec:
    """How one view kind is built, listed and named."""

    kind: str
    plural: str
    build: Builder
    summarize: Summarizer


class ViewRegistry:
    def __init__(self) -> None:
        self._by_kind: dict[str, ViewSpec] = {}

    def register(self, spec: ViewSpec) -> None:
        self._by_kind[spec.kind] = spec

    def by_kind(self, kind: str) -> ViewSpec | None:
        return self._by_kind.get(kind)

    def by_plural(self, plural: str) -> ViewSpec | None:
        return next((s for s in self._by_kind.values() if s.plural == plural), None)


def _stored_view(kind: str) -> Builder:
    def build(store: DecisionStore, item_id: str, params: Params) -> dict[str, Any]:
        return store.load(kind, item_id)

    return build


def _part(model: Mapping[str, Any], key: str, field: str) -> Any:
    section = model.get(key)
    return section.get(field) if isinstance(section, Mapping) else None


def summarize_race(race_id: str, model: Mapping[str, Any]) -> dict[str, Any]:
    """One list row: the headline sentence answers the question before the click."""
    return {
        "id": race_id,
        "project": _part(model, "about", "project") or "",
        "when": str(model.get("generated_at") or ""),
        "sentence": _part(model, "recommendation", "sentence") or "",
        "level": _part(model, "confidence", "level") or "unknown",
    }


def default_registry() -> ViewRegistry:
    """The views that exist today. Other builders add themselves with one line each."""
    registry = ViewRegistry()
    registry.register(
        ViewSpec("race", "races", _stored_view("race"), summarize_race),
    )
    return registry


def decision_view_router(store: DecisionStore, registry: ViewRegistry) -> APIRouter:
    router = APIRouter(prefix="/api/v1/decision", tags=["Decision views"])

    def spec_for(kind: str) -> ViewSpec:
        spec = registry.by_kind(kind)
        if spec is None:
            raise HTTPException(404, f"There is no view called {kind}.")
        return spec

    def build(kind: str, item_id: str, params: Params) -> dict[str, Any]:
        spec = spec_for(kind)
        try:
            return spec.build(store, item_id, params)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, f"No {kind} with that id.") from exc

    @router.get("/{plural}")
    def list_views(plural: str) -> list[dict[str, Any]]:
        spec = registry.by_plural(plural)
        if spec is None:
            raise HTTPException(404, f"There is no list called {plural}.")
        rows = []
        for item_id in store.ids(spec.kind):
            try:
                rows.append(spec.summarize(item_id, spec.build(store, item_id, {})))
            except (KeyError, ValueError):
                continue  # a damaged file must not hide the others
        return sorted(rows, key=lambda r: str(r.get("when", "")), reverse=True)

    @router.get("/{kind}/{item_id}")
    def view_model(kind: str, item_id: str, request: Request) -> dict[str, Any]:
        return build(kind, item_id, dict(request.query_params))

    @router.get("/{kind}/{item_id}/export")
    def export(kind: str, item_id: str, request: Request) -> Response:
        html = decision.render(build(kind, item_id, dict(request.query_params)))
        return Response(
            html.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{kind}-{item_id}.html"'},
        )

    return router
