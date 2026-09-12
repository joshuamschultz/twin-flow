"""Domain contracts and profile adapters."""

from twinflow.domain.office import office_adapter
from twinflow.domain.registry import DomainAdapter, DomainIssue, DomainRegistry, registry
from twinflow.domain.supply_chain import SupplyChainDomain

registry.register(office_adapter)
registry.register(SupplyChainDomain())

__all__ = ["DomainAdapter", "DomainIssue", "DomainRegistry", "registry"]
