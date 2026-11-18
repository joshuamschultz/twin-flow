"""TF-004 T-170: distribution sources, the one calibration seam (COMP-132, D-092, D-093).

API fixed by these tests (implementer conforms):

    from twinflow.execution.sources import (
        CapsuleSource, DeterministicSource, DistributionSource, OpContext, ProfileSource,
        resolve,
    )

    @dataclass(frozen=True)
    class OpContext:
        work_center_id: str     # of the planned or chosen machine
        machine_id: str
        family: str | None      # setup_family, else batch_family, else None
        quantity: float

    @runtime_checkable
    class DistributionSource(Protocol):
        name: str               # "deterministic" | "profile" | "capsule"
        digest: str             # sha256 hex (64 lowercase chars) of canonical parameters
        def is_stochastic(self, shop: object) -> bool: ...
        def run_factor(self, ctx: OpContext) -> Distribution: ...
        def setup_factor(self, ctx: OpContext) -> Distribution: ...
        def breakdown(self, machine_id: str, work_center_id: str) -> BreakdownRule | None: ...
        def outages(self) -> tuple[ScriptedOutage, ...]: ...

    `shop` is the T-171 ShopModel. In T-170 the three built-ins answer for their whole
    parameter set and do not read `shop`; these tests pass None.

    DeterministicSource(): every factor Deterministic(1.0); no breakdowns; no outages;
        is_stochastic False.
    ProfileSource(policy: SimulationPolicy): rules from policy.run_factors /
        policy.setup_factors, most specific match wins regardless of list order:
        machine > work center + family > work center > family > default (no ids);
        no matching rule gives Deterministic(1.0). A matched rule with scope="unit" and a
        LogNormal(mean, c) gives LogNormal(mean, c / sqrt(quantity)); other families ignore
        scope. breakdown(): machine rule beats work-center rule, else None. outages():
        policy.outages. is_stochastic True when any factor rule is non-degenerate
        (Deterministic and LogNormal cv=0 are degenerate) or any breakdown/outage exists.
    CapsuleSource(capsule_model: Mapping[str, Any]): `capsule_model` is the capsule's raw
        `model` dict. Per location (`name` = work center id) the run factor is lognormal
        mean 1 with cv c / sqrt(quantity), c = location time_model `cv`, else
        defaults.cycle_time_cv, else 0 (exactly as model/compile.py reads it); c == 0 is
        degenerate (always 1). Setups Deterministic(1.0); no breakdowns; no outages.
        is_stochastic True iff some location has c > 0.
    resolve(policy) -> DistributionSource by policy.distribution_source name
        ("deterministic" -> DeterministicSource, "profile" -> ProfileSource(policy)).
"""

from __future__ import annotations

import copy
import math
import pathlib
from typing import Any

import numpy as np
import pytest
import yaml

from twinflow.engine.rng import SOURCE_RUN_FACTOR, keyed_uniform
from twinflow.execution.distributions import quantile
from twinflow.execution.policy import (
    BreakdownRule,
    Deterministic,
    Distribution,
    FactorRule,
    LogNormal,
    ScriptedOutage,
    SimulationPolicy,
    Uniform,
)
from twinflow.execution.sources import (
    CapsuleSource,
    DeterministicSource,
    DistributionSource,
    OpContext,
    ProfileSource,
    resolve,
)
from twinflow.model import load_model

ROOT = pathlib.Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
CNC_SHOP = EXAMPLES / "cnc-shop" / "cnc-shop.twin.yaml"
HOME_BAKERY = EXAMPLES / "home-bakery" / "home-bakery.twin.yaml"
HMLV = EXAMPLES / "hmlv-calendar" / "hmlv-calendar.twin.yaml"


def _ctx(
    wc: str = "WC1", machine: str = "M1", family: str | None = None, qty: float = 1.0
) -> OpContext:
    return OpContext(work_center_id=wc, machine_id=machine, family=family, quantity=qty)


def _always_one(dist: Distribution) -> bool:
    return all(quantile(dist, u) == 1.0 for u in (1e-6, 0.25, 0.5, 0.75, 1 - 1e-6))


def _profile(**fields: Any) -> SimulationPolicy:
    return SimulationPolicy(distribution_source="profile", **fields)


def _det(value: float) -> FactorRule:
    return FactorRule(distribution=Deterministic(value=value))


# --- DeterministicSource ------------------------------------------------------------------


def test_deterministic_source_is_never_stochastic() -> None:
    source = DeterministicSource()
    assert isinstance(source, DistributionSource)
    assert source.name == "deterministic"
    assert source.is_stochastic(None) is False
    assert source.run_factor(_ctx(qty=40)) == Deterministic(value=1.0)
    assert source.setup_factor(_ctx(family="F1")) == Deterministic(value=1.0)
    assert source.breakdown("M1", "WC1") is None
    assert source.outages() == ()


# --- ProfileSource rule precedence ---------------------------------------------------------


PRECEDENCE_RULES = (
    FactorRule(distribution=Deterministic(value=1.01)),
    FactorRule(machine_id="M1", distribution=Deterministic(value=1.05)),
    FactorRule(family="F1", distribution=Deterministic(value=1.02)),
    FactorRule(work_center_id="WC1", distribution=Deterministic(value=1.03)),
    FactorRule(work_center_id="WC1", family="F1", distribution=Deterministic(value=1.04)),
)


@pytest.mark.parametrize(
    ("wc", "machine", "family", "expected"),
    [
        ("WC1", "M1", "F1", 1.05),  # machine beats everything
        ("WC1", "M1", None, 1.05),
        ("WC1", "M2", "F1", 1.04),  # work center + family
        ("WC1", "M2", "F2", 1.03),  # work center
        ("WC1", "M2", None, 1.03),
        ("WC2", "M3", "F1", 1.02),  # family
        ("WC2", "M3", "F2", 1.01),  # default
        ("WC2", "M3", None, 1.01),
    ],
)
@pytest.mark.parametrize("order", ["declared", "reversed"])
def test_profile_rule_precedence_is_by_specificity_not_list_order(
    wc: str, machine: str, family: str | None, expected: float, order: str
) -> None:
    rules = PRECEDENCE_RULES if order == "declared" else tuple(reversed(PRECEDENCE_RULES))
    source = ProfileSource(_profile(run_factors=rules, setup_factors=rules))
    ctx = _ctx(wc, machine, family)
    assert source.run_factor(ctx) == Deterministic(value=expected)
    assert source.setup_factor(ctx) == Deterministic(value=expected)


def test_profile_run_and_setup_rules_are_independent() -> None:
    source = ProfileSource(
        _profile(run_factors=(_det(1.2),), setup_factors=(_det(1.4),)),
    )
    assert source.run_factor(_ctx()) == Deterministic(value=1.2)
    assert source.setup_factor(_ctx()) == Deterministic(value=1.4)


def test_profile_without_a_matching_rule_is_deterministic_one() -> None:
    rule = FactorRule(work_center_id="GRIND", distribution=LogNormal(mean=1.0, cv=0.2))
    source = ProfileSource(_profile(run_factors=(rule,)))
    assert source.run_factor(_ctx("COIL", "M9")) == Deterministic(value=1.0)
    assert source.setup_factor(_ctx("GRIND", "M9")) == Deterministic(value=1.0)


# --- scope = "unit" ------------------------------------------------------------------------


@pytest.mark.parametrize(("quantity", "expected_cv"), [(1.0, 0.3), (9.0, 0.1), (100.0, 0.03)])
def test_unit_scope_lognormal_divides_cv_by_sqrt_quantity(
    quantity: float, expected_cv: float
) -> None:
    rule = FactorRule(scope="unit", distribution=LogNormal(mean=1.1, cv=0.3))
    factor = ProfileSource(_profile(run_factors=(rule,))).run_factor(_ctx(qty=quantity))
    assert isinstance(factor, LogNormal)
    assert factor.mean == pytest.approx(1.1, rel=1e-12)
    assert factor.cv == pytest.approx(expected_cv, rel=1e-12)


def test_unit_scope_lot_factor_has_the_sum_of_iid_units_spread() -> None:
    rule = FactorRule(scope="unit", distribution=LogNormal(mean=1.0, cv=0.4))
    factor = ProfileSource(_profile(run_factors=(rule,))).run_factor(_ctx(qty=16.0))
    values = np.array(
        [quantile(factor, keyed_uniform(1, 0, SOURCE_RUN_FACTOR, f"k{i}")) for i in range(50_000)]
    )
    assert float(values.mean()) == pytest.approx(1.0, rel=0.01)
    assert float(values.std(ddof=1) / values.mean()) == pytest.approx(0.1, rel=0.02)


def test_lot_scope_lognormal_ignores_quantity() -> None:
    rule = FactorRule(distribution=LogNormal(mean=1.1, cv=0.3))
    factor = ProfileSource(_profile(run_factors=(rule,))).run_factor(_ctx(qty=25.0))
    assert factor == LogNormal(mean=1.1, cv=0.3)


def test_unit_scope_is_ignored_by_non_lognormal_families() -> None:
    rule = FactorRule(scope="unit", distribution=Uniform(low=0.9, high=1.2))
    factor = ProfileSource(_profile(run_factors=(rule,))).run_factor(_ctx(qty=25.0))
    assert factor == Uniform(low=0.9, high=1.2)


# --- breakdowns, outages, stochastic flag ------------------------------------------------


REPAIR = LogNormal(mean=3600, cv=0.5)
WC_BREAKDOWN = BreakdownRule(work_center_id="COIL", mtbf_seconds=144000, repair=REPAIR)
MACHINE_BREAKDOWN = BreakdownRule(machine_id="M1", mtbf_seconds=7200, repair=REPAIR)


@pytest.mark.parametrize(
    "rules", [(WC_BREAKDOWN, MACHINE_BREAKDOWN), (MACHINE_BREAKDOWN, WC_BREAKDOWN)]
)
def test_breakdown_machine_rule_beats_work_center_rule(
    rules: tuple[BreakdownRule, BreakdownRule],
) -> None:
    source = ProfileSource(_profile(breakdowns=rules))
    assert source.breakdown("M1", "COIL") == MACHINE_BREAKDOWN
    assert source.breakdown("M2", "COIL") == WC_BREAKDOWN
    assert source.breakdown("M3", "GRIND") is None


def test_profile_outages_are_the_policy_outages() -> None:
    outages = (
        ScriptedOutage(machine_id="M1", start=3600, duration=1800),
        ScriptedOutage(machine_id="M2", start=0, duration=60, probability=0.25),
    )
    assert ProfileSource(_profile(outages=outages)).outages() == outages


@pytest.mark.parametrize(
    ("fields", "stochastic"),
    [
        ({}, False),
        ({"run_factors": (_det(1.2),)}, False),
        ({"setup_factors": (FactorRule(distribution=LogNormal(mean=1.1, cv=0.0)),)}, False),
        ({"run_factors": (FactorRule(distribution=LogNormal(mean=1.0, cv=0.15)),)}, True),
        ({"setup_factors": (FactorRule(distribution=Uniform(low=1.0, high=1.2)),)}, True),
        ({"breakdowns": (WC_BREAKDOWN,)}, True),
        ({"outages": (ScriptedOutage(machine_id="M1", start=0, duration=60),)}, True),
    ],
)
def test_profile_is_stochastic_when_any_rule_varies(
    fields: dict[str, Any], stochastic: bool
) -> None:
    source = ProfileSource(_profile(**fields))
    assert isinstance(source, DistributionSource)
    assert source.name == "profile"
    assert source.is_stochastic(None) is stochastic


# --- digest and resolve -------------------------------------------------------------------


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def test_digest_is_stable_and_parameter_sensitive() -> None:
    base = {"run_factors": (FactorRule(distribution=LogNormal(mean=1.0, cv=0.15)),)}
    a = ProfileSource(_profile(**base)).digest
    b = ProfileSource(_profile(**base)).digest
    changed = ProfileSource(
        _profile(run_factors=(FactorRule(distribution=LogNormal(mean=1.0, cv=0.16)),))
    ).digest
    moved = ProfileSource(
        _profile(setup_factors=(FactorRule(distribution=LogNormal(mean=1.0, cv=0.15)),))
    ).digest
    assert _is_sha256_hex(a)
    assert a == b
    assert len({a, changed, moved}) == 3
    assert DeterministicSource().digest == DeterministicSource().digest
    assert _is_sha256_hex(DeterministicSource().digest)
    assert DeterministicSource().digest != ProfileSource(_profile()).digest


def test_profile_digest_ignores_non_calibration_policy_fields() -> None:
    rules = {"run_factors": (FactorRule(distribution=LogNormal(mean=1.0, cv=0.15)),)}
    assert (
        ProfileSource(_profile(replications=5, **rules)).digest
        == ProfileSource(_profile(replications=50, workers=4, **rules)).digest
    )


def test_resolve_by_name() -> None:
    assert resolve(SimulationPolicy()).name == "deterministic"
    profile = _profile(run_factors=(_det(1.2),))
    source = resolve(profile)
    assert isinstance(source, ProfileSource)
    assert source.digest == ProfileSource(profile).digest


# --- CapsuleSource vs model/compile.py ------------------------------------------------------


def _capsule_model(path: pathlib.Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())["model"]


def _with_location_cvs(model: dict[str, Any], cvs: dict[str, float]) -> dict[str, Any]:
    edited = copy.deepcopy(model)
    for location in edited["locations"]:
        if location["name"] in cvs:
            location["time_model"]["cv"] = cvs[location["name"]]
    return edited


def _compiled_noise_draws(model: dict[str, Any], tmp_path: pathlib.Path) -> dict[str, float]:
    """One seeded draw of compile.py's noise per location (1.0 when it adds none)."""
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(model))
    compiled = load_model(str(path))
    draws = {}
    for location in compiled.locations:
        noise = location.time_model.params.get("noise")
        draws[location.location_id] = 1.0 if noise is None else noise(np.random.default_rng(5))
    return draws


def _source_draw(dist: Distribution) -> float:
    """The same seeded draw compile.py's lognormal noise would make for `dist`."""
    if _always_one(dist):
        return 1.0
    assert isinstance(dist, LogNormal)
    assert dist.mean == 1.0
    sigma = math.sqrt(math.log(1 + dist.cv * dist.cv))
    return float(np.random.default_rng(5).lognormal(-sigma * sigma / 2, sigma))


def _models() -> list[tuple[str, dict[str, Any]]]:
    cnc = _capsule_model(CNC_SHOP)
    names = [loc["name"] for loc in cnc["locations"]]
    no_defaults = copy.deepcopy(cnc)
    no_defaults.pop("defaults")
    return [
        ("cnc-shop", cnc),
        ("home-bakery", _capsule_model(HOME_BAKERY)),
        ("hmlv-calendar", _capsule_model(HMLV)),
        ("cnc-shop-overrides", _with_location_cvs(cnc, {names[0]: 0.35, names[1]: 0.0})),
        ("cnc-no-defaults-override", _with_location_cvs(no_defaults, {names[2]: 0.5})),
    ]


@pytest.mark.parametrize(("label", "model"), _models(), ids=[label for label, _ in _models()])
def test_capsule_source_reads_cv_exactly_as_compile_does(
    label: str, model: dict[str, Any], tmp_path: pathlib.Path
) -> None:
    expected = _compiled_noise_draws(model, tmp_path)
    source = CapsuleSource(model)
    actual = {
        wc: _source_draw(source.run_factor(_ctx(wc, f"{wc}-1", None, 1.0))) for wc in expected
    }
    assert actual == pytest.approx(expected, rel=1e-12), label


def test_capsule_source_example_capsules_are_stochastic_and_hmlv_is_not() -> None:
    assert CapsuleSource(_capsule_model(CNC_SHOP)).is_stochastic(None) is True
    assert CapsuleSource(_capsule_model(HOME_BAKERY)).is_stochastic(None) is True
    assert CapsuleSource(_capsule_model(HMLV)).is_stochastic(None) is False


def test_capsule_source_location_cv_overrides_default() -> None:
    cnc = _capsule_model(CNC_SHOP)
    first, second = (loc["name"] for loc in cnc["locations"][:2])
    source = CapsuleSource(_with_location_cvs(cnc, {first: 0.35, second: 0.0}))
    assert source.run_factor(_ctx(first, "m")) == LogNormal(mean=1.0, cv=0.35)
    assert _always_one(source.run_factor(_ctx(second, "m")))
    third = cnc["locations"][2]["name"]
    assert source.run_factor(_ctx(third, "m")) == LogNormal(mean=1.0, cv=0.2)


def test_capsule_source_lot_cv_divides_by_sqrt_quantity() -> None:
    cnc = _capsule_model(CNC_SHOP)
    wc = cnc["locations"][0]["name"]
    factor = CapsuleSource(cnc).run_factor(_ctx(wc, "m", None, 4.0))
    assert isinstance(factor, LogNormal)
    assert factor.mean == 1.0
    assert factor.cv == pytest.approx(0.1, rel=1e-12)


def test_capsule_source_has_deterministic_setups_and_no_failures() -> None:
    cnc = _capsule_model(CNC_SHOP)
    wc = cnc["locations"][0]["name"]
    source = CapsuleSource(cnc)
    assert isinstance(source, DistributionSource)
    assert source.name == "capsule"
    assert source.setup_factor(_ctx(wc, "m", "F1", 10.0)) == Deterministic(value=1.0)
    assert source.breakdown("m", wc) is None
    assert source.outages() == ()


def test_capsule_digest_is_stable_and_cv_sensitive() -> None:
    cnc = _capsule_model(CNC_SHOP)
    bumped = copy.deepcopy(cnc)
    bumped["defaults"]["cycle_time_cv"] = 0.21
    assert CapsuleSource(cnc).digest == CapsuleSource(copy.deepcopy(cnc)).digest
    assert _is_sha256_hex(CapsuleSource(cnc).digest)
    assert CapsuleSource(cnc).digest != CapsuleSource(bumped).digest


@pytest.mark.parametrize("quantity", [0.0, 0.25, 0.999])
def test_unit_scope_lot_below_one_unit_keeps_the_unit_cv(quantity: float) -> None:
    """Lot cv = c / sqrt(max(quantity, 1)): never above the rule's own cv."""
    rule = FactorRule(scope="unit", distribution=LogNormal(mean=1.1, cv=0.3))
    factor = ProfileSource(_profile(run_factors=(rule,))).run_factor(_ctx(qty=quantity))
    assert factor == LogNormal(mean=1.1, cv=0.3)


def test_capsule_lot_below_one_unit_keeps_the_location_cv() -> None:
    cnc = _capsule_model(CNC_SHOP)
    wc = cnc["locations"][0]["name"]
    assert CapsuleSource(cnc).run_factor(_ctx(wc, "m", None, 0.5)) == LogNormal(mean=1.0, cv=0.2)


def test_capsule_unknown_work_center_uses_the_default_cv() -> None:
    cnc = _capsule_model(CNC_SHOP)
    assert CapsuleSource(cnc).run_factor(_ctx("NOT-A-LOCATION", "m")) == LogNormal(mean=1.0, cv=0.2)


def test_capsule_unknown_work_center_without_a_default_is_deterministic() -> None:
    no_defaults = copy.deepcopy(_capsule_model(CNC_SHOP))
    no_defaults.pop("defaults")
    source = CapsuleSource(no_defaults)
    assert source.run_factor(_ctx("NOT-A-LOCATION", "m")) == Deterministic(value=1.0)
