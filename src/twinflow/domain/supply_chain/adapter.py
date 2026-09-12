"""Registry-ready domain adapter without a dependency on the registry itself."""

from __future__ import annotations

from collections.abc import Mapping

from twinflow.domain.registry import DomainIssue
from twinflow.domain.supply_chain.evaluator import describe, evaluate
from twinflow.domain.supply_chain.validation import validate


class SupplyChainDomain:
    """Industry-neutral adapter implementing the shared JSON domain seam."""

    name = "supply_chain"
    domain = "supply_chain"
    capabilities = frozenset({"supply_chain.basic"})

    def validate(
        self, model: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> list[DomainIssue]:
        return [DomainIssue(issue.path, issue.message, issue.severity)
                for issue in validate(model, snapshot)]

    def describe(
        self, model: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object]:
        return describe(model, snapshot)

    def evaluate(
        self,
        model: Mapping[str, object],
        snapshot: Mapping[str, object],
        *,
        seed: int = 0,
        artifact_dir: str | None = None,
        limits: Mapping[str, int | float] | None = None,
    ) -> dict[str, object]:
        return evaluate(model, snapshot, seed, artifact_dir, limits)
