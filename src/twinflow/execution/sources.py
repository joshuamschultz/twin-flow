"""Distribution sources: the one calibration seam of the executor (COMP-132, D-092, D-093).

The executor asks a `DistributionSource` for every run factor, setup factor, breakdown
model and scripted outage. No parameter lives in executor code: the built-ins read the
signed `[simulation]` table (`ProfileSource`) or a scenario capsule (`CapsuleSource`),
and `DeterministicSource` is the zero-config default.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from twinflow.execution.policy import (
    BreakdownRule,
    Deterministic,
    Distribution,
    FactorRule,
    LogNormal,
    ScriptedOutage,
    SimulationPolicy,
)

_ONE = Deterministic(value=1.0)


@dataclass(frozen=True)
class OpContext:
    """What a factor rule matches on for one operation.

    `work_center_id` is that of the planned or chosen machine; `family` is the setup
    family, else the batch family, else None.
    """

    work_center_id: str
    machine_id: str
    family: str | None
    quantity: float


@runtime_checkable
class DistributionSource(Protocol):
    """Supplies every random model the executor uses (D-093)."""

    name: str
    digest: str

    def is_stochastic(self, shop: object) -> bool:
        """True when any draw for `shop` can differ between replications."""
        ...

    def run_factor(self, ctx: OpContext) -> Distribution:
        """Actual-over-planned run seconds for the op."""
        ...

    def setup_factor(self, ctx: OpContext) -> Distribution:
        """Actual-over-planned setup seconds for the op."""
        ...

    def breakdown(self, machine_id: str, work_center_id: str) -> BreakdownRule | None:
        """The failure model of a machine, or None when it never fails."""
        ...

    def outages(self) -> tuple[ScriptedOutage, ...]:
        """Scripted outages applied in every replication (each with its probability)."""
        ...


def _digest(parameters: Mapping[str, Any]) -> str:
    """Lowercase SHA-256 hex of the parameters as canonical JSON (sorted keys, compact)."""
    canonical = json.dumps(
        parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _lot_cv(cv: float, quantity: float) -> float:
    """Lot cv from a per-unit cv: the spread of a sum of iid unit factors (D-092).

    A lot smaller than one unit keeps the unit's cv, so the result never exceeds `cv`.
    """
    return cv / math.sqrt(max(quantity, 1.0))


class DeterministicSource:
    """Every factor is exactly 1; no breakdowns, no outages (the default, D-093)."""

    name = "deterministic"

    def __init__(self) -> None:
        self.digest = _digest({"source": self.name})

    def is_stochastic(self, shop: object) -> bool:
        """Never stochastic."""
        return False

    def run_factor(self, ctx: OpContext) -> Distribution:
        """Always 1: planned run seconds."""
        return _ONE

    def setup_factor(self, ctx: OpContext) -> Distribution:
        """Always 1: planned setup seconds."""
        return _ONE

    def breakdown(self, machine_id: str, work_center_id: str) -> BreakdownRule | None:
        """No machine fails."""
        return None

    def outages(self) -> tuple[ScriptedOutage, ...]:
        """No scripted outages."""
        return ()


def _specificity(
    rule_ids: tuple[str | None, str | None, str | None], ctx_ids: tuple[str, str, str | None]
) -> int | None:
    """Rank of a rule for a context, or None when a set id differs (D-092).

    Ids are (work center, machine, family). Machine 4 > work center and family 3 >
    work center 2 > family 1 > default 0.
    """
    if any(rule is not None and rule != ctx for rule, ctx in zip(rule_ids, ctx_ids, strict=True)):
        return None
    work_center, machine, family = (rule is not None for rule in rule_ids)
    if machine:
        return 4
    return 2 * work_center + family


def _most_specific_factor(rules: tuple[FactorRule, ...], ctx: OpContext) -> FactorRule | None:
    """The best-ranked matching rule; equal ranks keep the first declared."""
    ctx_ids = (ctx.work_center_id, ctx.machine_id, ctx.family)
    best: FactorRule | None = None
    best_rank = -1
    for rule in rules:
        rank = _specificity((rule.work_center_id, rule.machine_id, rule.family), ctx_ids)
        if rank is not None and rank > best_rank:
            best, best_rank = rule, rank
    return best


def _is_degenerate(dist: Distribution) -> bool:
    """True for a distribution that always gives the same value."""
    return isinstance(dist, Deterministic) or (isinstance(dist, LogNormal) and dist.cv == 0.0)


class ProfileSource:
    """Rules from the signed `[simulation]` table, most specific match first (D-092)."""

    name = "profile"

    def __init__(self, policy: SimulationPolicy) -> None:
        self._run_factors = policy.run_factors
        self._setup_factors = policy.setup_factors
        self._breakdowns = policy.breakdowns
        self._outages = policy.outages
        calibration = policy.model_dump(
            mode="json", include={"run_factors", "setup_factors", "breakdowns", "outages"}
        )
        self.digest = _digest({"source": self.name, **calibration})

    def is_stochastic(self, shop: object) -> bool:
        """True when any factor rule varies or any breakdown or outage exists."""
        factor_rules = self._run_factors + self._setup_factors
        varies = any(not _is_degenerate(rule.distribution) for rule in factor_rules)
        return varies or bool(self._breakdowns) or bool(self._outages)

    def run_factor(self, ctx: OpContext) -> Distribution:
        """The run factor of the most specific matching rule, else 1."""
        return _factor(self._run_factors, ctx)

    def setup_factor(self, ctx: OpContext) -> Distribution:
        """The setup factor of the most specific matching rule, else 1."""
        return _factor(self._setup_factors, ctx)

    def breakdown(self, machine_id: str, work_center_id: str) -> BreakdownRule | None:
        """The machine's rule, else its work center's, else a rule with no ids, else None."""
        ctx_ids = (work_center_id, machine_id, None)
        best: BreakdownRule | None = None
        best_rank = -1
        for rule in self._breakdowns:
            rank = _specificity((rule.work_center_id, rule.machine_id, None), ctx_ids)
            if rank is not None and rank > best_rank:
                best, best_rank = rule, rank
        return best

    def outages(self) -> tuple[ScriptedOutage, ...]:
        """The policy's scripted outages."""
        return self._outages


def _factor(rules: tuple[FactorRule, ...], ctx: OpContext) -> Distribution:
    """Resolve one factor; a unit-scope lognormal becomes the lot's lognormal (D-092)."""
    rule = _most_specific_factor(rules, ctx)
    if rule is None:
        return _ONE
    dist = rule.distribution
    if rule.scope == "unit" and isinstance(dist, LogNormal):
        return LogNormal(mean=dist.mean, cv=_lot_cv(dist.cv, ctx.quantity))
    return dist


class CapsuleSource:
    """Run variation from a scenario capsule's location `cv` (D-093).

    Reads the raw capsule `model` dict exactly as `model/compile.py` does: a location's
    own time-model `cv`, else `defaults.cycle_time_cv`, else 0. A work center that is not
    a capsule location gets the default. Setups are deterministic and nothing fails: the
    scenario translator blocks breakdowns.
    """

    name = "capsule"

    def __init__(self, capsule_model: Mapping[str, Any]) -> None:
        defaults = capsule_model.get("defaults", {})
        self._default_cv = float(defaults.get("cycle_time_cv", 0.0))
        self._cvs = {
            str(location["name"]): float(location.get("time_model", {}).get("cv", self._default_cv))
            for location in capsule_model.get("locations", [])
        }
        self.digest = _digest(
            {"source": self.name, "default_cv": self._default_cv, "location_cvs": self._cvs}
        )

    def is_stochastic(self, shop: object) -> bool:
        """True when any location, or the default for other work centers, has cv > 0."""
        return self._default_cv > 0.0 or any(cv > 0.0 for cv in self._cvs.values())

    def run_factor(self, ctx: OpContext) -> Distribution:
        """Lognormal mean 1 with the lot cv of the work center; exactly 1 when cv is 0."""
        cv = self._cvs.get(ctx.work_center_id, self._default_cv)
        if cv == 0.0:
            return _ONE
        return LogNormal(mean=1.0, cv=_lot_cv(cv, ctx.quantity))

    def setup_factor(self, ctx: OpContext) -> Distribution:
        """Always 1: capsules carry no setup variation."""
        return _ONE

    def breakdown(self, machine_id: str, work_center_id: str) -> BreakdownRule | None:
        """No machine fails."""
        return None

    def outages(self) -> tuple[ScriptedOutage, ...]:
        """No scripted outages."""
        return ()


_REGISTRY: dict[str, Callable[[SimulationPolicy], DistributionSource]] = {
    "deterministic": lambda _policy: DeterministicSource(),
    "profile": ProfileSource,
}


def resolve(policy: SimulationPolicy) -> DistributionSource:
    """The source named by `policy.distribution_source` (`capsule` is scenario-only)."""
    return _REGISTRY[policy.distribution_source](policy)
