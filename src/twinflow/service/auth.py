"""COMP-120 caller authentication: a registry of named caller tokens (D-117).

The registry is a JSON file (``TWINFLOW_TOKENS_FILE``) holding only SHA-256 hashes of
bearer tokens, each bound to a caller id, a set of scopes and an optional expiry::

    {"tokens": [{"caller_id": "arc", "token_sha256": "<64 lowercase hex>",
                 "scopes": ["read", "plan"], "not_after": "2027-01-01T00:00:00+00:00"}]}

A plaintext token exists once: when ``mint_token`` returns it to the operator. Every
refusal names the entry by index and never echoes a hash or a token.

Scale ceiling: ``authenticate`` compares the presented hash against every entry (no early
exit, so timing does not reveal which entry matched). That is linear in the entry count,
sized for tens to hundreds of service callers, not for per-person tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCOPES = frozenset({"read", "plan"})
CLASSIFICATION_SCOPES = {"read_only": "read", "state_modifying": "plan"}
_ENTRY_KEYS = frozenset({"caller_id", "token_sha256", "scopes", "not_after"})
_REQUIRED_KEYS = frozenset({"caller_id", "token_sha256", "scopes"})
_HASH = re.compile(r"^[0-9a-f]{64}$")
# Caller ids land in audit rows and log lines; a narrow alphabet keeps them inert there.
_CALLER_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_REGISTRY_BYTES = 1_048_576
_ALLOWED_MODE_BITS = 0o600


class TokenRegistryError(ValueError):
    """The token registry is unreadable, malformed, or would become so."""


@dataclass(frozen=True)
class Caller:
    """An authenticated service caller and the scopes its token grants."""

    caller_id: str
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        """True when this caller's token grants ``scope``."""
        return scope in self.scopes


LOCAL_CALLER = Caller(caller_id="local", scopes=SCOPES)


@dataclass(frozen=True)
class _Entry:
    caller: Caller
    token_sha256: str
    not_after: datetime | None


def mint_token() -> str:
    """A fresh bearer token: 256 random bits as 43 URL-safe characters."""
    return secrets.token_urlsafe(32)


def token_sha256(token: str) -> str:
    """Lowercase hex SHA-256 of the UTF-8 token; the only form the registry stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def required_scope(classification: str) -> str:
    """The scope a tool of ``classification`` needs; unknown classifications fail closed."""
    try:
        return CLASSIFICATION_SCOPES[classification]
    except KeyError:
        raise ValueError(f"unknown tool classification {classification!r}") from None


class TokenRegistry:
    """Validated, immutable view of the token registry file."""

    def __init__(self, entries: Iterable[_Entry]) -> None:
        self._entries = tuple(entries)

    @classmethod
    def load(cls, path: str | Path) -> TokenRegistry:
        """Read and validate the registry; any defect refuses the whole file."""
        return cls(_validate_entries(_read_raw_entries(Path(path))))

    def authenticate(self, token: str, *, now: datetime | None = None) -> Caller | None:
        """The caller owning ``token``, or None when it is unknown or expired.

        The presented hash is compared with every entry using ``hmac.compare_digest``;
        the comprehension never short-circuits, on a hit or on a miss.
        """
        if not token:
            return None
        presented = token_sha256(token)
        matches = [e for e in self._entries if hmac.compare_digest(presented, e.token_sha256)]
        if not matches:
            return None
        entry = matches[0]
        moment = now if now is not None else datetime.now(UTC)
        if entry.not_after is not None and moment >= entry.not_after:
            return None
        return entry.caller


def add_token(
    path: str | Path,
    *,
    caller_id: str,
    scopes: Iterable[str],
    days: int | None,
    now: datetime | None = None,
) -> str:
    """Mint a token, append its hash to the registry atomically, and return the token.

    The file is rewritten through a 0600 temporary file and ``os.replace``, so a crash
    leaves either the old or the new registry, never a partial one. Concurrent writers
    are not serialised; this is an offline operator command.
    """
    target = Path(path)
    if days is not None and days < 1:
        raise TokenRegistryError("token lifetime in days must be positive")
    moment = now if now is not None else datetime.now(UTC)
    token = mint_token()
    entry = {
        "caller_id": caller_id,
        "token_sha256": token_sha256(token),
        "scopes": sorted(set(scopes)),
        "not_after": None if days is None else (moment + timedelta(days=days)).isoformat(),
    }
    raw_entries = _read_raw_entries(target) if target.exists() else []
    raw_entries.append(entry)
    _validate_entries(raw_entries)
    _write_atomically(target, {"tokens": raw_entries})
    return token


def _read_raw_entries(path: Path) -> list[Any]:
    try:
        with path.open("rb") as handle:
            _check_mode(os.fstat(handle.fileno()).st_mode)
            raw = handle.read(_MAX_REGISTRY_BYTES + 1)
    except OSError as exc:
        raise TokenRegistryError(f"token registry cannot be read ({exc.strerror})") from None
    if len(raw) > _MAX_REGISTRY_BYTES:
        raise TokenRegistryError("token registry is larger than 1 MiB")
    try:
        document = json.loads(raw)
    except ValueError:
        raise TokenRegistryError("token registry is not valid JSON") from None
    if not isinstance(document, dict) or not isinstance(document.get("tokens"), list):
        raise TokenRegistryError('token registry must be an object with a "tokens" list')
    return list(document["tokens"])


def _check_mode(mode: int) -> None:
    if os.name == "posix" and stat.S_IMODE(mode) & ~_ALLOWED_MODE_BITS:
        raise TokenRegistryError("token registry file mode must be 0600 or stricter")


def _validate_entries(raw_entries: list[Any]) -> list[_Entry]:
    entries: list[_Entry] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_entries):
        try:
            entry = _parse_entry(raw)
            if entry.token_sha256 in seen:
                raise TokenRegistryError("duplicate token hash")
        except TokenRegistryError as exc:
            raise TokenRegistryError(f"token registry entry {index}: {exc}") from None
        seen.add(entry.token_sha256)
        entries.append(entry)
    return entries


def _parse_entry(raw: Any) -> _Entry:
    if not isinstance(raw, dict):
        raise TokenRegistryError("must be an object")
    keys = set(raw)
    if not _REQUIRED_KEYS <= keys or not keys <= _ENTRY_KEYS:
        raise TokenRegistryError(f"keys must be {sorted(_ENTRY_KEYS)} (not_after optional)")
    caller_id = raw["caller_id"]
    if not isinstance(caller_id, str) or not _CALLER_ID.fullmatch(caller_id):
        raise TokenRegistryError("caller_id must be 1-64 characters of A-Z a-z 0-9 . _ -")
    digest = raw["token_sha256"]
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise TokenRegistryError("token_sha256 must be 64 lowercase hex characters")
    return _Entry(
        caller=Caller(caller_id=caller_id, scopes=_parse_scopes(raw["scopes"])),
        token_sha256=digest,
        not_after=_parse_not_after(raw.get("not_after")),
    )


def _parse_scopes(raw: Any) -> frozenset[str]:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(s, str) and s in SCOPES for s in raw)
    ):
        raise TokenRegistryError(f"scopes must be a non-empty list drawn from {sorted(SCOPES)}")
    return frozenset(raw)


def _parse_not_after(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        moment = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise TokenRegistryError("not_after must be a timezone-aware ISO-8601 time or null")
    return moment


def _write_atomically(path: Path, document: dict[str, Any]) -> None:
    # mkstemp creates the file 0600 for the current user, before any byte is written.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tokens-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
