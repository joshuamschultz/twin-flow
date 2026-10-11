"""`twinflow serve` startup rules (COMP-120, COMP-127 / DEV-8, REQ-170).

The runner refuses to start off loopback without a token registry or a signing key, before
uvicorn binds anything, and strips query strings from uvicorn's access log.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from twinflow.service import serve


@pytest.fixture
def no_bind(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record uvicorn.run calls instead of binding a port."""
    import uvicorn

    calls: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    for name in ("TWINFLOW_TOKENS_FILE", "TWINFLOW_SIGNING_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    return calls


def _tokens_file(path: Path) -> Path:
    path.write_text(json.dumps({"tokens": []}), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_serve_off_loopback_without_token_registry_refuses_before_binding(
    tmp_path: Path, no_bind: list[Any]
) -> None:
    with pytest.raises(ValueError, match="token registry"):
        serve.main(["--host", "0.0.0.0", "--workspace-root", str(tmp_path / "ws")])  # noqa: S104
    assert no_bind == []


def test_serve_off_loopback_without_signing_key_refuses_before_binding(
    tmp_path: Path, no_bind: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWINFLOW_TOKENS_FILE", str(_tokens_file(tmp_path / "tokens.json")))

    with pytest.raises(Exception, match="(?i)signing key"):
        serve.main(["--host", "0.0.0.0", "--workspace-root", str(tmp_path / "ws")])  # noqa: S104
    assert no_bind == []


def test_access_log_filter_drops_every_query_string() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "GET", "/mcp?access_token=secret&x=1", "1.1", 400),
        None,
    )

    assert serve._DropQueryString().filter(record) is True
    assert record.args == ("127.0.0.1:5", "GET", "/mcp", "1.1", 400)
