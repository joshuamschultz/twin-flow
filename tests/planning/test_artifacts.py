"""Signed planning artifacts: put, get_active, get and every tamper case (T-220, COMP-127).

Requirements: REQ-184, REQ-186. Decisions: D-126 (signed artifacts, fail closed, rollback is
a tamper case), D-127 (Ed25519 service key).

Contract pinned here (the implementation follows these tests):

    from twinflow.application.artifacts import (
        ArtifactRef, ArtifactSignatureInvalid, Signer, SignedArtifactStore,
    )

* `ArtifactSignatureInvalid(Exception)` with `code == "artifact_signature_invalid"`.
* `ArtifactRef(kind, digest, sequence, key_id, signed_at)`: frozen pydantic model.
* `Signer` Protocol: `sign(data) -> bytes`, `verify(data, signature) -> bool`, `key_id: str`,
  `public_key_raw: bytes`. The store depends only on this Protocol (a test double here).
* `SignedArtifactStore(repository: WorkspaceRepository, signer: Signer, *, clock)`, `clock()`
  returns an aware `datetime` used as `signed_at`.
* `put(kind, artifact, *, caller_id) -> ArtifactRef`, kind in {"planning_profiles",
  "k_tables"} (anything else: `ValueError`, nothing stored); `sequence` = highest stored
  sequence of that kind + 1 (first is 1); the record is stored in repository kind == `kind`
  with id `f"{sequence:08d}"`; the signed active pointer in kind "artifact_pointers" with
  id == `kind`.
* Stored layout (PINNED so the tamper tests can edit rows the way an attacker would):
  - record body: `{"payload": str, "envelope": {"kind", "schema_version", "digest",
    "sequence", "signed_at", "key_id"}, "signature": str}`;
    `payload` = canonical JSON text of `artifact.model_dump(mode="json")` (`sort_keys=True`,
    `separators=(",", ":")`); `envelope.digest` = `ref.digest` = SHA-256 hex of the payload's
    UTF-8 bytes; `signature` = standard base64 of a 64-byte Ed25519 signature.
  - pointer body: `{"pointer": {"kind", "digest", "sequence", "key_id"}, "signature": str}`.
  The exact bytes that are signed are NOT pinned (SDD: canonical JSON of envelope plus
  payload digest).
* `get_active(kind, model) -> tuple[model, ArtifactRef] | None`: None only when the kind was
  never put (no pointer and no records). Verification per SDD COMP-127: pointer signature,
  key id, pointer sequence == highest stored sequence of the kind (rollback guard), record
  signature, payload digest. Every failure raises `ArtifactSignatureInvalid`; nothing falls
  back to a default or to an older record.
* `get(kind, digest, model) -> tuple[model, ArtifactRef]` returns a NAMED record (an older
  sequence is fine), verifying its key id, signature and payload digest; an unknown digest
  raises `LookupError` (`KeyError` qualifies).

Adversarial pins beyond the listed tamper cases: the pointer deleted while records remain is
a failure (not "never put"); the record a pointer names deleted is a failure; an extra record
above the pointer's sequence is a failure; a non-base64 signature is
`ArtifactSignatureInvalid`, not a decoding error; a store with a different (rotated) key fails
closed on the old records.

Open gap NOT tested (cannot be caught by this design): deleting the newest record row AND
rewriting the pointer to the older, validly signed pointer leaves "pointer sequence ==
highest stored" true. Detecting it needs a monotonic sequence outside the database.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, ValidationError

from twinflow.application.artifacts import (
    ArtifactRef,
    ArtifactSignatureInvalid,
    SignedArtifactStore,
    Signer,
)
from twinflow.application.repository import WorkspaceRepository
from twinflow.erp.profile import ErpPolicy, PlanningProfile
from twinflow.scheduling import SchedulingProfile

PROFILES = "planning_profiles"
K_TABLES = "k_tables"
POINTERS = "artifact_pointers"
CALLER = "arc"
T0 = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC)


# --- test doubles -----------------------------------------------------------------------------


class KeySigner:
    """A `Signer` over an in-memory Ed25519 key (the store must not care where keys live)."""

    def __init__(self, key: Ed25519PrivateKey | None = None) -> None:
        self._key = key or Ed25519PrivateKey.generate()
        self._public = self._key.public_key()

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    def verify(self, data: bytes, signature: bytes) -> bool:
        try:
            self._public.verify(signature, data)
        except InvalidSignature:
            return False
        return True

    @property
    def public_key_raw(self) -> bytes:
        return self._public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @property
    def key_id(self) -> str:
        return hashlib.sha256(self.public_key_raw).hexdigest()[:16]


def as_signer(signer: KeySigner) -> Signer:
    """Typed view: mypy checks the double satisfies the Protocol structurally."""
    return signer


class Clock:
    """Each call returns the next minute after T0."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> dt.datetime:
        self.calls += 1
        return T0 + dt.timedelta(minutes=self.calls)


class KEntry(BaseModel):
    """Stand-in for a TF-003 K-table row (TF-003's KTable is not on this branch)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    work_center_id: str
    k1: float
    k2: float


class KTableStandIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    version: int
    entries: tuple[KEntry, ...]


def profile(budget: float = 17.0) -> PlanningProfile:
    return PlanningProfile(
        scheduling=SchedulingProfile(time_budget_seconds=budget),
        erp=ErpPolicy.model_validate({"rush_class": 4}),
    )


def k_table(k1: float = 1.25) -> KTableStandIn:
    return KTableStandIn(
        version=1,
        entries=(KEntry(work_center_id="coil", k1=k1, k2=0.5),),
    )


def canonical(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


# --- fixtures and row access ------------------------------------------------------------------


@pytest.fixture
def repository(tmp_path: Path) -> WorkspaceRepository:
    return WorkspaceRepository(tmp_path / "ws")


@pytest.fixture
def signer() -> KeySigner:
    return KeySigner()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(repository: WorkspaceRepository, signer: KeySigner, clock: Clock) -> SignedArtifactStore:
    return SignedArtifactStore(repository, as_signer(signer), clock=clock)


def record_id(sequence: int) -> str:
    return f"{sequence:08d}"


def read_record(repository: WorkspaceRepository, kind: str, sequence: int) -> dict[str, Any]:
    return repository.get(kind, record_id(sequence))


def write_record(
    repository: WorkspaceRepository, kind: str, sequence: int, body: dict[str, Any]
) -> None:
    repository.update(kind, record_id(sequence), body)


def edit_record(
    repository: WorkspaceRepository,
    kind: str,
    sequence: int,
    change: Callable[[dict[str, Any]], None],
) -> None:
    body = read_record(repository, kind, sequence)
    change(body)
    write_record(repository, kind, sequence, body)


def edit_pointer(
    repository: WorkspaceRepository, kind: str, change: Callable[[dict[str, Any]], None]
) -> None:
    body = repository.get(POINTERS, kind)
    change(body)
    repository.update(POINTERS, kind, body)


def raw_execute(repository: WorkspaceRepository, sql: str, *params: object) -> None:
    """Edit the sqlite file directly, the way a host-level attacker would."""
    db = sqlite3.connect(repository.path)
    try:
        with db:
            db.execute(sql, params)
    finally:
        db.close()


def flip_base64(text: str) -> str:
    raw = bytearray(base64.b64decode(text))
    raw[0] ^= 0x01
    return base64.b64encode(bytes(raw)).decode()


# --- error and model types --------------------------------------------------------------------


def test_artifact_signature_invalid_has_the_stable_code() -> None:
    assert issubclass(ArtifactSignatureInvalid, Exception)
    assert ArtifactSignatureInvalid.code == "artifact_signature_invalid"


def test_artifact_ref_is_frozen() -> None:
    ref = ArtifactRef(kind=PROFILES, digest="ab" * 32, sequence=1, key_id="0" * 16, signed_at=T0)

    with pytest.raises(ValidationError):
        ref.sequence = 2  # type: ignore[misc]  # frozen model: assignment must fail


def test_artifacts_module_does_not_import_the_service_layer() -> None:
    code = (
        "import sys, twinflow.application.artifacts\n"
        "print(any(m == 'twinflow.service' or m.startswith('twinflow.service.') "
        "for m in sys.modules))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a literal
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )

    assert out.stdout.strip() == "False"


# --- put --------------------------------------------------------------------------------------


def test_put_first_profile_returns_sequence_1_ref(
    store: SignedArtifactStore, signer: KeySigner
) -> None:
    ref = store.put(PROFILES, profile(), caller_id=CALLER)

    assert ref.kind == PROFILES
    assert ref.sequence == 1
    assert ref.key_id == signer.key_id
    assert ref.signed_at == T0 + dt.timedelta(minutes=1)
    assert ref.digest == hashlib.sha256(canonical(profile()).encode("utf-8")).hexdigest()


def test_put_stores_the_pinned_record_layout(
    store: SignedArtifactStore, repository: WorkspaceRepository, signer: KeySigner
) -> None:
    ref = store.put(PROFILES, profile(), caller_id=CALLER)

    body = read_record(repository, PROFILES, 1)

    assert set(body) == {"payload", "envelope", "signature"}
    assert body["payload"] == canonical(profile())
    envelope = body["envelope"]
    assert set(envelope) == {"kind", "schema_version", "digest", "sequence", "signed_at", "key_id"}
    assert envelope["kind"] == PROFILES
    assert envelope["digest"] == ref.digest
    assert envelope["sequence"] == 1
    assert envelope["key_id"] == signer.key_id
    assert len(base64.b64decode(body["signature"], validate=True)) == 64


def test_put_writes_a_signed_active_pointer(
    store: SignedArtifactStore, repository: WorkspaceRepository, signer: KeySigner
) -> None:
    ref = store.put(PROFILES, profile(), caller_id=CALLER)

    body = repository.get(POINTERS, PROFILES)

    assert set(body) == {"pointer", "signature"}
    assert body["pointer"] == {
        "kind": PROFILES,
        "digest": ref.digest,
        "sequence": 1,
        "key_id": signer.key_id,
    }
    assert len(base64.b64decode(body["signature"], validate=True)) == 64


def test_put_twice_assigns_the_next_sequence_and_moves_the_pointer(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(17.0), caller_id=CALLER)

    second = store.put(PROFILES, profile(18.0), caller_id=CALLER)

    assert second.sequence == 2
    assert repository.count(PROFILES) == 2
    assert repository.get(POINTERS, PROFILES)["pointer"]["sequence"] == 2
    assert repository.get(POINTERS, PROFILES)["pointer"]["digest"] == second.digest


def test_put_sequences_are_per_kind(store: SignedArtifactStore) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)
    store.put(PROFILES, profile(18.0), caller_id=CALLER)

    ref = store.put(K_TABLES, k_table(), caller_id=CALLER)

    assert ref.sequence == 1
    assert ref.kind == K_TABLES


def test_put_unknown_kind_is_refused_and_stores_nothing(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    with pytest.raises(ValueError):
        store.put("scenarios", profile(), caller_id=CALLER)  # type: ignore[arg-type]  # bad kind on purpose

    assert repository.count("scenarios") == 0
    assert repository.count(POINTERS) == 0


# --- get_active / get: round trips ------------------------------------------------------------


def test_get_active_never_put_returns_none(store: SignedArtifactStore) -> None:
    assert store.get_active(PROFILES, PlanningProfile) is None
    assert store.get_active(K_TABLES, KTableStandIn) is None


def test_get_active_round_trips_a_profile(store: SignedArtifactStore) -> None:
    ref = store.put(PROFILES, profile(), caller_id=CALLER)

    found = store.get_active(PROFILES, PlanningProfile)

    assert found is not None
    artifact, active_ref = found
    assert isinstance(artifact, PlanningProfile)
    assert artifact == profile()
    assert active_ref == ref


def test_get_active_round_trips_a_k_table(store: SignedArtifactStore) -> None:
    ref = store.put(K_TABLES, k_table(), caller_id=CALLER)

    found = store.get_active(K_TABLES, KTableStandIn)

    assert found == (k_table(), ref)


def test_missing_k_table_is_none_while_a_profile_is_active(store: SignedArtifactStore) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)

    assert store.get_active(K_TABLES, KTableStandIn) is None


def test_get_active_returns_the_latest_put(store: SignedArtifactStore) -> None:
    store.put(PROFILES, profile(17.0), caller_id=CALLER)
    second = store.put(PROFILES, profile(18.0), caller_id=CALLER)

    assert store.get_active(PROFILES, PlanningProfile) == (profile(18.0), second)


def test_get_named_digest_returns_an_older_version(store: SignedArtifactStore) -> None:
    first = store.put(PROFILES, profile(17.0), caller_id=CALLER)
    store.put(PROFILES, profile(18.0), caller_id=CALLER)

    assert store.get(PROFILES, first.digest, PlanningProfile) == (profile(17.0), first)


def test_get_unknown_digest_raises_lookup_error(store: SignedArtifactStore) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)

    with pytest.raises(LookupError):
        store.get(PROFILES, "0" * 64, PlanningProfile)


def test_a_reopened_store_reads_what_was_put(
    repository: WorkspaceRepository, signer: KeySigner, tmp_path: Path
) -> None:
    SignedArtifactStore(repository, as_signer(signer), clock=Clock()).put(
        PROFILES, profile(), caller_id=CALLER
    )
    reopened = SignedArtifactStore(
        WorkspaceRepository(tmp_path / "ws"), as_signer(signer), clock=Clock()
    )

    found = reopened.get_active(PROFILES, PlanningProfile)

    assert found is not None and found[0] == profile()


# --- tampering: every case fails closed -------------------------------------------------------


def assert_refused(store: SignedArtifactStore, kind: str = PROFILES) -> None:
    model: type[BaseModel] = PlanningProfile if kind == PROFILES else KTableStandIn
    with pytest.raises(ArtifactSignatureInvalid) as raised:
        store.get_active(kind, model)
    assert raised.value.code == "artifact_signature_invalid"


@pytest.fixture
def two_puts(store: SignedArtifactStore, repository: WorkspaceRepository) -> dict[str, Any]:
    """Two profile versions; the first version's validly signed pointer captured on the way."""
    first = store.put(PROFILES, profile(17.0), caller_id=CALLER)
    first_pointer = repository.get(POINTERS, PROFILES)
    second = store.put(PROFILES, profile(18.0), caller_id=CALLER)
    return {"first": first, "second": second, "first_pointer": first_pointer}


def test_tamper_payload_byte_changed_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(17.0), caller_id=CALLER)

    def change(body: dict[str, Any]) -> None:
        assert "17.0" in body["payload"]
        body["payload"] = body["payload"].replace("17.0", "99.0")

    edit_record(repository, PROFILES, 1, change)

    assert_refused(store)


def test_tamper_payload_with_recomputed_digests_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    # The attacker changes the payload AND rewrites both digests to match it.
    store.put(PROFILES, profile(17.0), caller_id=CALLER)
    forged = canonical(profile(99.0))
    forged_digest = hashlib.sha256(forged.encode("utf-8")).hexdigest()

    def change(body: dict[str, Any]) -> None:
        body["payload"] = forged
        body["envelope"]["digest"] = forged_digest

    edit_record(repository, PROFILES, 1, change)
    edit_pointer(repository, PROFILES, lambda b: b["pointer"].update(digest=forged_digest))

    assert_refused(store)


def test_tamper_record_signature_changed_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)

    edit_record(repository, PROFILES, 1, lambda b: b.update(signature=flip_base64(b["signature"])))

    assert_refused(store)


def test_tamper_record_signature_not_base64_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)

    edit_record(repository, PROFILES, 1, lambda b: b.update(signature="!!not base64!!"))

    assert_refused(store)


def test_tamper_envelope_digest_changed_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)

    edit_record(repository, PROFILES, 1, lambda b: b["envelope"].update(digest="0" * 64))

    assert_refused(store)


def test_tamper_envelope_sequence_changed_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)

    edit_record(repository, PROFILES, 1, lambda b: b["envelope"].update(sequence=7))

    assert_refused(store)


def test_tamper_record_key_id_swapped_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)
    other_id = KeySigner().key_id

    edit_record(repository, PROFILES, 1, lambda b: b["envelope"].update(key_id=other_id))

    assert_refused(store)


def test_tamper_pointer_key_id_swapped_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)
    other_id = KeySigner().key_id

    edit_pointer(repository, PROFILES, lambda b: b["pointer"].update(key_id=other_id))

    assert_refused(store)


@pytest.mark.parametrize("claim_our_key_id", [False, True], ids=["their-key-id", "our-key-id"])
def test_tamper_record_signed_by_another_key_is_refused(
    store: SignedArtifactStore,
    repository: WorkspaceRepository,
    signer: KeySigner,
    tmp_path: Path,
    claim_our_key_id: bool,
) -> None:
    # Same artifact, same sequence, same digest, but signed by an attacker's key.
    store.put(PROFILES, profile(), caller_id=CALLER)
    attacker_repo = WorkspaceRepository(tmp_path / "attacker")
    SignedArtifactStore(attacker_repo, as_signer(KeySigner()), clock=Clock()).put(
        PROFILES, profile(), caller_id=CALLER
    )
    forged = read_record(attacker_repo, PROFILES, 1)
    assert (
        forged["envelope"]["digest"] == read_record(repository, PROFILES, 1)["envelope"]["digest"]
    )
    if claim_our_key_id:
        forged["envelope"]["key_id"] = signer.key_id

    write_record(repository, PROFILES, 1, forged)

    assert_refused(store)


def test_tamper_pointer_and_record_both_from_another_key_are_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository, tmp_path: Path
) -> None:
    store.put(PROFILES, profile(17.0), caller_id=CALLER)
    attacker_repo = WorkspaceRepository(tmp_path / "attacker")
    SignedArtifactStore(attacker_repo, as_signer(KeySigner()), clock=Clock()).put(
        PROFILES, profile(99.0), caller_id=CALLER
    )

    write_record(repository, PROFILES, 1, read_record(attacker_repo, PROFILES, 1))
    repository.update(POINTERS, PROFILES, attacker_repo.get(POINTERS, PROFILES))

    assert_refused(store)


def test_tamper_rollback_pointer_to_older_validly_signed_sequence_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository, two_puts: dict[str, Any]
) -> None:
    repository.update(POINTERS, PROFILES, two_puts["first_pointer"])

    assert_refused(store)


def test_tamper_pointer_signature_changed_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)

    edit_pointer(repository, PROFILES, lambda b: b.update(signature=flip_base64(b["signature"])))

    assert_refused(store)


def test_tamper_pointer_sequence_edited_without_resigning_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository, two_puts: dict[str, Any]
) -> None:
    first = two_puts["first"]

    edit_pointer(
        repository,
        PROFILES,
        lambda b: b["pointer"].update(sequence=first.sequence, digest=first.digest),
    )

    assert_refused(store)


def test_tamper_pointer_deleted_while_records_remain_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    # Deleting the pointer must not turn "tampered" into "never put" (which means defaults).
    store.put(PROFILES, profile(), caller_id=CALLER)

    raw_execute(repository, "DELETE FROM records WHERE kind=? AND id=?", POINTERS, PROFILES)

    assert_refused(store)


def test_tamper_active_record_deleted_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)

    raw_execute(repository, "DELETE FROM records WHERE kind=? AND id=?", PROFILES, record_id(1))

    assert_refused(store)


def test_tamper_extra_record_above_the_pointer_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)
    copy = read_record(repository, PROFILES, 1)
    copy["envelope"]["sequence"] = 2

    repository.create(PROFILES, record_id(2), copy)

    assert_refused(store)


def test_tamper_k_table_payload_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(K_TABLES, k_table(1.25), caller_id=CALLER)

    edit_record(
        repository, K_TABLES, 1, lambda b: b.update(payload=b["payload"].replace("1.25", "9.75"))
    )

    assert_refused(store, K_TABLES)


def test_tamper_k_table_rollback_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    store.put(K_TABLES, k_table(1.25), caller_id=CALLER)
    old_pointer = repository.get(POINTERS, K_TABLES)
    store.put(K_TABLES, k_table(2.5), caller_id=CALLER)

    repository.update(POINTERS, K_TABLES, old_pointer)

    assert_refused(store, K_TABLES)


def test_rotated_key_fails_closed_on_old_records(
    repository: WorkspaceRepository, store: SignedArtifactStore
) -> None:
    store.put(PROFILES, profile(), caller_id=CALLER)
    rotated = SignedArtifactStore(repository, as_signer(KeySigner()), clock=Clock())

    with pytest.raises(ArtifactSignatureInvalid):
        rotated.get_active(PROFILES, PlanningProfile)


def test_named_get_of_a_tampered_record_is_refused(
    store: SignedArtifactStore, repository: WorkspaceRepository, two_puts: dict[str, Any]
) -> None:
    first = two_puts["first"]

    edit_record(
        repository, PROFILES, 1, lambda b: b.update(payload=b["payload"].replace("17.0", "5.0"))
    )

    with pytest.raises(ArtifactSignatureInvalid):
        store.get(PROFILES, first.digest, PlanningProfile)


def test_tamper_leaves_no_fallback_after_a_good_read(
    store: SignedArtifactStore, repository: WorkspaceRepository
) -> None:
    # A verified read earlier must not be cached and served after the row is tampered.
    store.put(PROFILES, profile(), caller_id=CALLER)
    assert store.get_active(PROFILES, PlanningProfile) is not None

    edit_record(repository, PROFILES, 1, lambda b: b.update(signature=flip_base64(b["signature"])))

    assert_refused(store)


# --- concurrent puts -------------------------------------------------------------------------


class HeldSigner(KeySigner):
    """Holds the first `sign` call (inside the first put) until released."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True

    def sign(self, data: bytes) -> bytes:
        if self._first:
            self._first = False
            self.entered.set()
            assert self.release.wait(10)
        return super().sign(data)


def test_concurrent_puts_take_distinct_sequences_and_leave_a_readable_active(
    repository: WorkspaceRepository, clock: Clock
) -> None:
    signer = HeldSigner()
    store = SignedArtifactStore(repository, as_signer(signer), clock=clock)
    refs: list[ArtifactRef] = []
    errors: list[BaseException] = []

    def put(budget: float) -> None:
        try:
            refs.append(store.put(PROFILES, profile(budget), caller_id=CALLER))
        except BaseException as exc:  # recorded and asserted below
            errors.append(exc)

    first = threading.Thread(target=put, args=(11.0,))
    second = threading.Thread(target=put, args=(22.0,))
    first.start()
    assert signer.entered.wait(10)
    second.start()
    second.join(0.3)  # time for an unserialized second put to read the same count
    signer.release.set()
    first.join(10)
    second.join(10)

    assert errors == []
    assert sorted(ref.sequence for ref in refs) == [1, 2]
    active = store.get_active(PROFILES, PlanningProfile)
    assert active is not None
    newest = max(refs, key=lambda ref: ref.sequence)
    assert active[1].digest == newest.digest
