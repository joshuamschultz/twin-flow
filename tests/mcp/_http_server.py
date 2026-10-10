"""A real `create_app` under uvicorn on a loopback ephemeral port (T-213, COMP-124).

Import it as `from _http_server import running_service` (pytest's default `prepend`
import mode puts this directory on `sys.path`).

`running_service(tmp_path, tokens={caller_id: scopes})` writes a 0600 token registry and
an Ed25519 service signing key under `tmp_path / "secrets"` (outside the workspace), builds
the app with `ServiceSettings(tokens_file=..., signing_key_file=...)`, and serves it with
uvicorn (lifespan on) on `127.0.0.1:<ephemeral>` in a background thread. On exit the
server is asked to stop and the thread is joined, so the workspace owner lock is released
before the next test.

`RunningService.url` is the base URL (`http://127.0.0.1:<port>`); `mcp_url` is `url +
"/mcp"`; `tokens` maps each caller id to its plaintext bearer token.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

from twinflow.application.portfolio_port import PortfolioApi
from twinflow.service.app import create_app
from twinflow.service.auth import add_token
from twinflow.service.settings import ServiceSettings
from twinflow.service.signing import generate_signing_key

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
HOST = "127.0.0.1"
STARTUP_TIMEOUT_SECONDS = 20.0
SHUTDOWN_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class RunningService:
    """Where the live service listens, and the credentials to call it."""

    url: str
    tokens: dict[str, str]
    workspace_root: Path
    key_path: Path

    @property
    def mcp_url(self) -> str:
        return f"{self.url}/mcp"

    def bearer(self, caller_id: str) -> dict[str, str]:
        """The `Authorization` header for `caller_id`."""
        return {"Authorization": f"Bearer {self.tokens[caller_id]}"}


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _write_secrets(
    secrets_dir: Path, tokens: Mapping[str, Any]
) -> tuple[Path, Path, dict[str, str]]:
    secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    tokens_file = secrets_dir / "tokens.json"
    plain = {
        caller_id: add_token(tokens_file, caller_id=caller_id, scopes=list(scopes), days=None)
        for caller_id, scopes in tokens.items()
    }
    key_path = secrets_dir / "service-signing-key.pem"
    generate_signing_key(key_path)
    return tokens_file, key_path, plain


@contextmanager
def running_service(
    tmp_path: Path,
    *,
    tokens: Mapping[str, Any],
    portfolio_api: PortfolioApi | None = None,
    settings_overrides: Mapping[str, Any] | None = None,
) -> Iterator[RunningService]:
    """Serve `create_app(...)` with uvicorn on loopback for the duration of the block."""
    if not tokens:
        raise ValueError("running_service needs at least one caller token")
    tokens_file, key_path, plain = _write_secrets(tmp_path / "secrets", tokens)
    workspace_root = tmp_path / "workspace"
    options: dict[str, Any] = {
        "tokens_file": tokens_file,
        "signing_key_file": key_path,
        "calls_per_minute": 10_000,
    }
    options.update(settings_overrides or {})
    app = create_app(
        EXAMPLES,
        workspace_root=workspace_root,
        settings=ServiceSettings(**options),
        portfolio_api=portfolio_api,
    )
    port = _free_port()
    config = uvicorn.Config(
        app, host=HOST, port=port, lifespan="on", log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=f"uvicorn-{port}", daemon=True)
    thread.start()
    try:
        _wait_started(server, thread)
        yield RunningService(
            url=f"http://{HOST}:{port}",
            tokens=plain,
            workspace_root=workspace_root,
            key_path=key_path,
        )
    finally:
        server.should_exit = True
        thread.join(SHUTDOWN_TIMEOUT_SECONDS)
        if thread.is_alive():  # pragma: no cover - a hung shutdown must not pass silently
            server.force_exit = True
            thread.join(SHUTDOWN_TIMEOUT_SECONDS)
            raise RuntimeError("uvicorn did not stop")


def _wait_started(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("uvicorn exited during startup (see the log above)")
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError("uvicorn did not start in time")
        time.sleep(0.02)


__all__ = ["EXAMPLES", "RunningService", "running_service"]
