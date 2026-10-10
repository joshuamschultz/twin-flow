"""The service signing key: load, custody checks and key creation (COMP-127, D-126, D-127).

The service holds one Ed25519 key (PKCS#8 PEM, `TWINFLOW_SIGNING_KEY_FILE`) to sign planning
artifacts. The key file must live outside the workspace and be readable by its owner only.
`ServiceSigner` keeps the key as a `cryptography` `Ed25519PrivateKey` object and exposes only
`sign`, `verify`, the raw public key and the key id; nothing returns private bytes, and the
signer refuses to be pickled or copied (that would export the key). Every check fails closed
with `SigningKeyError`, whose message never carries key material or file content.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import NoReturn, SupportsIndex

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# An Ed25519 PKCS#8 PEM is about 120 bytes; anything far larger is not a key file.
MAX_KEY_FILE_BYTES = 16_384


class SigningKeyError(ValueError):
    """The signing key is missing, misplaced, too permissive or not an Ed25519 private key."""


class ServiceSigner:
    """Signs and verifies with the service Ed25519 key without ever exposing private bytes."""

    __slots__ = ("_key", "_public_key_raw", "_key_id")

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        self._public_key_raw = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self._key_id = _key_id(self._public_key_raw)

    @classmethod
    def load(cls, path: Path, *, workspace_root: Path) -> ServiceSigner:
        """Load an unencrypted Ed25519 PKCS#8 PEM key from outside `workspace_root`.

        Refuses a missing file, a file whose resolved path (symlinks and `..` followed) is
        inside the resolved workspace root, a file with any group or world permission bit, and
        any PEM that is not an unencrypted Ed25519 private key.
        """
        resolved = _resolve_existing(path)
        if resolved.is_relative_to(workspace_root.resolve()):
            raise SigningKeyError("signing key file must be outside the workspace")
        return cls(_parse_ed25519(_read_owner_only(resolved)))

    @classmethod
    def require(
        cls, path: Path | None, *, workspace_root: Path, local_only: bool
    ) -> ServiceSigner | None:
        """Apply the bind-time rule: a key is optional only on loopback; a given key must load."""
        if path is None:
            if local_only:
                return None
            raise SigningKeyError("a signing key file is required off loopback")
        return cls.load(path, workspace_root=workspace_root)

    @property
    def public_key_raw(self) -> bytes:
        """The 32-byte raw Ed25519 public key."""
        return self._public_key_raw

    @property
    def key_id(self) -> str:
        """First 16 hex characters of SHA-256 over the raw public key."""
        return self._key_id

    def sign(self, data: bytes) -> bytes:
        """Return the 64-byte Ed25519 signature of `data`."""
        return self._key.sign(data)

    def verify(self, data: bytes, signature: bytes) -> bool:
        """Return True only for a valid signature by this key; never raises."""
        try:
            self._key.public_key().verify(signature, data)
        except (InvalidSignature, TypeError, ValueError):
            return False
        return True

    def __repr__(self) -> str:
        return f"ServiceSigner(key_id={self._key_id})"

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        # Pickling or copying would serialize the private key out of the service.
        raise TypeError("ServiceSigner cannot be pickled or copied")


def generate_signing_key(path: Path) -> None:
    """Write a new Ed25519 PKCS#8 PEM to `path` with mode 0600.

    Never overwrites and never follows a symlink at `path` (`O_CREAT | O_EXCL | O_NOFOLLOW`);
    an existing path raises `SigningKeyError` and is left unchanged. The mode is set
    explicitly so a permissive umask cannot widen it.
    """
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise SigningKeyError("refusing to overwrite an existing file") from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def key_id_of(path: Path) -> str:
    """Return the key id of an owner-only Ed25519 key file (for `admin signing-key new`)."""
    key = _parse_ed25519(_read_owner_only(_resolve_existing(path)))
    return _key_id(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def _key_id(public_key_raw: bytes) -> str:
    return hashlib.sha256(public_key_raw).hexdigest()[:16]


def _resolve_existing(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SigningKeyError("signing key file not found") from None


def _read_owner_only(resolved: Path) -> bytes:
    """Read a regular file through one descriptor so the mode checked is the file read."""
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise SigningKeyError("signing key file cannot be opened") from None
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise SigningKeyError("signing key path is not a regular file")
        if status.st_mode & 0o077:
            raise SigningKeyError("signing key file must not be group or world accessible")
        data = os.read(fd, MAX_KEY_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_KEY_FILE_BYTES:
        raise SigningKeyError("signing key file is too large")
    return data


def _parse_ed25519(pem: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        # `from None`: the parser's exception may quote file content.
        raise SigningKeyError("not an unencrypted Ed25519 PKCS#8 private key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningKeyError("not an unencrypted Ed25519 PKCS#8 private key")
    return key
