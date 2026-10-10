"""The one thin adapter between the planning service (COMP-117) and TF-003's portfolio API.

TF-003 (`twinflow.scheduling.portfolio.api`, TF-003 COMP-029) is a dependency that may be
absent. This module names only the shape the planning service reads, as structural
`Protocol`s, and `load_portfolio_api()` imports the real module lazily. Nothing else in the
application imports TF-003, so a TF-003 signature change is absorbed here (TF-005 SDD
External Integrations, Risks).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from typing import Any, Protocol, cast

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
    "load_portfolio_api",
]

PORTFOLIO_MODULE = "twinflow.scheduling.portfolio.api"

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
