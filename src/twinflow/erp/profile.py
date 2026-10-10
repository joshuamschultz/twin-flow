"""Planning profile: TF-002 scheduling policy plus the ERP mapping policy (COMP-113).

One TOML file holds both halves. The `[erp]` table becomes `ErpPolicy`; every other
top-level key goes to the unchanged TF-002 `SchedulingProfile` (D-115). Every load failure
raises `ProfileError` naming the dotted TOML path of the offending key. Error messages
carry only the key path and the validation error type, never the offending value.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Sequence
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from twinflow.scheduling.model import Id, SchedulingProfile, SetupFunction, WeightClass

ERP_TABLE = "erp"

ModelT = TypeVar("ModelT", bound=BaseModel)


class ProfileError(ValueError):
    """A planning profile could not be parsed or validated; the message names the key path."""


class _Frozen(BaseModel):
    """Same contract as the TF-002 model: immutable, closed, finite floats (D-060)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ErpPolicy(_Frozen):
    """How ERP records map onto a scheduling problem (COMP-113, COMP-126)."""

    class_weights: dict[Id, WeightClass] = Field(default_factory=dict)
    default_class: WeightClass = 1
    stock_class: WeightClass = 1
    rush_class: WeightClass = 4
    alternate_penalty_seconds: float = Field(default=600, ge=0)
    shortage_policy: Literal["flag", "hold"] = "flag"
    due_at: Literal["end_of_day", "start_of_day"] = "end_of_day"
    horizon_days: int = Field(default=56, ge=1, le=366)
    po_past_due_grace_seconds: float = Field(default=86_400, ge=0)
    setup_family_param: Id | None = None
    setup_functions: dict[Id, SetupFunction] = Field(default_factory=dict)
    use_overtime: StrictBool = True


class PlanningProfile(_Frozen):
    """The TF-002 scheduling profile and the ERP policy, loaded from one TOML file."""

    scheduling: SchedulingProfile
    erp: ErpPolicy


def load_planning_profile(text: str) -> PlanningProfile:
    """Parse TOML text into a `PlanningProfile`; omitted keys take their defaults.

    Raises:
        ProfileError: the text is not valid TOML, or any key is unknown or invalid. The
            message lists each failing dotted key path with its validation error type.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ProfileError(f"planning profile is not valid TOML: {error}") from error

    erp_data = data.pop(ERP_TABLE, {})
    problems: list[str] = []
    scheduling = _validate(SchedulingProfile, data, (), problems)
    erp = _validate(ErpPolicy, erp_data, (ERP_TABLE,), problems)
    if scheduling is None or erp is None:
        raise ProfileError("invalid planning profile: " + "; ".join(problems))
    return PlanningProfile(scheduling=scheduling, erp=erp)


def profile_digest(profile: PlanningProfile) -> str:
    """Lowercase SHA-256 hex of the profile as canonical JSON (sorted keys, compact)."""
    canonical = json.dumps(
        profile.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate(
    model: type[ModelT], data: Any, prefix: tuple[str, ...], problems: list[str]
) -> ModelT | None:
    """Validate `data`; on failure append one `path: type` entry per error and return None."""
    try:
        return model.model_validate(data)
    except ValidationError as error:
        problems.extend(
            f"{_dotted_path(prefix, detail['loc'])}: {detail['type']}" for detail in error.errors()
        )
        return None


def _dotted_path(prefix: tuple[str, ...], loc: Sequence[int | str]) -> str:
    """Join a pydantic error location under its TOML table prefix, e.g. `erp.horizon_days`."""
    parts = [*prefix, *(str(part) for part in loc)]
    return ".".join(parts) if parts else "<top level>"
