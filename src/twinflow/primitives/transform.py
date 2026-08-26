"""COMP-010 Transform — the one conversion contract: bundles in -> bundles out.

Absorbs the former Transform/Split/Assemble/Batch/Yield archetypes (D-043).
Remainders and scrap are ordinary output bundles, never special cases.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from twinflow.primitives.bundle import AttrValue, Bundle
from twinflow.primitives.part import PartTypeRegistry

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


class Transform:
    """Applies a compiled TransformSpec to a list of input Bundles.

    One code path serves every shape in structure.md's primitives table
    (cut, pour, transform, assemble, scrap, bin) — the shape lives entirely
    in how many OutputSpecs the spec declares, never in a branch here (D-043).
    """

    def __init__(self, spec: TransformSpec) -> None:
        self._spec = spec

    def apply(self, inputs: list[Bundle], registry: PartTypeRegistry) -> list[Bundle]:
        """Produce one new Bundle per output spec, in order.

        Each output's qty and attrs callables receive the full `inputs` list.
        Inputs are never mutated; a new list is always returned.
        """
        del registry  # not yet consulted; kept for the Layer 2 compiler's contract
        return [
            Bundle(
                qty=output.qty(inputs),
                thing=output.thing,
                uom=output.uom,
                attrs={name: attr_fn(inputs) for name, attr_fn in output.attrs.items()},
            )
            for output in self._spec.outputs
        ]
