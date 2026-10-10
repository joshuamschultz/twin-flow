"""The one thin adapter between the planning service (COMP-117) and TF-003's portfolio API.

TF-003 (`twinflow.scheduling.portfolio.api`, TF-003 COMP-029) is a dependency that may be
absent. This module names only the shape the planning service reads, as structural
`Protocol`s, and `load_portfolio_api()` imports the real module lazily. Nothing else in the
application imports TF-003, so a TF-003 signature change is absorbed here (TF-005 SDD
External Integrations, Risks). The ATCS K-table support (`KTable`, `as_k_lookup`, TF-003
COMP-028) is reached the same way, through `load_k_table_model()` and `k_lookup_for()`.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from typing import Any, Protocol, cast

from pydantic import BaseModel

from twinflow.scheduling import (
    Job,
    ObjectiveKind,
    Override,
    PreviousPlan,
    Schedule,
    ScheduleIssue,
    SolveRequest,
    SolveResult,
)

__all__ = [
    "CandidateLike",
    "MenuEntryLike",
    "PortfolioApi",
    "PortfolioResultLike",
    "k_lookup_for",
    "load_k_table_model",
    "load_portfolio_api",
]

PORTFOLIO_MODULE = "twinflow.scheduling.portfolio.api"
KTABLE_MODULE = "twinflow.scheduling.learning.ktable"

log = logging.getLogger(__name__)


class MenuEntryLike(Protocol):
    """A TF-003 `MenuEntry`: one candidate's values on the menu axes."""

    @property
    def candidate_id(self) -> str: ...

    @property
    def supported(self) -> bool: ...

    @property
    def values(self) -> Mapping[ObjectiveKind, float]: ...


class CandidateLike(Protocol):
    """A TF-003 `Candidate`: one verified-or-not schedule from one pipeline."""

    @property
    def id(self) -> str: ...

    @property
    def pipeline(self) -> tuple[str, ...]: ...

    @property
    def schedule(self) -> Schedule: ...

    @property
    def verified(self) -> bool: ...

    @property
    def objective_values(self) -> Mapping[ObjectiveKind, float]: ...

    @property
    def issues(self) -> tuple[ScheduleIssue, ...]: ...


class PortfolioResultLike(Protocol):
    """A TF-003 `PortfolioResult`: candidates, their menu and the recommended candidate id."""

    @property
    def candidates(self) -> tuple[CandidateLike, ...]: ...

    @property
    def menu(self) -> tuple[MenuEntryLike, ...]: ...

    @property
    def recommended(self) -> str | None: ...

    @property
    def issues(self) -> tuple[ScheduleIssue, ...]: ...

    @property
    def elapsed_seconds(self) -> float: ...


class PortfolioApi(Protocol):
    """The four TF-003 planning functions, signatures per the TF-005 SDD."""

    def solve_portfolio(
        self,
        request: SolveRequest,
        *,
        budget_seconds: float | None = None,
        seed: int = 0,
        pipelines: tuple[str, ...] | None = None,
        k_lookup: object | None = None,
    ) -> PortfolioResultLike: ...

    # `result` is TF-003's concrete `PortfolioResult`; each implementation accepts only its
    # own result type, so the port cannot name a narrower parameter type than `Any`.
    def get_pareto_menu(
        self, result: Any, axes: tuple[ObjectiveKind, ...] | None = None
    ) -> tuple[MenuEntryLike, ...]: ...

    def resolve_with_overrides(
        self,
        request: SolveRequest,
        previous_plan: PreviousPlan,
        overrides: tuple[Override, ...],
        *,
        budget_seconds: float | None = None,
        seed: int = 0,
    ) -> PortfolioResultLike: ...

    def insert_rush_order(
        self,
        request: SolveRequest,
        previous_plan: PreviousPlan,
        rush_job: Job,
        *,
        budget_seconds: float | None = None,
        seed: int = 0,
    ) -> SolveResult: ...


def load_portfolio_api() -> PortfolioApi | None:
    """The TF-003 portfolio module, or None when it is not installed (capability absent)."""
    try:
        module = importlib.import_module(PORTFOLIO_MODULE)
    except ImportError:
        log.info("TF-003 portfolio api not importable; planning solves are unavailable")
        return None
    # A module is a structural implementation; mypy cannot check a module against a Protocol.
    return cast(PortfolioApi, module)


def load_k_table_model() -> type[BaseModel] | None:
    """TF-003's `KTable` model, or None when TF-003 is not installed."""
    module = _k_table_module()
    model = getattr(module, "KTable", None) if module is not None else None
    if isinstance(model, type) and issubclass(model, BaseModel):
        return model
    return None


def k_lookup_for(table: BaseModel) -> object | None:
    """TF-003's `as_k_lookup(table)` (the ATCS `k_lookup`), or None when TF-003 lacks it."""
    module = _k_table_module()
    as_k_lookup = getattr(module, "as_k_lookup", None) if module is not None else None
    if not callable(as_k_lookup):
        return None
    lookup: object = as_k_lookup(table)
    return lookup


def _k_table_module() -> object | None:
    try:
        return importlib.import_module(KTABLE_MODULE)
    except ImportError:
        return None
