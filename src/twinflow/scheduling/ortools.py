"""Optional CP-SAT seam.

The core package never imports OR-Tools. Integrators can implement the Solver
protocol or install the optional dependency and extend this adapter.
"""

from __future__ import annotations

import importlib

from twinflow.scheduling.core import ScheduleIssue, ScheduleResult, SchedulingProblem


class OptionalDependencyError(RuntimeError):
    """Raised when an optional optimization backend is requested but absent."""


class ORToolsSolver:
    """CP-SAT availability probe with an honest UNKNOWN fallback."""

    def __init__(self) -> None:
        try:
            importlib.import_module("ortools.sat.python.cp_model")
        except ImportError as exc:
            raise OptionalDependencyError(
                "install the 'ortools' extra to use ORToolsSolver"
            ) from exc

    def solve(
        self, problem: SchedulingProblem, *, time_limit_seconds: float | None = None
    ) -> ScheduleResult:
        del problem, time_limit_seconds
        return ScheduleResult(
            "UNKNOWN",
            issues=(
                ScheduleIssue(
                    "ortools_adapter",
                    "solver",
                    "CP-SAT adapter is optional; no model translation is enabled",
                ),
            ),
        )
