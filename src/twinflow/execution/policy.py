"""Simulation policy and distribution types: the `[simulation]` table (COMP-131, D-092, D-109).

Pure Pydantic types, no behaviour. A distribution in a factor field multiplies planned
seconds; in a repair field it is seconds. Every model is frozen, closed to unknown fields
and finite (the TF-002 contract, D-060), and every list is bounded (D-109).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from twinflow.policy import DispatchName
from twinflow.scheduling.model import Id, ObjectiveKind, PosFloat

MAX_RULES = 1_000
MAX_EMPIRICAL_SAMPLES = 10_000
MAX_ROBUSTNESS_DELTAS = 10
# Float sums of decimal probabilities drift (0.1 + 0.2 + 0.7 > 1.0); allow the drift only.
_PROBABILITY_SUM_TOLERANCE = 1e-9

Probability = Annotated[float, Field(ge=0, le=1)]


class _Frozen(BaseModel):
    """Same contract as the TF-002 model: immutable, closed, finite floats (D-060)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Deterministic(_Frozen):
    """A constant: `value` every draw."""

    kind: Literal["deterministic"] = "deterministic"
    value: PosFloat = 1.0


class LogNormal(_Frozen):
    """Lognormal by its own mean and coefficient of variation (cv 0 is the constant mean)."""

    kind: Literal["lognormal"] = "lognormal"
    mean: PosFloat = 1.0
    cv: float = Field(ge=0, le=3)


class Triangular(_Frozen):
    """Triangular on [low, high] with peak at `mode`."""

    kind: Literal["triangular"] = "triangular"
    low: float = Field(ge=0)
    mode: float
    high: float

    @model_validator(mode="after")
    def _ordered(self) -> Triangular:
        if not self.low <= self.mode <= self.high or self.low >= self.high:
            raise ValueError("triangular needs low <= mode <= high and low < high")
        return self


class Uniform(_Frozen):
    """Uniform on [low, high]."""

    kind: Literal["uniform"] = "uniform"
    low: float = Field(ge=0)
    high: float

    @model_validator(mode="after")
    def _ordered(self) -> Uniform:
        if self.high <= self.low:
            raise ValueError("uniform needs high > low")
        return self


class ShiftedGamma(_Frozen):
    """A fixed minimum plus a gamma tail with this mean and shape (Pinedo p.430)."""

    kind: Literal["shifted_gamma"] = "shifted_gamma"
    minimum: float = Field(ge=0)
    mean: float
    shape: float = Field(ge=0.5, le=20)

    @model_validator(mode="after")
    def _mean_above_minimum(self) -> ShiftedGamma:
        if self.mean <= self.minimum:
            raise ValueError("shifted_gamma needs mean > minimum")
        return self


class Empirical(_Frozen):
    """Observed values, drawn by linear interpolation of their sorted order."""

    kind: Literal["empirical"] = "empirical"
    samples: tuple[PosFloat, ...] = Field(min_length=1, max_length=MAX_EMPIRICAL_SAMPLES)


Distribution = Annotated[
    Deterministic | LogNormal | Triangular | Uniform | ShiftedGamma | Empirical,
    Field(discriminator="kind"),
]


class FactorRule(_Frozen):
    """A run or setup factor for the ops it matches; unset ids match anything (D-092).

    `scope = "unit"` reads a lognormal cv as per unit, so the lot cv shrinks with quantity.
    """

    work_center_id: Id | None = None
    machine_id: Id | None = None
    family: Id | None = None
    scope: Literal["lot", "unit"] = "lot"
    distribution: Distribution


class BreakdownRule(_Frozen):
    """Clock-time failures every `mtbf_seconds` on average, down for `repair` seconds (D-094)."""

    work_center_id: Id | None = None
    machine_id: Id | None = None
    mtbf_seconds: float = Field(ge=60)
    repair: Distribution


class ScriptedOutage(_Frozen):
    """A planned what-if: `machine_id` down for `duration` from `start`, per replication."""

    machine_id: Id
    start: float
    duration: PosFloat
    probability: float = Field(default=1.0, gt=0, le=1)


class SimulationPolicy(_Frozen):
    """The `[simulation]` table of the planning profile (SDD Data Model, D-109)."""

    replications: int = Field(default=20, ge=1)
    max_replications: int = Field(default=100, ge=1, le=1000)
    time_budget_seconds: float = Field(default=60, gt=0, le=300)
    confidence_level: float = Field(default=0.90, ge=0.5, le=0.999)
    workers: int = Field(default=1, ge=1, le=16)
    baseline_rule: DispatchName = "edd"
    distribution_source: Literal["deterministic", "profile"] = "deterministic"
    run_factors: tuple[FactorRule, ...] = Field(default=(), max_length=MAX_RULES)
    setup_factors: tuple[FactorRule, ...] = Field(default=(), max_length=MAX_RULES)
    breakdowns: tuple[BreakdownRule, ...] = Field(default=(), max_length=MAX_RULES)
    outages: tuple[ScriptedOutage, ...] = Field(default=(), max_length=MAX_RULES)
    robustness_deltas_seconds: tuple[PosFloat, ...] = Field(
        default=(1800, 3600, 7200, 14400, 28800), min_length=1, max_length=MAX_ROBUSTNESS_DELTAS
    )
    robustness_delta_probabilities: tuple[Probability, ...] | None = None
    robustness_probe_machines: int = Field(default=1, ge=1, le=5)
    robustness_objective: ObjectiveKind = ObjectiveKind.WEIGHTED_TARDINESS
    max_candidates: int = Field(default=12, ge=1, le=50)

    @model_validator(mode="after")
    def _replications_within_cap(self) -> SimulationPolicy:
        if self.replications > self.max_replications:
            raise ValueError("replications must not exceed max_replications")
        return self

    @model_validator(mode="after")
    def _delta_probabilities_fit(self) -> SimulationPolicy:
        probabilities = self.robustness_delta_probabilities
        if probabilities is None:
            return self
        if len(probabilities) != len(self.robustness_deltas_seconds):
            raise ValueError(
                "robustness_delta_probabilities must match robustness_deltas_seconds in length"
            )
        if sum(probabilities) > 1.0 + _PROBABILITY_SUM_TOLERANCE:
            raise ValueError("robustness_delta_probabilities must sum to at most 1")
        return self

    @model_validator(mode="after")
    def _rules_are_used(self) -> SimulationPolicy:
        """Reject a calibration the deterministic source would silently ignore."""
        has_rules = self.run_factors or self.setup_factors or self.breakdowns or self.outages
        if has_rules and self.distribution_source == "deterministic":
            raise ValueError(
                "simulation_rules_unused: run_factors, setup_factors, breakdowns and outages "
                'need distribution_source = "profile"'
            )
        return self
