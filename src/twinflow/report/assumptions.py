"""COMP-025 AssumptionsCollector — every default the engine actually substituted, named.

Derives the Assumptions block by inspecting an already-built `CompiledModel` passed
IN — never by reaching up into `model/` or `plan/`, which may not import `report/`
(structure.md: "report/ depends on instrumentation/, never the reverse"; report may
depend on model, never model on report). `setup` and `machine_eligibility` are
unconditional in v1: the compiler declares no changeover matrix and no eligibility
data at all, so both defaults always apply. `labor_pool` and `pull_rule.<location_id>`
are conditional on what the model actually declared (D-027, D-040).
"""

from __future__ import annotations

from dataclasses import dataclass

from twinflow.model import CompiledModel


@dataclass(frozen=True)
class Assumption:
    """One substituted default: `{field, default_used, why_absent}` (COMP-025)."""

    field: str
    default_used: str
    why_absent: str


class AssumptionsCollector:
    """Collects the Assumptions block from a compiled model. Side-effect free."""

    def collect(self, compiled_model: CompiledModel) -> list[Assumption]:
        """Return every default substituted for this `compiled_model`.

        Never mutates `compiled_model`; safe to call repeatedly on the same model.
        """
        assumptions: list[Assumption] = [
            Assumption(
                field="setup",
                default_used="setup time zero",
                why_absent="no changeover matrix declared",
            ),
            Assumption(
                field="machine_eligibility",
                default_used="any machine eligible",
                why_absent="no machine eligibility data declared",
            ),
        ]
        if len(compiled_model.labor_pools) == 1:
            assumptions.append(
                Assumption(
                    field="labor_pool",
                    default_used="one undifferentiated labor pool",
                    why_absent="only one labor pool declared",
                )
            )
        for location in compiled_model.locations:
            if location.pull_rule.default_applied:
                assumptions.append(
                    Assumption(
                        field=f"pull_rule.{location.location_id}",
                        default_used="arrival order used as sequence",
                        why_absent=f"no pull rule declared for location {location.location_id!r}",
                    )
                )
        return assumptions
