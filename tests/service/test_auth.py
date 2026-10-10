"""COMP-120 caller authentication: token registry, minting, CLI and middleware (T-209).

Requirements: REQ-170, REQ-172, REQ-173. Decision: D-117 (named caller tokens with
scopes replace the shared token; no shim).

Pinned contract choices (orchestrator-pinned API, plus these test-level pins):
- Registry refusal messages name the 0-based entry index as ``entry <i>`` and never
  contain the offending hash value.
- ``not_after`` must be timezone-aware ISO-8601 or null; a naive timestamp is refused.
- ``authenticate`` treats ``now < not_after`` as valid and ``now > not_after`` as expired.
- ``required_scope`` fails closed: an unknown classification raises ``ValueError``.
- Middleware error bodies are JSON ``{"code", "correlation_id", ...}`` with stable codes:
  401 -> ``unauthenticated``, query token -> ``token_in_query``,
  control characters -> ``malformed_token``.
- ``/health`` and ``/ready`` stay public; ``/api/health`` is protected (it is under /api).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from twinflow.cli.main import main as cli_main
from twinflow.service import auth
from twinflow.service.auth import (
    Caller,
    TokenRegistry,
    TokenRegistryError,
    add_token,
    mint_token,
    required_scope,
    token_sha256,
)
from twinflow.service.middleware import ServiceBoundaryMiddleware

POSIX = sys.platform != "win32"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
URLSAFE_43 = re.compile(r"^[A-Za-z0-9_-]{43}$")


# --------------------------------------------------------------------------- helpers


def _token(label: str) -> str:
    """Build fake bearer tokens at runtime so secret scanners stay quiet."""
    return "-".join(("tf005", label, "fake", "bearer", "value", "0123456789"))


def _entry(
    token: str,
    *,
    caller_id: str = "arc",
    scopes: list[str] | None = None,
    not_after: str | None = None,
) -> dict[str, Any]:
    return {
        "caller_id": caller_id,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "scopes": ["read", "plan"] if scopes is None else scopes,
        "not_after": not_after,
    }


def _write_registry(path: Path, entries: list[dict[str, Any]], *, mode: int = 0o600) -> Path:
    path.write_text(json.dumps({"tokens": entries}), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _three_entry_registry(tmp_path: Path) -> tuple[TokenRegistry, list[str]]:
    tokens = [_token("first"), _token("second"), _token("third")]
    entries = [
        _entry(tokens[0], caller_id="arc", scopes=["read"]),
        _entry(tokens[1], caller_id="planner", scopes=["read", "plan"]),
        _entry(tokens[2], caller_id="arc", scopes=["read", "plan"]),
    ]
    return TokenRegistry.load(_write_registry(tmp_path / "tokens.json", entries)), tokens


# --------------------------------------------------------------------------- minting


def test_mint_token_is_43_urlsafe_characters_and_unique() -> None:
    minted = {mint_token() for _ in range(500)}

    assert len(minted) == 500
    assert all(URLSAFE_43.fullmatch(token) for token in minted)


def test_mint_token_draws_256_bits_from_secrets_token_urlsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[int] = []
    original = auth.secrets.token_urlsafe

    def spy(nbytes: int | None = None) -> str:
        requested.append(nbytes if nbytes is not None else -1)
        return original(nbytes)

    monkeypatch.setattr(auth.secrets, "token_urlsafe", spy)

    mint_token()

    assert requested == [32]


def test_token_sha256_is_lowercase_hex_sha256_of_utf8_token() -> None:
    token = _token("hash")

    assert token_sha256(token) == hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- registry load


def test_registry_load_and_authenticate_valid_token_returns_named_caller(tmp_path: Path) -> None:
    token = _token("valid")
    path = _write_registry(tmp_path / "tokens.json", [_entry(token, scopes=["read"])])

    caller = TokenRegistry.load(path).authenticate(token, now=NOW)

    assert caller == Caller(caller_id="arc", scopes=frozenset({"read"}))


@pytest.mark.skipif(not POSIX, reason="file mode check is POSIX-only")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o602, 0o660, 0o644, 0o666, 0o700])
def test_registry_load_refuses_file_mode_beyond_0600(tmp_path: Path, mode: int) -> None:
    path = _write_registry(tmp_path / "tokens.json", [_entry(_token("mode"))], mode=mode)

    with pytest.raises(TokenRegistryError):
        TokenRegistry.load(path)


@pytest.mark.skipif(not POSIX, reason="file mode check is POSIX-only")
def test_registry_load_accepts_stricter_mode_0400(tmp_path: Path) -> None:
    token = _token("readonly")
    path = _write_registry(tmp_path / "tokens.json", [_entry(token)], mode=0o400)

    assert TokenRegistry.load(path).authenticate(token, now=NOW) is not None


@pytest.mark.parametrize(
    "bad_hash",
    [
        "A" * 64,  # uppercase hex
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "",
        hashlib.sha256(b"x").hexdigest().upper(),
    ],
)
def test_registry_load_refuses_non_64_lowercase_hex_and_names_entry_index(
    tmp_path: Path, bad_hash: str
) -> None:
    bad = {**_entry(_token("bad")), "token_sha256": bad_hash}
    path = _write_registry(tmp_path / "tokens.json", [_entry(_token("good")), bad])

    with pytest.raises(TokenRegistryError) as excinfo:
        TokenRegistry.load(path)

    assert "entry 1" in str(excinfo.value)
    if bad_hash:
        assert bad_hash not in str(excinfo.value)


def test_registry_load_refuses_duplicate_hash_without_echoing_it(tmp_path: Path) -> None:
    token = _token("dup")
    path = _write_registry(
        tmp_path / "tokens.json",
        [_entry(token, caller_id="arc"), _entry(token, caller_id="other")],
    )

    with pytest.raises(TokenRegistryError) as excinfo:
        TokenRegistry.load(path)

    assert "entry 1" in str(excinfo.value)
    assert token_sha256(token) not in str(excinfo.value)
    assert token not in str(excinfo.value)


@pytest.mark.parametrize("scopes", [["admin"], ["read", "write"], [], ["READ"]])
def test_registry_load_refuses_unknown_or_empty_scopes(tmp_path: Path, scopes: list[str]) -> None:
    path = _write_registry(
        tmp_path / "tokens.json", [_entry(_token("good")), _entry(_token("s"), scopes=scopes)]
    )

    with pytest.raises(TokenRegistryError, match="entry 1"):
        TokenRegistry.load(path)


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"tokens": {}}', '{"other": []}', '{"tokens": [42]}', ""],
)
def test_registry_load_refuses_malformed_document(tmp_path: Path, content: str) -> None:
    path = tmp_path / "tokens.json"
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)

    with pytest.raises(TokenRegistryError):
        TokenRegistry.load(path)


def test_registry_load_refuses_naive_not_after(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path / "tokens.json", [_entry(_token("naive"), not_after="2027-01-01T00:00:00")]
    )

    with pytest.raises(TokenRegistryError, match="entry 0"):
        TokenRegistry.load(path)


def test_registry_load_refuses_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TokenRegistryError):
        TokenRegistry.load(tmp_path / "absent.json")


def test_registry_error_is_a_value_error() -> None:
    assert issubclass(TokenRegistryError, ValueError)


def test_registry_allows_one_caller_to_hold_two_tokens_for_rotation(tmp_path: Path) -> None:
    old, new = _token("old"), _token("new")
    path = _write_registry(
        tmp_path / "tokens.json",
        [_entry(old, caller_id="arc"), _entry(new, caller_id="arc")],
    )
    registry = TokenRegistry.load(path)

    first = registry.authenticate(old, now=NOW)
    second = registry.authenticate(new, now=NOW)

    assert first is not None and second is not None
    assert first.caller_id == second.caller_id == "arc"


# --------------------------------------------------------------------------- authenticate


def test_authenticate_unknown_and_empty_token_return_none(tmp_path: Path) -> None:
    registry, _ = _three_entry_registry(tmp_path)

    assert registry.authenticate(_token("unknown"), now=NOW) is None
    assert registry.authenticate("", now=NOW) is None


def test_authenticate_honors_not_after(tmp_path: Path) -> None:
    token = _token("expiring")
    not_after = NOW + timedelta(days=1)
    path = _write_registry(
        tmp_path / "tokens.json", [_entry(token, not_after=not_after.isoformat())]
    )
    registry = TokenRegistry.load(path)

    assert registry.authenticate(token, now=not_after - timedelta(seconds=1)) is not None
    assert registry.authenticate(token, now=not_after + timedelta(seconds=1)) is None


def test_authenticate_does_not_accept_the_hash_itself_as_a_token(tmp_path: Path) -> None:
    token = _token("hash-as-token")
    registry = TokenRegistry.load(_write_registry(tmp_path / "tokens.json", [_entry(token)]))

    assert registry.authenticate(token_sha256(token), now=NOW) is None


@pytest.mark.parametrize("presented_index", [0, 2, None])
def test_authenticate_compares_every_entry_with_compare_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, presented_index: int | None
) -> None:
    registry, tokens = _three_entry_registry(tmp_path)
    presented = _token("miss") if presented_index is None else tokens[presented_index]
    calls: list[tuple[object, object]] = []
    original: Callable[..., bool] = auth.hmac.compare_digest

    def spy(left: Any, right: Any) -> bool:
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)

    result = registry.authenticate(presented, now=NOW)

    assert len(calls) == 3  # no early exit on hit, no short-circuit on miss
    assert (result is None) is (presented_index is None)


# --------------------------------------------------------------------------- scopes


def test_required_scope_maps_classification_to_scope() -> None:
    assert required_scope("read_only") == "read"
    assert required_scope("state_modifying") == "plan"


def test_required_scope_fails_closed_on_unknown_classification() -> None:
    with pytest.raises(ValueError):
        required_scope("destructive")


def test_caller_allows_only_held_scopes_and_is_frozen() -> None:
    reader = Caller(caller_id="arc", scopes=frozenset({"read"}))

    assert reader.allows("read") is True
    assert reader.allows("plan") is False
    with pytest.raises(AttributeError):
        reader.caller_id = "someone-else"  # type: ignore[misc]  # frozen-ness is under test


# --------------------------------------------------------------------------- add_token


@pytest.mark.skipif(not POSIX, reason="file mode check is POSIX-only")
def test_add_token_creates_0600_file_with_hash_only_and_token_authenticates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tokens.json"

    token = add_token(path, caller_id="arc", scopes=["read", "plan"], days=None, now=NOW)

    assert URLSAFE_43.fullmatch(token)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert token not in text
    assert token_sha256(token) in text
    caller = TokenRegistry.load(path).authenticate(token, now=NOW)
    assert caller == Caller(caller_id="arc", scopes=frozenset({"read", "plan"}))


def test_add_token_appends_and_keeps_existing_tokens_valid(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    first = add_token(path, caller_id="arc", scopes=["read"], days=None, now=NOW)

    second = add_token(path, caller_id="planner", scopes=["plan"], days=None, now=NOW)

    registry = TokenRegistry.load(path)
    assert first != second
    assert len(json.loads(path.read_text(encoding="utf-8"))["tokens"]) == 2
    assert registry.authenticate(first, now=NOW) == Caller("arc", frozenset({"read"}))
    assert registry.authenticate(second, now=NOW) == Caller("planner", frozenset({"plan"}))


def test_add_token_days_sets_not_after(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"

    token = add_token(path, caller_id="arc", scopes=["read"], days=30, now=NOW)

    registry = TokenRegistry.load(path)
    assert registry.authenticate(token, now=NOW + timedelta(days=29)) is not None
    assert registry.authenticate(token, now=NOW + timedelta(days=31)) is None
    (entry,) = json.loads(path.read_text(encoding="utf-8"))["tokens"]
    assert datetime.fromisoformat(entry["not_after"]) == NOW + timedelta(days=30)


def test_add_token_leaves_no_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"

    add_token(path, caller_id="arc", scopes=["read"], days=None, now=NOW)
    add_token(path, caller_id="arc", scopes=["read"], days=None, now=NOW)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["tokens.json"]


def test_add_token_refuses_unknown_scope_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"

    with pytest.raises(TokenRegistryError):
        add_token(path, caller_id="arc", scopes=["admin"], days=None, now=NOW)

    assert not path.exists()


# --------------------------------------------------------------------------- CLI


def test_cli_token_new_prints_token_once_and_stores_only_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "tokens.json"

    code = cli_main(
        [
            "admin",
            "token",
            "new",
            "--caller",
            "arc",
            "--scopes",
            "read,plan",
            "--days",
            "30",
            "--tokens-file",
            str(path),
        ]
    )

    out, err = capsys.readouterr()
    assert code == 0
    token = out.strip()
    assert URLSAFE_43.fullmatch(token)
    assert (out + err).count(token) == 1
    text = path.read_text(encoding="utf-8")
    assert token not in text
    assert token_sha256(token) in text
    caller = TokenRegistry.load(path).authenticate(token, now=datetime.now(UTC))
    assert caller == Caller("arc", frozenset({"read", "plan"}))


def test_cli_token_new_defaults_to_twinflow_tokens_file_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "env-tokens.json"
    monkeypatch.setenv("TWINFLOW_TOKENS_FILE", str(path))

    code = cli_main(["admin", "token", "new", "--caller", "arc", "--scopes", "read"])

    token = capsys.readouterr().out.strip()
    assert code == 0
    assert TokenRegistry.load(path).authenticate(token, now=datetime.now(UTC)) is not None


def test_cli_token_new_without_any_tokens_file_fails_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TWINFLOW_TOKENS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)

    code = cli_main(["admin", "token", "new", "--caller", "arc", "--scopes", "read"])

    out, _ = capsys.readouterr()
    assert code != 0
    assert out.strip() == ""
    assert list(tmp_path.iterdir()) == []


def test_cli_token_new_refuses_unknown_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "tokens.json"

    code = cli_main(
        [
            "admin",
            "token",
            "new",
            "--caller",
            "arc",
            "--scopes",
            "admin",
            "--tokens-file",
            str(path),
        ]
    )

    out, _ = capsys.readouterr()
    assert code != 0
    assert out.strip() == ""
    assert not path.exists()


# --------------------------------------------------------------------------- middleware


async def _echo_caller_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Downstream app: drains the body and echoes the caller the middleware attached."""
    while True:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body", False):
            break
    caller = scope.get("state", {}).get("caller")
    payload = (
        None if caller is None else {"caller_id": caller.caller_id, "scopes": sorted(caller.scopes)}
    )
    body = json.dumps({"caller": payload}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _client(registry: TokenRegistry | None) -> TestClient:
    return TestClient(
        ServiceBoundaryMiddleware(
            _echo_caller_app,
            registry=registry,
            max_request_bytes=5_242_880,
            max_mcp_request_bytes=16_777_216,
        )
    )


@pytest.fixture
def registry_and_tokens(tmp_path: Path) -> tuple[TokenRegistry, dict[str, str]]:
    tokens = {"valid": _token("mw-valid"), "expired": _token("mw-expired")}
    entries = [
        _entry(tokens["valid"], caller_id="arc", scopes=["read"]),
        _entry(tokens["expired"], caller_id="old", not_after="2000-01-01T00:00:00+00:00"),
    ]
    return TokenRegistry.load(_write_registry(tmp_path / "tokens.json", entries)), tokens


PROTECTED = ["/api/health", "/api/workspace/schema", "/mcp", "/mcp/", "/openapi.json"]
PROTECTED += ["/docs", "/redoc"]


def _assert_error(response: Any, status: int, code: str, *, secret: str | None = None) -> None:
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert body["correlation_id"] == response.headers["x-correlation-id"]
    assert len(response.content) < 512
    assert "Traceback" not in response.text
    if secret is not None:
        assert secret not in response.text
        assert token_sha256(secret) not in response.text


@pytest.mark.parametrize("path", PROTECTED)
def test_middleware_missing_token_is_401_with_bearer_challenge(
    registry_and_tokens: tuple[TokenRegistry, dict[str, str]], path: str
) -> None:
    registry, _ = registry_and_tokens

    response = _client(registry).get(path)

    _assert_error(response, 401, "unauthenticated")
    assert response.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    "header",
    [
        "Bearer " + _token("unknown"),
        "Basic " + _token("unknown"),
        "Bearer",
        "Bearer ",
        _token("no-scheme"),
    ],
)
def test_middleware_unknown_or_malformed_scheme_is_401(
    registry_and_tokens: tuple[TokenRegistry, dict[str, str]], header: str
) -> None:
    registry, _ = registry_and_tokens

    response = _client(registry).get("/mcp", headers={"Authorization": header})

    _assert_error(response, 401, "unauthenticated", secret=_token("unknown"))
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_middleware_expired_token_is_401(
    registry_and_tokens: tuple[TokenRegistry, dict[str, str]],
) -> None:
    registry, tokens = registry_and_tokens

    response = _client(registry).get(
        "/api/health", headers={"Authorization": f"Bearer {tokens['expired']}"}
    )

    _assert_error(response, 401, "unauthenticated", secret=tokens["expired"])
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_middleware_valid_token_sets_caller_in_scope_state(
    registry_and_tokens: tuple[TokenRegistry, dict[str, str]],
) -> None:
    registry, tokens = registry_and_tokens

    response = _client(registry).get("/mcp", headers={"Authorization": f"Bearer {tokens['valid']}"})

    assert response.status_code == 200
    assert response.json() == {"caller": {"caller_id": "arc", "scopes": ["read"]}}


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_middleware_probes_stay_public(
    registry_and_tokens: tuple[TokenRegistry, dict[str, str]], path: str
) -> None:
    registry, _ = registry_and_tokens

    assert _client(registry).get(path).status_code == 200


@pytest.mark.parametrize("parameter", ["access_token", "token"])
def test_middleware_rejects_token_in_query_even_with_valid_header(
    registry_and_tokens: tuple[TokenRegistry, dict[str, str]], parameter: str
) -> None:
    registry, tokens = registry_and_tokens

    response = _client(registry).get(
        f"/mcp?{parameter}={tokens['valid']}",
        headers={"Authorization": f"Bearer {tokens['valid']}"},
    )

    _assert_error(response, 400, "token_in_query", secret=tokens["valid"])


@pytest.mark.parametrize("bad_char", ["\x00", "\x01", "\t", "\x1f", "\x7f"])
def test_middleware_rejects_token_with_control_characters(
    registry_and_tokens: tuple[TokenRegistry, dict[str, str]], bad_char: str
) -> None:
    registry, tokens = registry_and_tokens
    poisoned = tokens["valid"][:10] + bad_char + tokens["valid"][10:]

    response = _client(registry).get("/mcp", headers={"Authorization": f"Bearer {poisoned}"})

    _assert_error(response, 400, "malformed_token", secret=tokens["valid"])


def test_middleware_without_registry_is_open_local_mode_with_local_caller() -> None:
    response = _client(None).get("/mcp")

    assert response.status_code == 200
    assert response.json() == {"caller": {"caller_id": "local", "scopes": ["plan", "read"]}}


def test_middleware_closes_websocket_scopes_without_reaching_the_app(
    registry_and_tokens: tuple[TokenRegistry, dict[str, str]],
) -> None:
    """Only `http` is authenticated, so any other non-lifespan scope fails closed."""
    reached: list[str] = []

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["type"])

    middleware = ServiceBoundaryMiddleware(
        downstream,
        registry=registry_and_tokens[0],
        max_request_bytes=5_242_880,
        max_mcp_request_bytes=16_777_216,
    )
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "websocket", "path": "/mcp", "headers": [], "query_string": b""}
    asyncio.run(middleware(scope, receive, send))

    assert reached == []
    assert sent == [{"type": "websocket.close", "code": 1008}]
