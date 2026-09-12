"""Registry-ready domain adapter without a dependency on the registry itself."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from twinflow.domain.supply_chain.contracts import ValidationIssue
from twinflow.domain.supply_chain.evaluator import describe, evaluate
from twinflow.domain.supply_chain.validation import validate


class SupplyChainDomain:
    """Industry-neutral adapter implementing the shared JSON domain seam."""

    name = "supply_chain"

    def validate(
        self, model: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> list[ValidationIssue]:
        return validate(model, snapshot)

    def describe(
        self, model: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object]:
        return describe(model, snapshot)

    def evaluate(
        self,
        model: Mapping[str, object],
        snapshot: Mapping[str, object],
        seed: int,
        artifact_dir: str | Path | None = None,
        limits: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return evaluate(model, snapshot, seed, artifact_dir, limits)
