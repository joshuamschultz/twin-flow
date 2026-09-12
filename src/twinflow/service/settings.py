"""Explicit settings for the single-workspace service boundary."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceSettings:
    """Security and resource bounds injected into the service and workspace."""

    local_only: bool = True
    api_token: str | None = None
    cors_origins: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")
    max_request_bytes: int = 5_242_880
    max_active_jobs: int = 4
    max_replications: int = 50
    max_records_per_kind: int = 10_000
    max_evidence_rows: int = 1_000

    def __post_init__(self) -> None:
        if not self.local_only and self.api_token is None:
            raise ValueError("dedicated service mode requires an API token")
        if self.api_token is not None and len(self.api_token) < 16:
            raise ValueError("API token must contain at least 16 characters")
        for name, value in (
            ("max_request_bytes", self.max_request_bytes),
            ("max_active_jobs", self.max_active_jobs),
            ("max_replications", self.max_replications),
            ("max_records_per_kind", self.max_records_per_kind),
            ("max_evidence_rows", self.max_evidence_rows),
        ):
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if self.max_request_bytes > 5_242_880:
            raise ValueError("max_request_bytes cannot exceed the API schema bound")
        if "*" in self.cors_origins or "*" in self.allowed_hosts:
            raise ValueError("wildcard CORS origins and allowed hosts are not permitted")

    @classmethod
    def from_env(cls, *, host: str = "127.0.0.1") -> ServiceSettings:
        """Build settings once at startup; environment is not read per request."""
        token = os.environ.get("TWINFLOW_API_TOKEN") or None
        cors = _csv_env("TWINFLOW_CORS_ORIGINS")
        allowed = _csv_env("TWINFLOW_ALLOWED_HOSTS") or ("127.0.0.1", "localhost", "testserver")
        settings = cls(
            local_only=_is_loopback(host),
            api_token=token,
            cors_origins=cors,
            allowed_hosts=allowed,
        )
        settings.validate_bind(host)
        return settings

    def validate_bind(self, host: str) -> None:
        """Refuse accidental remote exposure without the configured bearer token."""
        if not _is_loopback(host) and self.api_token is None:
            raise ValueError("non-loopback service binding requires TWINFLOW_API_TOKEN")


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.environ.get(name, "").split(",") if value.strip())
