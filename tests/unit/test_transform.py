"""COMP-010 Transform — unit tests (T-008, red phase).

Transform is the ONE conversion contract: a list[Bundle] of inputs becomes a
list[Bundle] of outputs. It absorbs the former Transform/Split/Assemble/Batch/Yield
archetypes (D-043) — remainders and scrap are ORDINARY output bundles, never special
cases, and the SAME `Transform.apply()` code path serves every shape below.

The expression sandbox (COMP-015) does not exist yet, so a "compiled" TransformSpec
is modeled here as plain Python callables over the input bundle list — exactly the
shape `model/expressions.py` will later emit from YAML (D-044). `OutputSpec` and
`TransformSpec` below are the CONTRACT the implementer must conform to; only
`Transform` itself is imported from src, because the compiled-spec types do not
exist there yet (T-009 has not landed). Duck typing carries the contract: `Transform`
must accept any spec whose `.outputs` are objects exposing `.thing`, `.uom`,
`.qty(inputs)`, and `.attrs` (a mapping of name -> callable(inputs)).

One parametrised test drives all six worked cases from structure.md's primitives
table (D-043) through the identical `Transform(spec).apply(inputs, registry)` path:
cut (with remainder), pour, transform, assemble, scrap, and bin (sortation). No
per-shape branching exists in the test, and none may exist in the implementation.

Pure Layer 1 primitives: no YAML, no I/O, no simpleeval, no Polars. Inputs are built
from real Bundle and PartTypeRegistry objects only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import NamedTuple

import pytest

from factory_twin.primitives.bundle import AttrValue, Bundle
from factory_twin.primitives.part import PartTypeRegistry
from factory_twin.primitives.transform import Transform

# ---------------------------------------------------------------------------
# The TransformSpec contract — test-authored, implementer conforms (T-009).
#
# QtyFn / AttrFn stand in for compiled simpleeval expressions (COMP-015, not yet
# built). Each receives the full list of input Bundles and returns a scalar.
# ---------------------------------------------------------------------------

QtyFn = Callable[[list[Bundle]], float]
AttrFn = Callable[[list[Bundle]], AttrValue]


@dataclass(frozen=True)
class OutputSpec:
    """One output bundle a Transform produces: part type, uom, and the callables
    that compute its qty and (optionally) its attributes from the inputs."""

    thing: str
    uom: str
    qty: QtyFn
    attrs: Mapping[str, AttrFn] = field(default_factory=dict)


@dataclass(frozen=True)
class TransformSpec:
    """A compiled transform: an ordered list of output specs. Output bundles are
    produced in this same order — a remainder or scrap bundle is just another
    entry in the list, never a distinguished special case (D-043)."""

    outputs: list[OutputSpec]


# ---------------------------------------------------------------------------
# Shared PartTypeRegistry covering every part type used across the six cases.
# ---------------------------------------------------------------------------


def _registry() -> PartTypeRegistry:
    def spec(uom: str, attributes: dict[str, type] | None = None) -> dict:
        return {"attributes": attributes or {}, "uom": uom}

    return PartTypeRegistry(
        {
            # CUT
            "wire": spec("ft"),
            "blank": spec("piece"),
            # POUR
            "aluminum": spec("lb"),
            "casting": spec("piece"),
            # TRANSFORM
            "raw": spec("piece"),
            "plated": spec("piece"),
            # ASSEMBLE
            "housing": spec("piece"),
            "lid": spec("piece"),
            "screw": spec("piece"),
            "assembly": spec("piece", {"components": float}),
            # SCRAP
            "good": spec("piece"),
            "scrap": spec("piece"),
            # BIN
            "chip": spec("piece"),
            "gradeA": spec("piece"),
            "gradeB": spec("piece"),
            "gradeC": spec("piece"),
        }
    )


class ExpectedBundle(NamedTuple):
    """One expected output: thing, qty, uom, and attrs to check on the produced
    Bundle. attrs defaults to {} meaning "no attrs asserted beyond presence"."""

    thing: str
    qty: float
    uom: str
    attrs: dict[str, AttrValue] = {}


class TransformCase(NamedTuple):
    case_id: str
    inputs: list[Bundle]
    spec: TransformSpec
    expected: list[ExpectedBundle]


# ---------------------------------------------------------------------------
# The six worked cases (structure.md primitives table, D-043).
# ---------------------------------------------------------------------------

_CUT = TransformCase(
    case_id="cut",
    inputs=[Bundle(10.0, "wire", "ft")],
    spec=TransformSpec(
        outputs=[
            # 9 one-foot blanks cut from 10 ft of wire.
            OutputSpec(thing="blank", uom="piece", qty=lambda ins: 9.0),
            # The leftover wire is an ORDINARY output bundle, not a special case.
            OutputSpec(thing="wire", uom="ft", qty=lambda ins: ins[0].qty - 9.0),
        ]
    ),
    expected=[
        ExpectedBundle("blank", 9.0, "piece"),
        ExpectedBundle("wire", 1.0, "ft"),
    ],
)

_POUR = TransformCase(
    case_id="pour",
    inputs=[Bundle(10.0, "aluminum", "lb")],
    spec=TransformSpec(
        outputs=[
            OutputSpec(thing="casting", uom="piece", qty=lambda ins: ins[0].qty / 2.0),
        ]
    ),
    expected=[ExpectedBundle("casting", 5.0, "piece")],
)

_TRANSFORM = TransformCase(
    case_id="transform",
    inputs=[Bundle(100.0, "raw", "piece")],
    spec=TransformSpec(
        outputs=[
            # Thing changes; qty is preserved unchanged.
            OutputSpec(thing="plated", uom="piece", qty=lambda ins: ins[0].qty),
        ]
    ),
    expected=[ExpectedBundle("plated", 100.0, "piece")],
)

_ASSEMBLE = TransformCase(
    case_id="assemble",
    inputs=[
        Bundle(50.0, "housing", "piece"),
        Bundle(50.0, "lid", "piece"),
        Bundle(100.0, "screw", "piece"),
    ],
    spec=TransformSpec(
        outputs=[
            OutputSpec(
                thing="assembly",
                uom="piece",
                qty=lambda ins: min(b.qty for b in ins if b.thing in ("housing", "lid")),
                attrs={"components": lambda ins: float(len(ins))},
            ),
        ]
    ),
    expected=[ExpectedBundle("assembly", 50.0, "piece", {"components": 3.0})],
)

_SCRAP = TransformCase(
    case_id="scrap",
    inputs=[Bundle(100.0, "blank", "piece")],
    spec=TransformSpec(
        outputs=[
            OutputSpec(thing="good", uom="piece", qty=lambda ins: ins[0].qty * 0.95),
            # Scrap is an ORDINARY output bundle, not a special case.
            OutputSpec(thing="scrap", uom="piece", qty=lambda ins: ins[0].qty * 0.05),
        ]
    ),
    expected=[
        ExpectedBundle("good", 95.0, "piece"),
        ExpectedBundle("scrap", 5.0, "piece"),
    ],
)

_BIN = TransformCase(
    case_id="bin",
    inputs=[Bundle(1000.0, "chip", "piece")],
    spec=TransformSpec(
        outputs=[
            OutputSpec(thing="gradeA", uom="piece", qty=lambda ins: ins[0].qty * 0.3),
            OutputSpec(thing="gradeB", uom="piece", qty=lambda ins: ins[0].qty * 0.5),
            OutputSpec(thing="gradeC", uom="piece", qty=lambda ins: ins[0].qty * 0.2),
        ]
    ),
    expected=[
        ExpectedBundle("gradeA", 300.0, "piece"),
        ExpectedBundle("gradeB", 500.0, "piece"),
        ExpectedBundle("gradeC", 200.0, "piece"),
    ],
)

_CASES = [_CUT, _POUR, _TRANSFORM, _ASSEMBLE, _SCRAP, _BIN]


@pytest.mark.parametrize("case", _CASES, ids=[c.case_id for c in _CASES])
def test_transform_apply_produces_expected_output_bundles(case: TransformCase) -> None:
    """One code path, six shapes (D-043): cut-with-remainder, pour, rename-in-place,
    many-to-one assemble, scrap-as-ordinary-output, and one-to-many bin sortation.

    No branch in this test inspects `case.case_id` to alter what it asserts — every
    case is checked identically, which is only possible if `Transform.apply()` truly
    has no per-shape special casing either.
    """
    registry = _registry()
    transform = Transform(case.spec)

    outputs = transform.apply(case.inputs, registry)

    assert isinstance(outputs, list)
    assert len(outputs) == len(case.expected)

    for produced, expected in zip(outputs, case.expected, strict=True):
        # Remainder and scrap bundles must be ordinary Bundle instances — nothing
        # about their type or shape marks them as special.
        assert isinstance(produced, Bundle)
        assert produced.thing == expected.thing
        assert produced.qty == pytest.approx(expected.qty)
        assert produced.uom == expected.uom
        # The output uom must agree with what the registry declares for that part
        # type — a sanity cross-check on the fixture itself as much as the code.
        assert produced.uom == registry.uom(produced.thing)
        for attr_name, attr_value in expected.attrs.items():
            assert produced.attrs[attr_name] == pytest.approx(attr_value)


def test_transform_apply_returns_new_list_not_input_list_identity() -> None:
    """`apply()` must not hand back the caller's own input list — inputs and
    outputs are distinct collections even when (degenerately) qty is unchanged."""
    registry = _registry()
    transform = Transform(_TRANSFORM.spec)

    outputs = transform.apply(_TRANSFORM.inputs, registry)

    assert outputs is not _TRANSFORM.inputs


def test_transform_apply_does_not_mutate_input_bundles() -> None:
    """Bundle is frozen (D-043) and Transform is pure — the caller's input Bundle
    objects must be byte-for-byte unchanged after apply()."""
    registry = _registry()
    original = Bundle(100.0, "raw", "piece")
    inputs = [original]
    transform = Transform(_TRANSFORM.spec)

    transform.apply(inputs, registry)

    assert inputs == [original]
    assert inputs[0].qty == 100.0
    assert inputs[0].thing == "raw"
