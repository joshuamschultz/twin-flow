"""Domain contracts and profile adapters."""

from twinflow.domain.office import office_adapter
from twinflow.domain.registry import DomainAdapter, DomainIssue, DomainRegistry, registry
from twinflow.domain.supply_chain import SupplyChainDomain
from twinflow.engines.step.adapter import MakeToStockAdapter

registry.register(office_adapter)
registry.register(SupplyChainDomain())
registry.register(MakeToStockAdapter())

__all__ = ["DomainAdapter", "DomainIssue", "DomainRegistry", "registry"]
