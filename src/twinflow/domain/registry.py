"""Small registry shared by capsule and application layers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DomainIssue:
    path: str
    message: str
    severity: str = "error"
    suggestion: str | None = None


class DomainAdapter(Protocol):
    domain: str
    capabilities: frozenset[str]

    def validate(
        self, model: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> list[DomainIssue]: ...
    def describe(
        self, model: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> dict[str, object]: ...
    def evaluate(
        self,
        model: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        *,
        seed: int = 0,
        artifact_dir: str | None = None,
        limits: Mapping[str, int | float] | None = None,
    ) -> dict[str, object]: ...


class DomainRegistry:
    """Explicitly registered adapters; no dynamic imports or agent framework."""

    def __init__(self) -> None:
        self._adapters: dict[str, DomainAdapter] = {}

    def register(self, adapter: DomainAdapter) -> None:
        if not adapter.domain or adapter.domain in self._adapters:
            raise ValueError(f"domain already registered: {adapter.domain!r}")
        self._adapters[adapter.domain] = adapter

    def get(self, domain: str) -> DomainAdapter:
        try:
            return self._adapters[domain]
        except KeyError as exc:
            raise KeyError(f"unsupported domain: {domain}") from exc

    def validate(
        self, domain: str, model: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> list[DomainIssue]:
        return self.get(domain).validate(model, snapshot)

    def describe(
        self, domain: str, model: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> dict[str, object]:
        return self.get(domain).describe(model, snapshot)

    def evaluate(
        self, domain: str, model: Mapping[str, Any], snapshot: Mapping[str, Any], **kwargs: Any
    ) -> dict[str, object]:
        return self.get(domain).evaluate(model, snapshot, **kwargs)

    def capabilities(self) -> dict[str, frozenset[str]]:
        return {name: adapter.capabilities for name, adapter in self._adapters.items()}


registry = DomainRegistry()
