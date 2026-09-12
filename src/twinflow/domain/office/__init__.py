"""Standalone front-office case domain."""

from twinflow.domain.office.adapter import OfficeAdapter
from twinflow.domain.office.contracts import (
    Approval,
    Case,
    Document,
    OfficeModel,
    OfficeSnapshot,
    Resource,
    Task,
)

office_adapter = OfficeAdapter()

__all__ = [
    "Approval",
    "Case",
    "Document",
    "OfficeModel",
    "OfficeSnapshot",
    "Resource",
    "Task",
    "OfficeAdapter",
    "office_adapter",
]
