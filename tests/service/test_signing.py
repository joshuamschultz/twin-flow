"""Service signing key: load, custody checks, introspection, CLI (T-220, COMP-127).

Requirements: REQ-184, REQ-186. Decisions: D-126, D-127 (Ed25519 service key, held by the
service, never exported).

Contract pinned here (the implementation follows these tests):

    from twinflow.service.signing import (
        ServiceSigner, SigningKeyError, generate_signing_key,
    )

* `SigningKeyError(ValueError)`.
* `ServiceSigner.load(path: Path, *, workspace_root: Path) -> ServiceSigner` reads an
  unencrypted Ed25519 PKCS#8 PEM. It raises `SigningKeyError` when: the file is missing; the
  resolved file (symlinks followed) is inside the resolved `workspace_root`; the file mode has
  any group or world bit (`mode & 0o077 != 0`); the PEM is not an Ed25519 private key
  (other key types, garbage, encrypted). Error messages never contain key material.
* `ServiceSigner.require(path: Path | None, *, workspace_root: Path, local_only: bool)
  -> ServiceSigner | None`: no path and `local_only` -> None; no path and not `local_only`
  -> `SigningKeyError`; a given path is always loaded with every `load` check (a bad key is
  refused on loopback too).
* Public surface (non-dunder names) is exactly `sign`, `verify`, `public_key_raw`, `key_id`,
  `load`, `require`. `sign(data) -> bytes` (64-byte Ed25519 signature); `verify(data,
  signature) -> bool` (False, never raises, for any bad signature); `public_key_raw` -> 32
  raw bytes; `key_id` = first 16 hex chars of SHA-256(public_key_raw).
* Custody: the instance holds a `cryptography` `Ed25519PrivateKey` object and no raw private
  bytes anywhere in its instance state (`vars()` / `__slots__`); `repr`/`str` show the key id
  and nothing secret; the signer cannot be pickled (pickling would export the key).
* `generate_signing_key(path: Path) -> None` writes a PKCS#8 PEM with mode 0600 and refuses
  to overwrite an existing file (raises; the old file is unchanged).
* CLI: `twinflow.cli.main.main(["admin", "signing-key", "new", "--out", PATH])` returns 0,
  writes mode 0600 and prints exactly the key id on stdout; an existing PATH returns
  non-zero and leaves the file unchanged.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa

from twinflow.cli.main import main
from twinflow.service.signing import ServiceSigner, SigningKeyError, generate_signing_key

ALLOWED_PUBLIC_NAMES = {"sign", "verify", "public_key_raw", "key_id", "load", "require"}


# --- helpers -------------------------------------------------------------------------------------


def _write_pem(path: Path, pem: bytes, mode: int = 0o600) -> Path:
    path.write_bytes(pem)
    os.chmod(path, mode)
    return path


def _ed25519_pem(key: ed25519.Ed25519PrivateKey | None = None) -> bytes:
    key = key or ed25519.Ed25519PrivateKey.generate()
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _private_raw(path: Path) -> bytes:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert isinstance(key, ed25519.Ed25519PrivateKey)
    return key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )


def _public_raw(path: Path) -> bytes:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert isinstance(key, ed25519.Ed25519PrivateKey)
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _instance_state(signer: object) -> list[object]:
    """Every value held on the instance, through `__dict__` and every `__slots__` entry."""
    values: list[object] = list(getattr(signer, "__dict__", {}).values())
    for cls in type(signer).__mro__:
        for slot in getattr(cls, "__slots__", ()):
            if hasattr(signer, slot):
                values.append(getattr(signer, slot))
    return values


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def keys_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "keys"
    directory.mkdir()
    return directory


@pytest.fixture
def key_file(keys_dir: Path) -> Path:
    return _write_pem(keys_dir / "service.pem", _ed25519_pem())


@pytest.fixture
def signer(key_file: Path, workspace: Path) -> ServiceSigner:
    return ServiceSigner.load(key_file, workspace_root=workspace)


# --- the error type ----------------------------------------------------------------------------


def test_signing_key_error_is_a_value_error() -> None:
    assert issubclass(SigningKeyError, ValueError)


# --- load: happy path ----------------------------------------------------------------------------


def test_load_signer_public_key_matches_the_file(signer: ServiceSigner, key_file: Path) -> None:
    assert signer.public_key_raw == _public_raw(key_file)
    assert isinstance(signer.public_key_raw, bytes)
    assert len(signer.public_key_raw) == 32


def test_load_signer_key_id_is_first_16_hex_of_sha256_of_public_key(
    signer: ServiceSigner, key_file: Path
) -> None:
    assert signer.key_id == hashlib.sha256(_public_raw(key_file)).hexdigest()[:16]


def test_sign_produces_an_ed25519_signature_any_verifier_accepts(
    signer: ServiceSigner, key_file: Path
) -> None:
    data = b"planning profile v1"

    signature = signer.sign(data)

    assert isinstance(signature, bytes) and len(signature) == 64
    public = ed25519.Ed25519PublicKey.from_public_bytes(_public_raw(key_file))
    public.verify(signature, data)  # raises InvalidSignature on failure


def test_verify_accepts_own_signature(signer: ServiceSigner) -> None:
    assert signer.verify(b"abc", signer.sign(b"abc")) is True


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda d, s: (d + b"x", s), id="data-changed"),
        pytest.param(lambda d, s: (d, bytes([s[0] ^ 1]) + s[1:]), id="signature-bit-flipped"),
        pytest.param(lambda d, s: (d, s[:-1]), id="signature-truncated"),
        pytest.param(lambda d, s: (d, b""), id="signature-empty"),
        pytest.param(lambda d, s: (d, s + b"\x00"), id="signature-too-long"),
    ],
)
def test_verify_returns_false_never_raises_on_bad_input(
    signer: ServiceSigner, tamper: Callable[[bytes, bytes], tuple[bytes, bytes]]
) -> None:
    data = b"k-table v3"
    bad_data, bad_signature = tamper(data, signer.sign(data))

    assert signer.verify(bad_data, bad_signature) is False


def test_verify_rejects_a_signature_from_another_key(signer: ServiceSigner) -> None:
    other = ed25519.Ed25519PrivateKey.generate()

    assert signer.verify(b"data", other.sign(b"data")) is False


def test_load_accepts_owner_read_only_mode(keys_dir: Path, workspace: Path) -> None:
    path = _write_pem(keys_dir / "ro.pem", _ed25519_pem(), mode=0o400)

    assert ServiceSigner.load(path, workspace_root=workspace).key_id


# --- load: refusals ------------------------------------------------------------------------------


def test_load_refuses_a_missing_file(keys_dir: Path, workspace: Path) -> None:
    with pytest.raises(SigningKeyError):
        ServiceSigner.load(keys_dir / "absent.pem", workspace_root=workspace)


def test_load_refuses_a_key_file_inside_the_workspace(workspace: Path) -> None:
    path = _write_pem(workspace / "service.pem", _ed25519_pem())

    with pytest.raises(SigningKeyError):
        ServiceSigner.load(path, workspace_root=workspace)


def test_load_refuses_a_key_file_nested_deep_inside_the_workspace(workspace: Path) -> None:
    nested = workspace / "a" / "b"
    nested.mkdir(parents=True)
    path = _write_pem(nested / "service.pem", _ed25519_pem())

    with pytest.raises(SigningKeyError):
        ServiceSigner.load(path, workspace_root=workspace)


def test_load_refuses_a_symlink_outside_pointing_into_the_workspace(
    workspace: Path, keys_dir: Path
) -> None:
    target = _write_pem(workspace / "hidden.pem", _ed25519_pem())
    link = keys_dir / "service.pem"
    link.symlink_to(target)

    with pytest.raises(SigningKeyError):
        ServiceSigner.load(link, workspace_root=workspace)


def test_load_refuses_when_the_workspace_root_is_a_symlink_to_the_key_dir(
    keys_dir: Path, key_file: Path, tmp_path: Path
) -> None:
    workspace_link = tmp_path / "ws-link"
    workspace_link.symlink_to(keys_dir, target_is_directory=True)

    with pytest.raises(SigningKeyError):
        ServiceSigner.load(key_file, workspace_root=workspace_link)


def test_load_refuses_a_dotdot_path_that_resolves_into_the_workspace(
    workspace: Path, keys_dir: Path
) -> None:
    _write_pem(workspace / "service.pem", _ed25519_pem())
    sneaky = keys_dir / ".." / "workspace" / "service.pem"

    with pytest.raises(SigningKeyError):
        ServiceSigner.load(sneaky, workspace_root=workspace)


def test_load_accepts_a_sibling_directory_sharing_the_workspace_name_prefix(
    tmp_path: Path, workspace: Path
) -> None:
    # "workspace-keys" starts with "workspace" but is NOT inside it (no string-prefix check).
    sibling = tmp_path / "workspace-keys"
    sibling.mkdir()
    path = _write_pem(sibling / "service.pem", _ed25519_pem())

    assert ServiceSigner.load(path, workspace_root=workspace).key_id


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o606, 0o610, 0o601, 0o777])
def test_load_refuses_group_or_world_permission_bits(
    keys_dir: Path, workspace: Path, mode: int
) -> None:
    path = _write_pem(keys_dir / "loose.pem", _ed25519_pem(), mode=mode)

    with pytest.raises(SigningKeyError):
        ServiceSigner.load(path, workspace_root=workspace)


def _other_key_pems() -> list[tuple[str, bytes]]:
    pkcs8 = serialization.PrivateFormat.PKCS8
    pem = serialization.Encoding.PEM
    none = serialization.NoEncryption()
    return [
        ("rsa", rsa.generate_private_key(65537, 2048).private_bytes(pem, pkcs8, none)),
        ("ec-p256", ec.generate_private_key(ec.SECP256R1()).private_bytes(pem, pkcs8, none)),
        ("ed448", ed448.Ed448PrivateKey.generate().private_bytes(pem, pkcs8, none)),
    ]


@pytest.mark.parametrize(("name", "pem"), _other_key_pems(), ids=lambda v: str(v)[:8])
def test_load_refuses_a_non_ed25519_private_key(
    keys_dir: Path, workspace: Path, name: str, pem: bytes
) -> None:
    path = _write_pem(keys_dir / f"{name}.pem", pem)

    with pytest.raises(SigningKeyError):
        ServiceSigner.load(path, workspace_root=workspace)


def test_load_refuses_an_ed25519_public_key_file(keys_dir: Path, workspace: Path) -> None:
    public_pem = (
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    path = _write_pem(keys_dir / "public.pem", public_pem)

    with pytest.raises(SigningKeyError):
        ServiceSigner.load(path, workspace_root=workspace)


def test_load_refuses_an_encrypted_pem(keys_dir: Path, workspace: Path) -> None:
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"hunter2"),
    )
    path = _write_pem(keys_dir / "encrypted.pem", pem)

    with pytest.raises(SigningKeyError):
        ServiceSigner.load(path, workspace_root=workspace)


def test_load_refuses_garbage_without_echoing_the_file(keys_dir: Path, workspace: Path) -> None:
    canary = "CANARY-7f3a91-NOT-A-KEY"
    path = _write_pem(keys_dir / "garbage.pem", f"-----BEGIN {canary}-----\n".encode())

    with pytest.raises(SigningKeyError) as raised:
        ServiceSigner.load(path, workspace_root=workspace)

    assert canary not in str(raised.value)


def test_load_error_for_loose_mode_does_not_contain_key_material(
    keys_dir: Path, workspace: Path
) -> None:
    pem = _ed25519_pem()
    path = _write_pem(keys_dir / "loose.pem", pem, mode=0o644)
    body = b"".join(pem.splitlines()[1:-1]).decode()

    with pytest.raises(SigningKeyError) as raised:
        ServiceSigner.load(path, workspace_root=workspace)

    message = str(raised.value) + repr(raised.value)
    assert body not in message
    assert _private_raw(path).hex() not in message


# --- require: the bind-time rule -----------------------------------------------------------------


def test_require_without_path_on_loopback_returns_none(workspace: Path) -> None:
    assert ServiceSigner.require(None, workspace_root=workspace, local_only=True) is None


def test_require_without_path_off_loopback_is_refused(workspace: Path) -> None:
    with pytest.raises(SigningKeyError):
        ServiceSigner.require(None, workspace_root=workspace, local_only=False)


@pytest.mark.parametrize("local_only", [True, False])
def test_require_with_a_good_path_loads_the_signer(
    key_file: Path, workspace: Path, local_only: bool
) -> None:
    signer = ServiceSigner.require(key_file, workspace_root=workspace, local_only=local_only)

    assert signer is not None
    assert signer.public_key_raw == _public_raw(key_file)


@pytest.mark.parametrize("local_only", [True, False])
def test_require_with_a_bad_path_is_refused_even_on_loopback(
    workspace: Path, local_only: bool
) -> None:
    inside = _write_pem(workspace / "service.pem", _ed25519_pem())

    with pytest.raises(SigningKeyError):
        ServiceSigner.require(inside, workspace_root=workspace, local_only=local_only)


@pytest.mark.parametrize("local_only", [True, False])
def test_require_with_a_missing_path_is_refused_even_on_loopback(
    keys_dir: Path, workspace: Path, local_only: bool
) -> None:
    with pytest.raises(SigningKeyError):
        ServiceSigner.require(
            keys_dir / "absent.pem", workspace_root=workspace, local_only=local_only
        )


# --- custody: introspection ----------------------------------------------------------------------


def test_signer_public_surface_is_only_sign_verify_public_key_and_key_id(
    signer: ServiceSigner,
) -> None:
    public = {name for name in dir(signer) if not name.startswith("_")}

    assert public == ALLOWED_PUBLIC_NAMES


def test_signer_holds_a_private_key_object_and_no_private_bytes(
    signer: ServiceSigner, key_file: Path
) -> None:
    private_raw = _private_raw(key_file)
    state = _instance_state(signer)

    assert any(isinstance(value, ed25519.Ed25519PrivateKey) for value in state)
    for value in state:
        if isinstance(value, bytes | bytearray | memoryview):
            assert bytes(value) != private_raw
            assert private_raw not in bytes(value)
        if isinstance(value, str):
            assert private_raw.hex() not in value
            assert "PRIVATE KEY" not in value


def test_no_public_member_returns_private_bytes(signer: ServiceSigner, key_file: Path) -> None:
    private_raw = _private_raw(key_file)
    outputs: list[object] = [
        signer.public_key_raw,
        signer.key_id,
        signer.sign(b""),
        signer.sign(private_raw),
        repr(signer),
        str(signer),
        format(signer, ""),
    ]

    for output in outputs:
        if isinstance(output, bytes):
            assert output != private_raw
            assert private_raw not in output
        else:
            assert private_raw.hex() not in str(output)


def test_repr_and_str_show_only_the_key_id(signer: ServiceSigner, key_file: Path) -> None:
    pem_body = b"".join(key_file.read_bytes().splitlines()[1:-1]).decode()

    for text in (repr(signer), str(signer)):
        assert signer.key_id in text
        assert pem_body not in text
        assert signer.public_key_raw.hex() not in text
        assert _private_raw(key_file).hex() not in text


def test_signer_cannot_be_pickled(signer: ServiceSigner) -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011 - any refusal; pickling exports the key
        pickle.dumps(signer)


def test_signer_attributes_cannot_be_reassigned(signer: ServiceSigner) -> None:
    with pytest.raises((AttributeError, TypeError)):
        signer.public_key_raw = b"\x00" * 32  # type: ignore[misc]  # read-only by contract


# --- generate_signing_key ------------------------------------------------------------------------


def test_generate_signing_key_writes_a_loadable_owner_only_pem(
    keys_dir: Path, workspace: Path
) -> None:
    path = keys_dir / "new.pem"

    generate_signing_key(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert len(_private_raw(path)) == 32
    assert ServiceSigner.load(path, workspace_root=workspace).public_key_raw == _public_raw(path)


def test_generate_signing_key_gives_a_new_key_each_time(keys_dir: Path) -> None:
    generate_signing_key(keys_dir / "a.pem")
    generate_signing_key(keys_dir / "b.pem")

    assert _public_raw(keys_dir / "a.pem") != _public_raw(keys_dir / "b.pem")


def test_generate_signing_key_refuses_to_overwrite(key_file: Path) -> None:
    before = key_file.read_bytes()

    with pytest.raises((FileExistsError, SigningKeyError)):
        generate_signing_key(key_file)

    assert key_file.read_bytes() == before


def test_generate_signing_key_refuses_to_follow_a_symlink(keys_dir: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("do not clobber")
    link = keys_dir / "link.pem"
    link.symlink_to(victim)

    with pytest.raises((FileExistsError, SigningKeyError)):
        generate_signing_key(link)

    assert victim.read_text() == "do not clobber"


def test_generate_signing_key_mode_ignores_a_permissive_umask(keys_dir: Path) -> None:
    previous = os.umask(0)
    try:
        generate_signing_key(keys_dir / "umask.pem")
    finally:
        os.umask(previous)

    assert stat.S_IMODE((keys_dir / "umask.pem").stat().st_mode) == 0o600


# --- CLI: twinflow admin signing-key new ---------------------------------------------------------


def test_cli_signing_key_new_writes_0600_and_prints_only_the_key_id(
    keys_dir: Path, workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = keys_dir / "cli.pem"

    code = main(["admin", "signing-key", "new", "--out", str(path)])

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    signer = ServiceSigner.load(path, workspace_root=workspace)
    assert captured.out.strip() == signer.key_id
    assert _private_raw(path).hex() not in captured.out + captured.err
    assert "PRIVATE KEY" not in captured.out + captured.err


def test_cli_signing_key_new_refuses_an_existing_file(
    key_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = key_file.read_bytes()

    code = main(["admin", "signing-key", "new", "--out", str(key_file)])

    captured = capsys.readouterr()
    assert code != 0
    assert key_file.read_bytes() == before
    assert "PRIVATE KEY" not in captured.out + captured.err
