"""Signed planning inputs for builds and solves (COMP-127 inside COMP-117; REQ-184, REQ-185).

`PlanningArtifacts` is the one place that decides which planning profile and which ATCS
K-table a build or a solve uses, and it reads both through the signed artifact store at the
moment of use (nothing is cached):

* Build: the active signed profile, or the stored signed version named by digest; an
  unknown digest is `not_found`. Without a store or an active profile only loopback local
  mode may build, with the built-in default profile (`load_planning_profile("")`) recorded
  as unsigned; anywhere else the build is refused with `no_planning_profile`.
* Solve: the profile the stored problem names is verified again from the store, and the
  stored problem's profile must be that signed profile. An unsigned problem is solvable
  only in local mode. Any failure is `ArtifactSignatureInvalid` (fail closed).
* K-table: the active K-table is verified before every build and solve; none ever put is
  not an error (`k_lookup` None, TF-003 picks K itself). With TF-003 installed it is passed
  to the solver through TF-003's `as_k_lookup`.

The digests and sequences used are recorded on every problem and plan record (`Provenance`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict

from twinflow.application._planning_models import NotFound, PlanningError
from twinflow.application.artifacts import ArtifactKind, ArtifactRef, ArtifactSignatureInvalid
from twinflow.application.portfolio_port import k_lookup_for, load_k_table_model
from twinflow.erp.profile import PlanningProfile, load_planning_profile, profile_digest
from twinflow.scheduling import SchedulingProfile

__all__ = [
    "ArtifactSource",
    "KTableUse",
    "NoPlanningProfile",
    "PlanningArtifacts",
    "Provenance",
]

ModelT = TypeVar("ModelT", bound=BaseModel)

PROFILES: ArtifactKind = "planning_profiles"
K_TABLES: ArtifactKind = "k_tables"


class NoPlanningProfile(PlanningError):
    """No signed planning profile is available and the default is not allowed here."""

    code = "no_planning_profile"


class ArtifactSource(Protocol):
    """The verified reads the planning service needs; `SignedArtifactStore` is one."""

    def get(
        self, kind: ArtifactKind, digest: str, model: type[ModelT]
    ) -> tuple[ModelT, ArtifactRef]:
        """A named, verified version; `KeyError` when no stored version has `digest`."""
        ...

    def get_active(
        self, kind: ArtifactKind, model: type[ModelT]
    ) -> tuple[ModelT, ArtifactRef] | None:
        """The verified active version, or None when `kind` was never put."""
        ...


@dataclass(frozen=True)
class Provenance:
    """Which signed artifacts a problem or plan was made with (stored on its record)."""

    profile_digest: str
    profile_sequence: int | None
    profile_signed: bool
    k_table_digest: str | None = None
    k_table_sequence: int | None = None

    @classmethod
    def unsigned(cls, profile: PlanningProfile) -> Provenance:
        """An in-process profile that no signed artifact backs."""
        return cls(profile_digest(profile), None, False)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Provenance:
        """Read back the fields `record_fields` stored."""
        return cls(
            profile_digest=str(record["profile_digest"]),
            profile_sequence=record["profile_sequence"],
            profile_signed=bool(record["profile_signed"]),
            k_table_digest=record["k_table_digest"],
            k_table_sequence=record["k_table_sequence"],
        )

    def record_fields(self) -> dict[str, Any]:
        """The fields stored on problem and plan records."""
        return asdict(self)


@dataclass(frozen=True)
class KTableUse:
    """The verified active K-table at the moment of use; all None when none was ever put."""

    k_lookup: object | None = None
    digest: str | None = None
    sequence: int | None = None


class _OpaqueArtifact(BaseModel):
    """Any payload: verifies a stored K-table's signatures when TF-003 is not installed."""

    model_config = ConfigDict(extra="allow")


class PlanningArtifacts:
    """Resolve and verify the planning profile and K-table for one build or solve."""

    def __init__(self, store: ArtifactSource | None, *, local_mode: bool) -> None:
        self._store = store
        self._local_mode = local_mode

    def profile(self, digest: str | None) -> tuple[PlanningProfile, Provenance]:
        """The profile a build or validation uses, and its profile provenance."""
        if digest is not None:
            return self._named_profile(digest)
        if self._store is not None:
            active = self._store.get_active(PROFILES, PlanningProfile)
            if active is not None:
                profile, ref = active
                return profile, Provenance(ref.digest, ref.sequence, True)
        if not self._local_mode:
            raise NoPlanningProfile("No signed planning profile is active; put one first")
        default = load_planning_profile("")
        return default, Provenance.unsigned(default)

    def for_build(self, digest: str | None) -> tuple[PlanningProfile, Provenance]:
        """The build's profile, with the active K-table recorded in its provenance."""
        profile, provenance = self.profile(digest)
        return profile, self._with_k_table(provenance, self.k_table())

    def for_solve(
        self, provenance: Provenance, stored: PlanningProfile, solved: SchedulingProfile
    ) -> tuple[Provenance, object | None]:
        """Re-verify a stored problem's profile; the plan provenance and the `k_lookup`.

        `solved` is the profile inside the stored request, the one the solve runs: it must
        be the stored profile's scheduling part, which must be the signed profile.
        """
        if solved != stored.scheduling:
            raise ArtifactSignatureInvalid("the problem's request profile is not its profile")
        if provenance.profile_signed:
            self._verify_signed(provenance.profile_digest, stored)
        elif not self._local_mode:
            raise ArtifactSignatureInvalid("an unsigned problem is solvable in local mode only")
        k_table = self.k_table()
        return self._with_k_table(provenance, k_table), k_table.k_lookup

    def k_table(self) -> KTableUse:
        """The verified active K-table, or an empty use when none was ever put."""
        if self._store is None:
            return KTableUse()
        model = load_k_table_model()
        active = self._store.get_active(K_TABLES, model or _OpaqueArtifact)
        if active is None:
            return KTableUse()
        table, ref = active
        lookup = k_lookup_for(table) if model is not None else None
        return KTableUse(k_lookup=lookup, digest=ref.digest, sequence=ref.sequence)

    def _named_profile(self, digest: str) -> tuple[PlanningProfile, Provenance]:
        if self._store is None:
            if not self._local_mode:
                raise NoPlanningProfile("No signed planning profile store is configured")
            raise NotFound("No stored planning profile has that digest")
        try:
            profile, ref = self._store.get(PROFILES, digest, PlanningProfile)
        except KeyError:
            raise NotFound("No stored planning profile has that digest") from None
        return profile, Provenance(ref.digest, ref.sequence, True)

    def _verify_signed(self, digest: str, stored: PlanningProfile) -> None:
        if self._store is None:
            raise ArtifactSignatureInvalid("a signed problem needs the artifact store")
        try:
            self._store.get(PROFILES, digest, PlanningProfile)
        except KeyError:
            raise ArtifactSignatureInvalid("the problem's signed profile is gone") from None
        if profile_digest(stored) != digest:
            raise ArtifactSignatureInvalid("the problem's profile is not its signed profile")

    @staticmethod
    def _with_k_table(provenance: Provenance, k_table: KTableUse) -> Provenance:
        return replace(provenance, k_table_digest=k_table.digest, k_table_sequence=k_table.sequence)
