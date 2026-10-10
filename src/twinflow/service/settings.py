"""Explicit settings for the single-workspace service boundary."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceSettings:
    """Security and resource bounds injected into the service and workspace.

    Callers authenticate with named tokens from the registry at ``tokens_file`` (COMP-120,
    D-117). Without a registry the service runs in open local mode, allowed on loopback only.
    """

    local_only: bool = True
    tokens_file: Path | None = None
    cors_origins: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")
    max_request_bytes: int = 5_242_880
    max_mcp_request_bytes: int = 16_777_216
    max_tool_result_bytes: int = 1_048_576
    max_solve_seconds: float = 120.0
    calls_per_minute: int = 120
    max_concurrent_solves_per_caller: int = 2
    mcp_allowed_origins: tuple[str, ...] = ()
    signing_key_file: Path | None = None
    max_active_jobs: int = 4
    max_replications: int = 50
    max_records_per_kind: int = 10_000
    max_evidence_rows: int = 1_000

    def __post_init__(self) -> None:
        if not self.local_only and self.tokens_file is None:
            raise ValueError("dedicated service mode requires a token registry (tokens_file)")
        for name, value in (
            ("max_request_bytes", self.max_request_bytes),
            ("max_mcp_request_bytes", self.max_mcp_request_bytes),
            ("max_tool_result_bytes", self.max_tool_result_bytes),
            ("max_solve_seconds", self.max_solve_seconds),
            ("calls_per_minute", self.calls_per_minute),
            ("max_concurrent_solves_per_caller", self.max_concurrent_solves_per_caller),
            ("max_active_jobs", self.max_active_jobs),
            ("max_replications", self.max_replications),
            ("max_records_per_kind", self.max_records_per_kind),
            ("max_evidence_rows", self.max_evidence_rows),
        ):
            if not value > 0:
                raise ValueError(f"{name} must be positive")
        if self.max_request_bytes > 5_242_880:
            raise ValueError("max_request_bytes cannot exceed the API schema bound")
        if "*" in self.cors_origins or "*" in self.allowed_hosts or "*" in self.mcp_allowed_origins:
            raise ValueError(
                "wildcard CORS origins, MCP origins and allowed hosts are not permitted"
            )

    @classmethod
    def from_env(cls, *, host: str = "127.0.0.1") -> ServiceSettings:
        """Build settings once at startup; environment is not read per request."""
        allowed = _csv_env("TWINFLOW_ALLOWED_HOSTS") or ("127.0.0.1", "localhost", "testserver")
        settings = cls(
            local_only=_is_loopback(host),
            tokens_file=_path_env("TWINFLOW_TOKENS_FILE"),
            cors_origins=_csv_env("TWINFLOW_CORS_ORIGINS"),
            allowed_hosts=allowed,
            mcp_allowed_origins=_csv_env("TWINFLOW_MCP_ALLOWED_ORIGINS"),
            signing_key_file=_path_env("TWINFLOW_SIGNING_KEY_FILE"),
        )
        settings.validate_bind(host)
        return settings

    def validate_bind(self, host: str) -> None:
        """Refuse remote exposure without a caller token registry."""
        if not _is_loopback(host) and self.tokens_file is None:
            raise ValueError("non-loopback service binding requires TWINFLOW_TOKENS_FILE")


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.environ.get(name, "").split(",") if value.strip())


def _path_env(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None
