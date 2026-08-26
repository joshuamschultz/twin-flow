"""COMP-017 ModelValidator — unit tests (T-028, red phase).

ModelValidator (`model/validate.py`) is the ONLY safety net (D-043's accepted cost of
collapsing seven archetypes into Location + Transform): it runs BEFORE any plan
exists and BEFORE any attempt to compile/run the model, which is the whole reason it
inspects the RAW `model.yaml` parse tree directly rather than calling
`LocationCompiler.compile()` internally — a config broken enough to crash the
compiler (e.g. an `emits` entry with no `qty` and no `scrap` block) must surface as a
`ValidationError` naming the offending path, never as an uncaught exception from
deep inside COMP-016 (D-007: fail closed on malformed input).

Per D-055 (".claude/decisions-log.md"), checks run in this fixed ORDER and every
failure is reported — never stop at the first:

    schema -> graph -> expression -> unit -> field-combination

Each `ValidationError` names the offending config path. This file commits the
concrete path-naming convention used throughout (implementer conforms):

    routing[<i>]                    one `routing` entry, e.g. "never reaches
                                     the finished part" (last step's location
                                     does not emit/scrap `routing[i].part`)
    routing[<i>].steps[<j>]         one step reference inside a routing chain
                                     -- a step naming a location that is not
                                     declared, or the step that CLOSES a cycle
                                     back to an already-visited location with
                                     no declared `max_rework` bound
    stocks[<i>]                     one `stocks` entry never consumed or
                                     produced by any location (orphan stock)
    locations[<i>]                  a whole-location field-combination
                                     violation (consumes nothing / produces
                                     nothing)
    locations[<i>].machine          a location's `machine` does not match any
                                     declared top-level `machines[*].name`
    locations[<i>].labor_skill      a location's `labor_skill` does not
                                     appear in any declared `labor.pools[*]`
                                     `skills` list
    locations[<i>].emits[<j>]       one `emits` entry whose uom differs from
                                     `consumes[0].uom` but omits `qty` (D-055
                                     rule 3, the "unit" check)
    locations[<i>].emits            a whole-location `scrap`-present-with-
                                     zero-or->1-bare-emits violation (D-055
                                     rule 2)
    locations[<i>].time_model.scale an `attribute_scaled` time model's `scale`
                                     expression naming an attribute not
                                     declared on the part in `consumes[0]`

Schema extensions this task's fixtures introduce, beyond the LOCATION schema
`test_compile.py` already committed (T-026), both additive and harmless to the
existing LocationCompiler (unknown dict keys are simply ignored by it; a raw
`scale` string is stored verbatim in `TimeModel.params` and only ever fails at
`TimeModel.sample()`, never at compile time -- structure.md's dependency
direction is respected, compile.py is not touched by this task):

    locations[i].max_rework: <int>   OPTIONAL. Declares a finite rework-retry
                                      bound on THIS location, per D-055's
                                      rework-bound rule. A cycle in a routing
                                      chain that revisits this location is
                                      legal only when this (or a model-level
                                      `max_rework`) is present.
    locations[i].time_model:
      kind: attribute_scaled
      base: <float>
      scale: <expression string>     -- the only place a config-authored
                                      expression lives in v1 (D-005/D-049);
                                      compiled through ExpressionSandbox
                                      against `consumes[0].thing`'s declared
                                      attributes (COMP-005 PartTypeRegistry).

Committed API (this test file fixes it; the implementer conforms):

    validator = ModelValidator(registry: PartTypeRegistry, sandbox: ExpressionSandbox)
    errors = validator.validate(raw_model: RawModel) -> list[ValidationError]

    validate() takes ONLY the raw model -- no plan parameter exists, because the
    validator is D-055's "runs before any plan exists" safety net.

    class ValidationError:
        .path: str       # the offending config path, per the table above
        .message: str     # human-readable detail; substring-checked by tests,
                          # never asserted verbatim (wording is not the contract)

RED-phase note (mirrors test_compile.py's precedent exactly): `ModelValidator`
currently has `__init__(self) -> None: raise NotImplementedError("T-029")` -- it
takes NO constructor arguments. Every test below constructs it as
`ModelValidator(registry, sandbox)`, the full designed 2-argument signature. At RED
that raises `TypeError` (signature mismatch -- too many positional arguments for the
stub) before Python ever reaches the stub body's `NotImplementedError` line. Both
are the RIGHT reason to fail per this task's rule: `ModelValidator` imports cleanly
from `twinflow.model.validate` today; only calling it fails, and only because
the real behavior does not exist yet.

Pure Layer 2 tests: real `RawModel` (built directly from a plain dict -- an
already-parsed shape identical to what `yaml.safe_load` would return, matching
`model/loader.py`'s own contract), real `PartTypeRegistry`, real
`ExpressionSandbox`. No mocks. Every fixture is built from a shared, otherwise-valid
baseline model so each isolates exactly ONE violation (except the "reports every
failure" class, which deliberately combines two).
"""

from __future__ import annotations

import inspect
from typing import Any

from twinflow.model.expressions import ExpressionSandbox
from twinflow.model.loader import RawModel
from twinflow.model.validate import ModelValidator, ValidationError
from twinflow.primitives.part import PartTypeRegistry

# ---------------------------------------------------------------------------
# Shared collaborators and the baseline valid model every fixture starts from.
# ---------------------------------------------------------------------------


def _registry() -> PartTypeRegistry:
    def spec(uom: str, attributes: dict[str, type] | None = None) -> dict[str, object]:
        return {"attributes": attributes or {}, "uom": uom}

    return PartTypeRegistry(
        {
            "wire": spec("ft", {"diameter": float}),
            "blank": spec("piece"),
            "glue": spec("lb"),
            "widget": spec("piece"),
            "dust": spec("lb"),
            "trimmed": spec("ft"),
            "scrap_bits": spec("ft"),
        }
    )


def _sandbox() -> ExpressionSandbox:
    return ExpressionSandbox(max_depth=10, max_length=200)


def _validator() -> ModelValidator:
    return ModelValidator(_registry(), _sandbox())


def _baseline_data() -> dict[str, Any]:
    """A fully valid model: `cutter` -> `assembler` routes to the finished
    part `widget`; every referenced machine, labor skill and stock resolves.
    Every fixture below starts from a fresh call to this function and mutates
    exactly one thing.
    """
    return {
        "part_types": {},
        "stocks": [],
        "machines": [{"name": "cutter_m"}, {"name": "asm_m"}],
        "labor": {"pools": [{"name": "floor_pool", "skills": ["cut_op", "asm_op"]}]},
        "locations": [
            {
                "name": "cutter",
                "consumes": [{"thing": "wire", "qty": 1, "uom": "ft"}],
                "emits": [{"thing": "blank", "qty": 10, "uom": "piece"}],
                "setup_key": "grp_cut",
                "time_model": {"kind": "rate_based", "rate": 5},
                "machine": "cutter_m",
                "labor_skill": "cut_op",
            },
            {
                "name": "assembler",
                "consumes": [
                    {"thing": "blank", "qty": 1, "uom": "piece"},
                    {"thing": "glue", "qty": 0.2, "uom": "lb"},
                ],
                "emits": [{"thing": "widget", "qty": 1, "uom": "piece"}],
                "setup_key": "grp_asm",
                "time_model": {"kind": "rate_based", "rate": 8},
                "machine": "asm_m",
                "labor_skill": "asm_op",
            },
        ],
        "routing": [{"part": "widget", "steps": ["cutter", "assembler"]}],
        "processes": [],
        "bom": [],
    }


def _raw(data: dict[str, Any]) -> RawModel:
    return RawModel(data)


# ---------------------------------------------------------------------------
# Criterion 8 (positive control) -- a valid model produces an empty list.
# Written first: every other fixture is "the baseline, plus one break", so
# this is the reference the rest of the file is measured against.
# ---------------------------------------------------------------------------


class TestValidModelProducesEmptyErrorList:
    def test_baseline_model_is_valid(self) -> None:
        errors = _validator().validate(_raw(_baseline_data()))

        assert errors == []

    def test_validate_return_type_is_a_list_of_validation_error(self) -> None:
        errors = _validator().validate(_raw(_baseline_data()))

        assert isinstance(errors, list)


# ---------------------------------------------------------------------------
# Contract: validate() runs with NO plan present (D-055).
# ---------------------------------------------------------------------------


class TestValidateRunsWithNoPlanPresent:
    def test_validate_signature_takes_only_the_model_no_plan_parameter(self) -> None:
        validator = _validator()
        sig = inspect.signature(validator.validate)
        params = list(sig.parameters.values())

        assert len(params) == 1, (
            "validate() must be callable with the model alone -- D-055: the "
            "validator is the only safety net and it runs BEFORE any plan exists"
        )

    def test_validate_succeeds_against_a_bare_model_no_plan_object_anywhere(self) -> None:
        # No plan, no work orders, nothing plan-shaped is constructed anywhere
        # in this test file -- this is the demonstration, not a separate check.
        errors = _validator().validate(_raw(_baseline_data()))

        assert errors == []


# ---------------------------------------------------------------------------
# Criterion 1 -- a routing graph that never reaches a finished part.
# ---------------------------------------------------------------------------


class TestRoutingNeverReachesFinishedPart:
    def test_last_step_does_not_emit_the_declared_finished_part(self) -> None:
        data = _baseline_data()
        # Drop the "assembler" step: the chain now ends at "cutter", which
        # emits "blank", never the declared finished part "widget".
        data["routing"] = [{"part": "widget", "steps": ["cutter"]}]

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert isinstance(errors[0], ValidationError)
        assert errors[0].path == "routing[0]"
        message = errors[0].message.lower()
        assert "widget" in message
        assert "reach" in message or "finished" in message


# ---------------------------------------------------------------------------
# Criterion 2 -- an orphan stock (declared but never consumed or produced).
# ---------------------------------------------------------------------------


class TestOrphanStockNeverConsumedOrProduced:
    def test_declared_stock_unreferenced_by_any_location_is_flagged(self) -> None:
        data = _baseline_data()
        data["stocks"] = [{"name": "orphan_bits", "uom": "piece"}]

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "stocks[0]"
        assert "orphan_bits" in errors[0].message


# ---------------------------------------------------------------------------
# Criterion 3 -- a missing referenced location or labor pool/skill.
# ---------------------------------------------------------------------------


class TestMissingReferencedLocationOrLaborSkill:
    def test_routing_step_names_a_location_that_is_not_declared(self) -> None:
        data = _baseline_data()
        data["routing"] = [{"part": "widget", "steps": ["cutter", "phantom_step", "assembler"]}]

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "routing[0].steps[1]"
        assert "phantom_step" in errors[0].message

    def test_location_labor_skill_is_not_declared_in_any_labor_pool(self) -> None:
        data = _baseline_data()
        data["locations"][1]["labor_skill"] = "welding_op"

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "locations[1].labor_skill"
        assert "welding_op" in errors[0].message

    def test_location_machine_is_not_declared_in_top_level_machines(self) -> None:
        """Adversarial addition beyond the listed criteria: the same 'missing
        reference' failure mode applies to `machine`, not just `labor_skill`."""
        data = _baseline_data()
        data["locations"][0]["machine"] = "phantom_machine"

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "locations[0].machine"
        assert "phantom_machine" in errors[0].message


# ---------------------------------------------------------------------------
# Criterion 4 -- an unbounded rework/scrap-destination loop (a cycle with no
# declared max_rework bound); a cycle WITH a declared bound must PASS.
# ---------------------------------------------------------------------------


class TestUnboundedReworkLoop:
    def _cyclic_data(self) -> dict[str, Any]:
        data = _baseline_data()
        # "cutter" is revisited at index 2, closing a cycle. The chain still
        # ends at "cutter", which emits "blank" -- routing[0].part is set to
        # "blank" so the *reachability* check (criterion 1) is independently
        # satisfied and cannot leak a second, unrelated error into this test.
        data["routing"] = [{"part": "blank", "steps": ["cutter", "assembler", "cutter"]}]
        return data

    def test_cycle_with_no_declared_max_rework_bound_is_rejected(self) -> None:
        data = self._cyclic_data()

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "routing[0].steps[2]"
        message = errors[0].message.lower()
        assert "cycle" in message or "rework" in message

    def test_cycle_with_declared_max_rework_on_the_looping_location_passes(self) -> None:
        data = self._cyclic_data()
        data["locations"][0]["max_rework"] = 3  # declared on "cutter", the looping location

        errors = _validator().validate(_raw(data))

        assert errors == []

    def test_cycle_with_declared_model_level_max_rework_passes(self) -> None:
        data = self._cyclic_data()
        data["max_rework"] = 3  # declared globally on the model, per D-055

        errors = _validator().validate(_raw(data))

        assert errors == []


# ---------------------------------------------------------------------------
# Criterion 5 -- an expression naming an undeclared attribute.
# ---------------------------------------------------------------------------


class TestExpressionNamesUndeclaredAttribute:
    def test_attribute_scaled_scale_expression_references_undeclared_attribute(self) -> None:
        data = _baseline_data()
        # "wire" declares only "diameter" (see _registry()); "wobble" is not
        # a declared attribute of "wire", which is consumes[0].thing here.
        data["locations"].append(
            {
                "name": "inspector",
                "consumes": [{"thing": "wire", "qty": 1, "uom": "ft"}],
                "emits": [{"thing": "blank", "qty": 1, "uom": "piece"}],
                "setup_key": "grp_insp",
                "time_model": {"kind": "attribute_scaled", "base": 2.0, "scale": "wobble * 2"},
                "machine": "cutter_m",
                "labor_skill": "cut_op",
            }
        )

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "locations[2].time_model.scale"
        assert "wobble" in errors[0].message


# ---------------------------------------------------------------------------
# Criterion 6 -- a uom change with no explicit emit spec (D-055 rule 3).
# ---------------------------------------------------------------------------


class TestUomChangeWithNoExplicitEmitSpec:
    def test_emit_uom_differs_from_basis_and_omits_qty(self) -> None:
        data = _baseline_data()
        # consumes[0].uom is "ft"; this emit's uom is "lb" (a uom change) and
        # it omits qty entirely, with no scrap block to supply a remainder.
        data["locations"].append(
            {
                "name": "duster",
                "consumes": [{"thing": "wire", "qty": 1, "uom": "ft"}],
                "emits": [{"thing": "dust", "uom": "lb"}],
                "setup_key": "grp_dust",
                "time_model": {"kind": "rate_based", "rate": 5},
                "machine": "cutter_m",
                "labor_skill": "cut_op",
            }
        )

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "locations[2].emits[0]"
        message = errors[0].message.lower()
        assert "uom" in message or "qty" in message


# ---------------------------------------------------------------------------
# Criterion 7 -- an illegal Location field combination (D-055 rules 1 and 2).
# ---------------------------------------------------------------------------


class TestIllegalLocationFieldCombination:
    def test_location_that_consumes_nothing_is_illegal(self) -> None:
        data = _baseline_data()
        data["locations"].append(
            {
                "name": "ghost",
                "consumes": [],
                "emits": [{"thing": "blank", "qty": 1, "uom": "piece"}],
                "setup_key": "grp_ghost",
                "time_model": {"kind": "rate_based", "rate": 5},
                "machine": "cutter_m",
                "labor_skill": "cut_op",
            }
        )

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "locations[2]"
        assert "consumes" in errors[0].message.lower()

    def test_location_that_produces_nothing_is_illegal(self) -> None:
        data = _baseline_data()
        data["locations"].append(
            {
                "name": "sink",
                "consumes": [{"thing": "wire", "qty": 1, "uom": "ft"}],
                "emits": [],
                "setup_key": "grp_sink",
                "time_model": {"kind": "rate_based", "rate": 5},
                "machine": "cutter_m",
                "labor_skill": "cut_op",
            }
        )

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "locations[2]"
        assert "emits" in errors[0].message.lower() or "produce" in errors[0].message.lower()

    def test_scrap_present_with_more_than_one_bare_emit_is_illegal(self) -> None:
        data = _baseline_data()
        data["locations"].append(
            {
                "name": "over_split",
                "consumes": [{"thing": "wire", "qty": 1, "uom": "ft"}],
                "emits": [
                    # Both bare (no qty), same uom as the basis -- legal per
                    # the unit check (rule 3 only fires on a uom CHANGE), but
                    # illegal here: with `scrap` present, exactly ONE emit may
                    # be bare, and this location declares two.
                    {"thing": "blank", "uom": "ft"},
                    {"thing": "trimmed", "uom": "ft"},
                ],
                "scrap": {"rate": 0.05, "thing": "scrap_bits", "uom": "ft"},
                "setup_key": "grp_split",
                "time_model": {"kind": "rate_based", "rate": 5},
                "machine": "cutter_m",
                "labor_skill": "cut_op",
            }
        )

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "locations[2].emits"
        assert "scrap" in errors[0].message.lower() or "bare" in errors[0].message.lower()

    def test_scrap_present_with_zero_bare_emits_is_illegal(self) -> None:
        data = _baseline_data()
        data["locations"].append(
            {
                "name": "over_specified",
                "consumes": [{"thing": "wire", "qty": 1, "uom": "ft"}],
                # Both emits carry explicit qty; with `scrap` present, exactly
                # ONE emit must be bare (the remainder) and none is.
                "emits": [{"thing": "blank", "qty": 0.9, "uom": "ft"}],
                "scrap": {"rate": 0.05, "thing": "scrap_bits", "uom": "ft"},
                "setup_key": "grp_overspec",
                "time_model": {"kind": "rate_based", "rate": 5},
                "machine": "cutter_m",
                "labor_skill": "cut_op",
            }
        )

        errors = _validator().validate(_raw(data))

        assert len(errors) == 1
        assert errors[0].path == "locations[2].emits"
        assert "scrap" in errors[0].message.lower() or "bare" in errors[0].message.lower()


# ---------------------------------------------------------------------------
# Criterion 9 -- "reports every failure": multiple distinct violations return
# multiple ValidationErrors, spanning two different check phases (graph +
# unit), proving the validator never stops at the first failure.
# ---------------------------------------------------------------------------


class TestReportsEveryFailureNotJustTheFirst:
    def test_model_with_two_distinct_violations_returns_two_errors(self) -> None:
        data = _baseline_data()
        data["stocks"] = [{"name": "orphan_bits", "uom": "piece"}]  # graph-phase violation
        data["locations"].append(  # unit-phase violation
            {
                "name": "duster",
                "consumes": [{"thing": "wire", "qty": 1, "uom": "ft"}],
                "emits": [{"thing": "dust", "uom": "lb"}],
                "setup_key": "grp_dust",
                "time_model": {"kind": "rate_based", "rate": 5},
                "machine": "cutter_m",
                "labor_skill": "cut_op",
            }
        )

        errors = _validator().validate(_raw(data))

        assert len(errors) == 2
        paths = {error.path for error in errors}
        assert paths == {"stocks[0]", "locations[2].emits[0]"}
        assert all(isinstance(error, ValidationError) for error in errors)
