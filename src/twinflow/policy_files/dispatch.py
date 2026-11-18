"""Dispatch policy files for the discrete-event simulator (T-249, COMP-154, COMP-155).

A dispatch policy file says `DECISION = "dispatch"` and defines `decide(state)`, which
returns one rule name. `state` (`DispatchState`) has plain names:

- `state.location`  the location choosing what to run next
- `state.queue`     the waiting jobs, a list of plain dicts with `order_id`, `thing`,
                    `qty`, `due_date` (None when the plan has none), `priority`,
                    `arrival_time`
- `state.actions`   the rule names it may return: fifo, edd, spt, critical_ratio
- `state.time`      the simulation clock in seconds
- `state.current`   the rule in use now

`des_runtime(file)` wraps the file in a `twinflow.policy.PolicyRuntime`. When `decide`
raises or returns a name that is not allowed, the runtime uses its fallback rule and the
reason (file, error, allowed names) is kept in the decision trace.

This is the minimal Phase 5 slice; Phase 3 builds the full policy view and loader.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from twinflow.policy import (
    DISPATCH_NAMES,
    DispatchAction,
    DispatchName,
    MaskedActionError,
    PolicyError,
    PolicyObservation,
    PolicyRuntime,
)
from twinflow.policy_files._load import PolicyFileError, load_module

DISPATCH = "dispatch"


@dataclass(frozen=True)
class DispatchState:
    """What a dispatch policy sees at one decision. `queue` is a fresh list per call."""

    location: str
    queue: list[dict[str, Any]]
    actions: tuple[str, ...]
    time: float
    current: str


def dispatch_state(observation: PolicyObservation) -> DispatchState:
    return DispatchState(
        location=observation.location_id,
        queue=[asdict(item) for item in observation.queue],
        actions=DISPATCH_NAMES,
        time=observation.simulation_time,
        current=observation.current_policy,
    )


class DispatchFilePolicy:
    """A `twinflow.policy.Policy` that asks a policy file's `decide(state)`."""

    def __init__(self, path: Path, module: ModuleType) -> None:
        self.path = path
        self._module = module
        self._decide: Callable[[DispatchState], object] = module.decide

    def decide(
        self, observation: PolicyObservation, allowed_actions: tuple[DispatchAction, ...]
    ) -> DispatchAction:
        try:
            choice = self._decide(dispatch_state(observation))
        except Exception as exc:  # user code: any error falls back (PolicyRuntime)
            raise PolicyError(
                f"{self.path.name}: decide raised {type(exc).__name__}: {exc}"
            ) from exc
        allowed = {action.policy: action for action in allowed_actions}
        if not isinstance(choice, str) or choice not in allowed:
            raise MaskedActionError(
                f"{self.path.name}: decide returned {choice!r}; return one of {', '.join(allowed)}"
            )
        return allowed[choice]


def _check(path: Path, module: ModuleType) -> None:
    decision = getattr(module, "DECISION", None)
    if decision != DISPATCH:
        found = "no DECISION" if decision is None else f"DECISION = {decision!r}"
        raise PolicyFileError(
            path,
            f"this is not a dispatch policy ({found}).",
            'Add the line DECISION = "dispatch" at the top of the file.',
        )
    if not callable(getattr(module, "decide", None)):
        raise PolicyFileError(
            path,
            "the file has no decide function.",
            "Add def decide(state): that returns a rule name, like 'fifo'.",
        )


def des_runtime(path: Path | str, *, fallback: DispatchName = "fifo") -> PolicyRuntime:
    """A `PolicyRuntime` that runs one dispatch policy file (in-process Python only).

    Raises `PolicyFileError` when the file cannot load or is not a dispatch policy. The
    runtime's `policy_artifact` is `policy_file:<name>:sha256:<file digest>`.
    """
    module = load_module(path)
    file = Path(path).resolve()
    _check(file, module)
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    return PolicyRuntime(
        DispatchFilePolicy(file, module),
        f"policy_file:{file.name}:sha256:{digest}",
        fallback=fallback,
    )
