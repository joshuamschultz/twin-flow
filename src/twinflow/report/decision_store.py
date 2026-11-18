"""View models on disk for the workspace DECISIONS pages (DATA-CONTRACT.md 7, B-31).

One JSON file per view and id under `<workspace>/decisions/<kind>/<id>.json`. The CLI
publishes (`twinflow policies race --workspace`); the service reads
(`twinflow.service.decision_views`). Standard library only, so the CLI needs no `api` extra.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


__all__ = ["DecisionStore"]
