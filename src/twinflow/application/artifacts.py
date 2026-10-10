"""Signed planning artifacts in the workspace repository (COMP-127, D-126, D-127).

The planning profile and the ATCS K-table are stored as signed records with a signed active
pointer. Every read verifies, in order: the pointer's shape, key id and signature; that the
pointer's sequence equals the highest stored sequence of the kind (rollback guard); then the
record's shape, key id, signature and payload digest. Any failure raises
`ArtifactSignatureInvalid`; nothing falls back to a default or an older record, and nothing
is cached between reads.

Stored layout (pinned by tests/planning/test_artifacts.py):

* record, kind = artifact kind, id = `f"{sequence:08d}"`:
  `{"payload": canonical JSON text, "envelope": {kind, schema_version, digest, sequence,
  signed_at, key_id}, "signature": base64 Ed25519 over the canonical envelope}`.
  The envelope carries the payload digest, so its signature covers the payload.
* pointer, kind `artifact_pointers`, id = artifact kind:
  `{"pointer": {kind, digest, sequence, key_id}, "signature": base64 Ed25519}`.

Record and pointer signatures use distinct domain prefixes so one can never stand in for the
other. Records are written with ids 1..n and never deleted, so the record count is the highest
stored sequence; a gap or an extra row is tampering and is refused.

Ceiling: `put` writes the record, then moves the pointer (two repository calls). A read that
lands between them, or a crash between them, sees a record above the pointer and is refused
until the next `put` succeeds. Known gap: deleting the newest record AND restoring the older
validly signed pointer is not detectable without a monotonic counter outside the database.

This module depends only on the `Signer` Protocol, never on `twinflow.service`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol, TypeVar, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from twinflow.application.repository import WorkspaceRepository

ArtifactKind = Literal["planning_profiles", "k_tables"]
ARTIFACT_KINDS: frozenset[str] = frozenset(get_args(ArtifactKind))
POINTER_KIND = "artifact_pointers"
SCHEMA_VERSION = "twinflow.artifact/1"
_RECORD_DOMAIN = b"twinflow.artifact.record/1\n"
_POINTER_DOMAIN = b"twinflow.artifact.pointer/1\n"

ModelT = TypeVar("ModelT", bound=BaseModel)


class ArtifactSignatureInvalid(Exception):
    """A stored artifact or its active pointer failed verification (fail closed)."""

    code: ClassVar[str] = "artifact_signature_invalid"


class Signer(Protocol):
    """What the store needs from a signing key; the service key is one implementation."""

    def sign(self, data: bytes) -> bytes:
        """Return a detached signature of `data`."""
        ...

    def verify(self, data: bytes, signature: bytes) -> bool:
        """Return True only for a valid signature by this key; never raises."""
        ...

    @property
    def key_id(self) -> str:
        """Stable short identifier of the public key."""
        ...

    @property
    def public_key_raw(self) -> bytes:
        """The raw public key bytes."""
        ...


class ArtifactRef(BaseModel):
    """Identity of one stored, signed artifact version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ArtifactKind
    digest: str
    sequence: int
    key_id: str
    signed_at: AwareDatetime


class _Stored(BaseModel):
    """Strict shape of what is read back; anything else is tampering."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class _Envelope(_Stored):
    kind: str
    schema_version: str
    digest: str
    sequence: int
    signed_at: str
    key_id: str


class _Record(_Stored):
    payload: str
    envelope: _Envelope
    signature: str


class _Pointer(_Stored):
    kind: str
    digest: str
    sequence: int
    key_id: str


class _PointerRecord(_Stored):
    pointer: _Pointer
    signature: str


class SignedArtifactStore:
    """Put and read signed planning artifacts; every read verifies from storage."""

    def __init__(
        self,
        repository: WorkspaceRepository,
        signer: Signer,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._signer = signer
        self._clock = clock
        # One process owns the workspace; this orders its puts so each takes the next
        # sequence and the pointer always names the newest record.
        self._put_lock = threading.Lock()

    @property
    def key_id(self) -> str:
        """The signing key's id; every stored artifact names it."""
        return self._signer.key_id

    @property
    def public_key_raw(self) -> bytes:
        """The raw public key that verifies stored artifacts (published, never private)."""
        return self._signer.public_key_raw

    def put(self, kind: ArtifactKind, artifact: BaseModel, *, caller_id: str) -> ArtifactRef:
        """Sign `artifact` as the next sequence of `kind` and make it active.

        `caller_id` names the principal; auditing it is the calling tool's job (T-221).
        """
        _require_kind(kind)
        with self._put_lock:
            return self._put_next(kind, _canonical(artifact.model_dump(mode="json")))

    def _put_next(self, kind: ArtifactKind, payload: str) -> ArtifactRef:
        ref = ArtifactRef(
            kind=kind,
            digest=_digest(payload),
            sequence=self._repository.count(kind) + 1,
            key_id=self._signer.key_id,
            signed_at=self._clock(),
        )
        envelope = _Envelope(
            kind=kind,
            schema_version=SCHEMA_VERSION,
            digest=ref.digest,
            sequence=ref.sequence,
            signed_at=ref.signed_at.isoformat(),
            key_id=ref.key_id,
        )
        record = _Record(
            payload=payload,
            envelope=envelope,
            signature=self._sign(_RECORD_DOMAIN, envelope),
        )
        self._repository.create(kind, _record_id(ref.sequence), record.model_dump())
        self._write_pointer(ref)
        return ref

    def get_active(
        self, kind: ArtifactKind, model: type[ModelT]
    ) -> tuple[ModelT, ArtifactRef] | None:
        """Return the verified active artifact, or None when `kind` was never put."""
        _require_kind(kind)
        stored = self._repository.count(kind)
        body = self._pointer_body(kind)
        if body is None:
            if stored == 0:
                return None
            raise ArtifactSignatureInvalid("active pointer missing while records remain")
        pointer = self._verified_pointer(kind, body)
        if pointer.sequence != stored:
            raise ArtifactSignatureInvalid("active pointer is not the newest record")
        record = self._record_body(kind, pointer.sequence)
        if record is None:
            raise ArtifactSignatureInvalid("active record missing")
        payload, ref = self._verified_record(kind, pointer.sequence, record)
        if ref.digest != pointer.digest:
            raise ArtifactSignatureInvalid("active pointer names another record")
        return model.model_validate_json(payload), ref

    def get(
        self, kind: ArtifactKind, digest: str, model: type[ModelT]
    ) -> tuple[ModelT, ArtifactRef]:
        """Return a named, verified version of `kind`; an unknown digest raises `KeyError`.

        Scans newest first (O(n) reads; n is bounded by the repository quota).
        """
        _require_kind(kind)
        for sequence in range(self._repository.count(kind), 0, -1):
            body = self._record_body(kind, sequence)
            if body is None or _claimed_digest(body) != digest:
                continue
            payload, ref = self._verified_record(kind, sequence, body)
            return model.model_validate_json(payload), ref
        raise KeyError(digest)

    def _sign(self, domain: bytes, value: _Stored) -> str:
        message = domain + _canonical(value.model_dump()).encode()
        return base64.b64encode(self._signer.sign(message)).decode("ascii")

    def _check_signature(self, domain: bytes, value: _Stored, signature: str) -> None:
        try:
            raw = base64.b64decode(signature, validate=True)
        except (binascii.Error, ValueError):
            raise ArtifactSignatureInvalid("signature is not base64") from None
        message = domain + _canonical(value.model_dump()).encode()
        if not self._signer.verify(message, raw):
            raise ArtifactSignatureInvalid("signature does not verify")

    def _check_key_id(self, key_id: str) -> None:
        if key_id != self._signer.key_id:
            raise ArtifactSignatureInvalid("signed by another key")

    def _write_pointer(self, ref: ArtifactRef) -> None:
        pointer = _Pointer(
            kind=ref.kind, digest=ref.digest, sequence=ref.sequence, key_id=ref.key_id
        )
        body = _PointerRecord(pointer=pointer, signature=self._sign(_POINTER_DOMAIN, pointer))
        try:
            self._repository.update(POINTER_KIND, ref.kind, body.model_dump())
        except KeyError:
            self._repository.create(POINTER_KIND, ref.kind, body.model_dump())

    def _pointer_body(self, kind: str) -> dict[str, Any] | None:
        try:
            return self._repository.get(POINTER_KIND, kind)
        except KeyError:
            return None

    def _record_body(self, kind: str, sequence: int) -> dict[str, Any] | None:
        try:
            return self._repository.get(kind, _record_id(sequence))
        except KeyError:
            return None

    def _verified_pointer(self, kind: str, body: dict[str, Any]) -> _Pointer:
        stored = _parse(_PointerRecord, body)
        if stored.pointer.kind != kind:
            raise ArtifactSignatureInvalid("pointer names another kind")
        self._check_key_id(stored.pointer.key_id)
        self._check_signature(_POINTER_DOMAIN, stored.pointer, stored.signature)
        return stored.pointer

    def _verified_record(
        self, kind: ArtifactKind, sequence: int, body: dict[str, Any]
    ) -> tuple[str, ArtifactRef]:
        record = _parse(_Record, body)
        envelope = record.envelope
        if envelope.kind != kind or envelope.sequence != sequence:
            raise ArtifactSignatureInvalid("record envelope does not match its row")
        self._check_key_id(envelope.key_id)
        self._check_signature(_RECORD_DOMAIN, envelope, record.signature)
        if _digest(record.payload) != envelope.digest:
            raise ArtifactSignatureInvalid("payload digest mismatch")
        ref = ArtifactRef(
            kind=kind,
            digest=envelope.digest,
            sequence=envelope.sequence,
            key_id=envelope.key_id,
            signed_at=datetime.fromisoformat(envelope.signed_at),
        )
        return record.payload, ref


def _require_kind(kind: str) -> None:
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"unknown artifact kind; expected one of {sorted(ARTIFACT_KINDS)}")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_id(sequence: int) -> str:
    return f"{sequence:08d}"


StoredT = TypeVar("StoredT", bound=_Stored)


def _parse(model: type[StoredT], body: dict[str, Any]) -> StoredT:
    try:
        return model.model_validate(body)
    except ValidationError:
        raise ArtifactSignatureInvalid("stored artifact has an unexpected shape") from None


def _claimed_digest(body: dict[str, Any]) -> object:
    envelope = body.get("envelope")
    return envelope.get("digest") if isinstance(envelope, dict) else None
