"""Domain contracts and profile adapters."""

from twinflow.domain.office import office_adapter
from twinflow.domain.registry import DomainAdapter, DomainIssue, DomainRegistry, registry

registry.register(office_adapter)

__all__ = ["DomainAdapter", "DomainIssue", "DomainRegistry", "registry"]
