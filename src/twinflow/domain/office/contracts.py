"""Validated office domain value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value


@dataclass(frozen=True)
class Resource:
    id: str
    roles: frozenset[str]
    capacity: int = 1
    calendar: tuple[tuple[float, float], ...] = ((0.0, float("inf")),)


@dataclass(frozen=True)
class Document:
    id: str
    revision: str


@dataclass(frozen=True)
class Approval:
    task_id: str
    document_id: str
    revision: str
    role: str


@dataclass(frozen=True)
class Task:
    id: str
    duration: float
    role: str | None = None
    prerequisites: tuple[str, ...] = ()
    required_data: tuple[str, ...] = ()
    document_id: str | None = None
    document_revision: str | None = None
    approval_role: str | None = None
    max_rework: int = 0


@dataclass(frozen=True)
class Case:
    id: str
    data: dict[str, Any]
    documents: tuple[Document, ...] = ()
    approvals: tuple[Approval, ...] = ()
    start_time: float = 0.0
    rework: dict[str, int] | None = None


@dataclass(frozen=True)
class OfficeModel:
    tasks: tuple[Task, ...]
    resources: tuple[Resource, ...]
    documents: tuple[Document, ...] = ()
    domain: str = "office"
    revision: str = "1"


@dataclass(frozen=True)
class OfficeSnapshot:
    cases: tuple[Case, ...]
    as_of: str | None = None


def _document(raw: object, path: str) -> Document:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")
    return Document(
        _text(raw.get("id"), f"{path}.id"),
        _text(raw.get("revision"), f"{path}.revision"),
    )


def parse_model(raw: dict[str, Any]) -> OfficeModel:
    if raw.get("domain") != "office":
        raise ValueError("model.domain must be 'office'")
    tasks: list[Task] = []
    for index, item in enumerate(raw.get("tasks", [])):
        path = f"$.tasks[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{path} must be an object")
        duration = item.get("duration", 0)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
            raise ValueError(f"{path}.duration must be positive")
        gate = item.get("approval")
        if gate is not None and not isinstance(gate, dict):
            raise ValueError(f"{path}.approval must be an object")
        tasks.append(
            Task(
                _text(item.get("id"), f"{path}.id"),
                float(duration),
                item.get("role"),
                tuple(item.get("prerequisites", ())),
                tuple(item.get("required_data", ())),
                gate.get("document_id") if gate else None,
                gate.get("revision") if gate else None,
                gate.get("role") if gate else None,
                int(item.get("max_rework", 0)),
            )
        )
    resources: list[Resource] = []
    for index, item in enumerate(raw.get("resources", [])):
        path = f"$.resources[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{path} must be an object")
        capacity = item.get("capacity", 1)
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError(f"{path}.capacity must be positive")
        windows: tuple[tuple[float, float], ...] = tuple(
            (float(window[0]), float(window[1]))
            for window in item.get("calendar", [[0, float("inf")]])
        )
        if any(len(window) != 2 or window[0] < 0 or window[1] <= window[0] for window in windows):
            raise ValueError(f"{path}.calendar contains an invalid window")
        resources.append(
            Resource(
                _text(item.get("id"), f"{path}.id"),
                frozenset(item.get("roles", ())),
                capacity,
                windows,
            )
        )
    documents = tuple(
        _document(v, f"$.documents[{i}]") for i, v in enumerate(raw.get("documents", []))
    )
    return OfficeModel(
        tuple(tasks), tuple(resources), documents, "office", str(raw.get("revision", "1"))
    )


def parse_snapshot(raw: dict[str, Any]) -> OfficeSnapshot:
    cases: list[Case] = []
    for index, item in enumerate(raw.get("cases", [])):
        path = f"$.cases[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{path} must be an object")
        docs = tuple(
            _document(v, f"{path}.documents[{i}]") for i, v in enumerate(item.get("documents", []))
        )
        approvals: list[Approval] = []
        for ai, approval in enumerate(item.get("approvals", [])):
            if not isinstance(approval, dict):
                raise ValueError(f"{path}.approvals[{ai}] must be an object")
            approvals.append(
                Approval(
                    _text(approval.get("task_id"), f"{path}.approvals[{ai}].task_id"),
                    _text(approval.get("document_id"), f"{path}.approvals[{ai}].document_id"),
                    _text(approval.get("revision"), f"{path}.approvals[{ai}].revision"),
                    _text(approval.get("role"), f"{path}.approvals[{ai}].role"),
                )
            )
        cases.append(
            Case(
                _text(item.get("id"), f"{path}.id"),
                dict(item.get("data", {})),
                docs,
                tuple(approvals),
                float(item.get("start_time", 0)),
                dict(item.get("rework", {})) or None,
            )
        )
    return OfficeSnapshot(tuple(cases), raw.get("as_of"))
