"""COMP-004 Bundle and COMP-005 PartTypeRegistry — unit tests (T-006, red phase).

Bundle: the immutable unit of flow — (qty, thing, uom, attrs). Splitting or merging
returns NEW Bundle objects; the original is never mutated (D-043).

PartTypeRegistry: holds every declared part type with its named, typed attributes and
its single unit of measure. Raises on an unknown part-type id and on an undeclared
attribute lookup — it never returns None, because it is the authority the validator
later consults to prove an expression references a real attribute.

Pure Layer 1 primitives: no YAML, no file I/O. Registries and bundles are built here
from plain Python values only.
"""

from __future__ import annotations

import pytest

from twinflow.primitives.bundle import Bundle
from twinflow.primitives.part import PartTypeRegistry

# ---------------------------------------------------------------------------
# PartTypeRegistry — COMP-005
# ---------------------------------------------------------------------------


def _make_registry() -> PartTypeRegistry:
    return PartTypeRegistry(
        {
            "blank": {
                "attributes": {"length": float, "diameter": float},
                "uom": "piece",
            },
            "wire": {
                "attributes": {"gauge": float},
                "uom": "ft",
            },
        }
    )


def test_registry_attributes_returns_declared_attribute_types() -> None:
    registry = _make_registry()

    assert registry.attributes("blank") == {"length": float, "diameter": float}


def test_registry_uom_returns_the_single_declared_uom() -> None:
    registry = _make_registry()

    assert registry.uom("blank") == "piece"
    assert registry.uom("wire") == "ft"


def test_registry_uom_lookup_on_unknown_part_id_raises() -> None:
    registry = _make_registry()

    with pytest.raises(KeyError):
        registry.uom("nonexistent_part")


def test_registry_attributes_lookup_on_unknown_part_id_raises() -> None:
    registry = _make_registry()

    with pytest.raises(KeyError):
        registry.attributes("nonexistent_part")


def test_registry_undeclared_attribute_lookup_raises_not_none() -> None:
    """The registry is the authority a validator consults to prove an expression
    references a real attribute — an undeclared attribute must raise, never return
    None, or a bad expression would silently validate."""
    registry = _make_registry()

    with pytest.raises(KeyError):
        registry.attribute_type("blank", "not_a_declared_attribute")


def test_registry_declared_attribute_type_lookup_succeeds() -> None:
    registry = _make_registry()

    assert registry.attribute_type("blank", "length") is float


# ---------------------------------------------------------------------------
# Bundle — COMP-004
# ---------------------------------------------------------------------------


def _make_bundle() -> Bundle:
    return Bundle(qty=10.0, thing="blank", uom="piece", attrs={"length": 12.0, "ok": True})


def test_bundle_exposes_qty_thing_uom_and_attrs() -> None:
    bundle = _make_bundle()

    assert bundle.qty == 10.0
    assert bundle.thing == "blank"
    assert bundle.uom == "piece"
    assert bundle.attrs == {"length": 12.0, "ok": True}


def test_bundle_declares_exactly_one_thing_and_one_uom() -> None:
    bundle = _make_bundle()

    # A single scalar thing / uom, not a collection — one bundle, one part type.
    assert isinstance(bundle.thing, str)
    assert isinstance(bundle.uom, str)


def test_bundle_qty_attribute_cannot_be_reassigned() -> None:
    bundle = _make_bundle()

    with pytest.raises(AttributeError):
        bundle.qty = 5.0  # type: ignore[misc]


def test_bundle_attrs_attribute_cannot_be_reassigned() -> None:
    bundle = _make_bundle()

    with pytest.raises(AttributeError):
        bundle.attrs = {"length": 999.0}  # type: ignore[misc]


def test_bundle_attrs_mapping_cannot_be_mutated_in_place() -> None:
    bundle = _make_bundle()

    with pytest.raises(TypeError):
        bundle.attrs["length"] = 999.0  # type: ignore[index]


def test_bundle_with_qty_returns_new_bundle_leaving_original_unchanged() -> None:
    original = _make_bundle()

    reduced = original.with_qty(4.0)

    assert reduced is not original
    assert reduced.qty == 4.0
    assert original.qty == 10.0
    # Everything else about identity carries over unchanged.
    assert reduced.thing == original.thing
    assert reduced.uom == original.uom
    assert reduced.attrs == original.attrs


def test_bundle_split_returns_two_new_bundles_summing_to_original_qty() -> None:
    original = _make_bundle()

    first, second = original.split(3.0)

    assert first is not original
    assert second is not original
    assert first.qty == 3.0
    assert second.qty == 7.0
    assert first.qty + second.qty == original.qty
    # Original bundle is untouched by the split.
    assert original.qty == 10.0


def test_bundle_split_preserves_thing_uom_and_attrs_on_both_pieces() -> None:
    original = _make_bundle()

    first, second = original.split(6.0)

    assert first.thing == second.thing == original.thing
    assert first.uom == second.uom == original.uom
    assert first.attrs == second.attrs == original.attrs


def test_bundle_split_with_qty_exceeding_original_raises() -> None:
    original = _make_bundle()

    with pytest.raises(ValueError, match="qty"):
        original.split(999.0)


def test_bundle_merge_returns_new_bundle_with_summed_qty() -> None:
    first = Bundle(qty=3.0, thing="blank", uom="piece", attrs={"length": 12.0})
    second = Bundle(qty=7.0, thing="blank", uom="piece", attrs={"length": 12.0})

    merged = first.merge(second)

    assert merged is not first
    assert merged is not second
    assert merged.qty == 10.0
    # Merge does not mutate either input.
    assert first.qty == 3.0
    assert second.qty == 7.0


def test_bundle_merge_with_mismatched_thing_raises() -> None:
    blank = Bundle(qty=3.0, thing="blank", uom="piece", attrs={})
    wire = Bundle(qty=7.0, thing="wire", uom="ft", attrs={})

    with pytest.raises(ValueError, match="thing"):
        blank.merge(wire)


def test_bundle_merge_with_mismatched_uom_raises() -> None:
    piece = Bundle(qty=3.0, thing="blank", uom="piece", attrs={})
    lot = Bundle(qty=7.0, thing="blank", uom="lot", attrs={})

    with pytest.raises(ValueError, match="uom"):
        piece.merge(lot)


def test_bundle_uom_matches_its_part_types_registry_uom() -> None:
    registry = _make_registry()
    bundle = Bundle(qty=1.0, thing="wire", uom=registry.uom("wire"), attrs={"gauge": 12.0})

    assert bundle.uom == registry.uom("wire")
