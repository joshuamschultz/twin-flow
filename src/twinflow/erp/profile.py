"""Planning profile: TF-002 scheduling policy, ERP mapping and simulation policy.

One TOML file holds all three (COMP-113, COMP-133). The `[erp]` table becomes `ErpPolicy`,
the `[simulation]` table becomes the TF-004 `SimulationPolicy` (D-104), and every other
top-level key goes to the unchanged TF-002 `SchedulingProfile` (D-115). Every load failure
raises `ProfileError` naming the dotted TOML path of the offending key, list items as `[i]`
and without a distribution's `kind` tag (`simulation.run_factors[2].distribution.cv`).
Error messages carry only the key path and the validation error type, never the value.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Sequence
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from twinflow.execution.policy import SimulationPolicy
from twinflow.scheduling.model import Id, SchedulingProfile, SetupFunction, WeightClass

ERP_TABLE = "erp"
SIMULATION_TABLE = "simulation"
# Tagged unions (distributions, setup terms) put the tag value in the pydantic error
# location; it names no TOML key, so error paths drop it.
_DISCRIMINATOR = "kind"

ModelT = TypeVar("ModelT", bound=BaseModel)

ShiftCrossing = Literal["forbid", "pause", "finish_unattended"]
"""TF-002 `Operation.shift_crossing` values the builder may emit."""


class ProfileError(ValueError):
    """A planning profile could not be parsed or validated; the message names the key path."""


class _Frozen(BaseModel):
    """Same contract as the TF-002 model: immutable, closed, finite floats (D-060)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ErpPolicy(_Frozen):
    """How ERP records map onto a scheduling problem (COMP-113, COMP-126).

    `shift_crossing_default` applies to every built op, `batch_shift_crossing` to ops with a
    `BatchSpec` (an oven load runs on unattended once started) (TF-005 builder-gap ruling).

    `horizon_days` is the planning window, not a wall (TF-005 review ruling A, DEV-12):
    calendars run on to the expansion cap (`calendars.expansion_days`) so work that does not
    fit the window is planned late instead of failing. `hard_horizon = true` puts the wall
    back: calendars stop at the horizon and the problem carries `horizon_end`.
    """

    class_weights: dict[Id, WeightClass] = Field(default_factory=dict)
    default_class: WeightClass = 1
    stock_class: WeightClass = 1
    rush_class: WeightClass = 4
    alternate_penalty_seconds: float = Field(default=600, ge=0)
    shortage_policy: Literal["flag", "hold"] = "flag"
    due_at: Literal["end_of_day", "start_of_day"] = "end_of_day"
    horizon_days: int = Field(default=56, ge=1, le=366)
    hard_horizon: StrictBool = False
    po_past_due_grace_seconds: float = Field(default=86_400, ge=0)
    setup_family_param: Id | None = None
    setup_functions: dict[Id, SetupFunction] = Field(default_factory=dict)
    use_overtime: StrictBool = True
    shift_crossing_default: ShiftCrossing = "pause"
    batch_shift_crossing: ShiftCrossing = "finish_unattended"


class PlanningProfile(_Frozen):
    """The TF-002 scheduling profile, ERP policy and simulation policy from one TOML file.

    `simulation` is signed with the rest (D-104, TF-005 D-126), so a profile stored before
    the table existed re-digests differently after load and is refused, never defaulted.
    """

    scheduling: SchedulingProfile
    erp: ErpPolicy
    simulation: SimulationPolicy = SimulationPolicy()


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
    simulation_data = data.pop(SIMULATION_TABLE, {})
    problems: list[str] = []
    scheduling = _validate(SchedulingProfile, data, "", problems)
    erp = _validate(ErpPolicy, erp_data, ERP_TABLE, problems)
    simulation = _validate(SimulationPolicy, simulation_data, SIMULATION_TABLE, problems)
    if scheduling is None or erp is None or simulation is None:
        raise ProfileError("invalid planning profile: " + "; ".join(problems))
    return PlanningProfile(scheduling=scheduling, erp=erp, simulation=simulation)


def profile_digest(profile: PlanningProfile) -> str:
    """Lowercase SHA-256 hex of the profile as canonical JSON (sorted keys, compact).

    ASCII-escaped like the signed artifact store's payload, so a stored profile with a
    non-ASCII id re-digests to the digest it was signed under.
    """
    canonical = json.dumps(
        profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate(model: type[ModelT], data: Any, table: str, problems: list[str]) -> ModelT | None:
    """Validate `data`; on failure append one `path: type` entry per error and return None."""
    try:
        return model.model_validate(data)
    except ValidationError as error:
        problems.extend(
            f"{_dotted_path(table, data, detail['loc'])}: {detail['type']}"
            for detail in error.errors()
        )
        return None


def _dotted_path(table: str, data: Any, loc: Sequence[int | str]) -> str:
    """Render a pydantic error location under its TOML table, e.g. `erp.horizon_days`.

    Walks the input beside the location: an index renders as `[i]`, and a name that is not
    a key of its table but is that table's `kind` value is a union tag and is dropped.
    """
    path = table
    node = data
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
            node = node[part] if isinstance(node, list) and 0 <= part < len(node) else None
            continue
        if _is_union_tag(node, part):
            continue
        path = f"{path}.{part}" if path else part
        node = node.get(part) if isinstance(node, dict) else None
    return path or "<top level>"


def _is_union_tag(node: Any, part: str) -> bool:
    """True when `part` is the tag of the tagged-union table `node`, not one of its keys."""
    return isinstance(node, dict) and part not in node and node.get(_DISCRIMINATOR) == part
