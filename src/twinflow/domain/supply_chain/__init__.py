"""Executable industry-neutral supply-network domain."""

from twinflow.domain.supply_chain.adapter import SupplyChainDomain
from twinflow.domain.supply_chain.contracts import ValidationIssue
from twinflow.domain.supply_chain.evaluator import describe, evaluate
from twinflow.domain.supply_chain.parsing import SupplyChainInputError
from twinflow.domain.supply_chain.validation import validate

__all__ = [
    "SupplyChainDomain",
    "SupplyChainInputError",
    "ValidationIssue",
    "describe",
    "evaluate",
    "validate",
]
