"""The tool catalogue's building blocks (COMP-118): one `ToolSpec` per tool, and its services.

`ToolSpec` is one row of the catalogue table: the typed input and output models, the
classification and the scope it needs, the MCP hints, the handler and the guard's timeout.
`ToolServices` is everything a handler may touch, all in process (no REST hop).

This module, `models`, `_handlers` and `guard` import no `mcp` SDK module, so the guard
pipeline also serves the REST routes without the `agent` extra.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from twinflow.application.action_tools import ActionTools
from twinflow.application.artifacts import SignedArtifactStore
from twinflow.application.decision_tools import DecisionTools
from twinflow.application.planning import PlanningService
from twinflow.application.planning_artifacts import PlanningArtifacts
from twinflow.application.workspace import Workspace
from twinflow.service.auth import Caller

Classification = Literal["read_only", "state_modifying"]
TimeoutCap = Literal["fixed", "solve", "simulate"]


@dataclass(frozen=True)
class ToolServices:
    """The in-process use cases behind the tools.

    `artifacts` is None when no signing key is configured: the planning-profile and K-table
    tools then fail closed (`no_planning_profile`, `capability_unavailable`). `local_mode`
    is loopback open local mode (or stdio): only there may a build fall back to the
    built-in default profile, recorded as unsigned. `actions` is built by `build_server`
    when not supplied.
    """

    workspace: Workspace
    decisions: DecisionTools
    planning: PlanningService
    artifacts: SignedArtifactStore | None
    models_root: Path | None
    actions: ActionTools | None = None
    local_mode: bool = False

    @property
    def planning_artifacts(self) -> PlanningArtifacts:
        """The signed-artifact resolver every build and solve goes through."""
        return PlanningArtifacts(self.artifacts, local_mode=self.local_mode)


# A handler takes the validated input model and returns the output model. The table is
# heterogeneous, so the input is typed `Any` here; each handler annotates its own model.
Handler = Callable[[ToolServices, Caller, Any], BaseModel]


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its contract, its classification and how the guard runs it.

    `solve_slot` marks solve-type tools (solves and simulations), which also hold one of
    the caller's concurrent solve slots (COMP-121) for the duration of the call.
    `timeout_cap` names the planning service cap the timeout follows: `fixed` uses
    `timeout_seconds` as is; `solve` and `simulate` add it, as grace, to the configured
    solve or simulate cap (TF-004 D-105: the two caps are independent). The guard and
    `describe_capabilities` read `timeout_for(services)`.
    """

    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    classification: Classification
    scope: str
    idempotent: bool
    destructive: bool
    handler: Handler
    timeout_seconds: float
    description: str
    solve_slot: bool = False
    timeout_cap: TimeoutCap = "fixed"

    def timeout_for(self, services: ToolServices) -> float:
        """The guard timeout; a capped tool's follows the service's configured cap."""
        if self.timeout_cap == "solve":
            return services.planning.max_solve_seconds + self.timeout_seconds
        if self.timeout_cap == "simulate":
            return services.planning.max_simulate_seconds + self.timeout_seconds
        return self.timeout_seconds

    def request_key(self, validated: BaseModel) -> str | None:
        """The idempotency key of a validated input, when the tool takes one."""
        key = getattr(validated, "request_key", None)
        return key if isinstance(key, str) else None


__all__ = ["Classification", "Handler", "TimeoutCap", "ToolServices", "ToolSpec"]
