"""Dispatch policy files for the discrete-event simulator (T-249, T-257, COMP-154, COMP-155).

A dispatch policy file is a policy extension that says `DECISION = "dispatch"` and defines
`decide(state)`, which returns one rule name. It is loaded by the one extension loader
(D-150), so its module name, problems and settings work like every other policy file.
`state` is `twinflow.extensions.view.DispatchState`, with plain names:

- `state.location`  the location choosing what to run next
- `state.queue`     the waiting jobs, read-only dicts with `order_id`, `thing`, `qty`,
                    `due_date` (None when the plan has none), `priority`, `arrival_time`
- `state.actions`   the rule names it may return: fifo, edd, spt, critical_ratio
- `state.time`      the simulation clock in seconds
- `state.current`   the rule in use now

`des_runtime(file)` wraps the file in a `twinflow.policy.PolicyRuntime`. When `decide`
raises or returns a name that is not allowed, the runtime uses its fallback rule and the
reason (file, line, error or allowed names) is kept in the decision trace.

Loading the file runs local code: in-process Python and the CLI only (D-139).
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from twinflow.extensions.checker import ExtensionFailed, call_checked
from twinflow.extensions.kinds import KINDS, check_return
from twinflow.extensions.loader import Extension, Problem, load_file
from twinflow.extensions.view import DispatchState, dispatch_state
from twinflow.policy import (
    DispatchAction,
    DispatchName,
    MaskedActionError,
    PolicyError,
    PolicyObservation,
    PolicyRuntime,
)

DISPATCH = "dispatch"


class PolicyFileError(ValueError):
    """A dispatch policy file cannot be used. `problem` says where, what and how to fix it."""

    def __init__(self, problem: Problem) -> None:
        super().__init__(str(problem))
        self.problem = problem

    @property
    def fix(self) -> str:
        return self.problem.fix


def observed_state(observation: PolicyObservation) -> DispatchState:
    """The dispatch view of one runtime observation (a fresh copy of the queue)."""
    return dispatch_state(
        observation.location_id,
        [asdict(item) for item in observation.queue],
        time=observation.simulation_time,
        current=observation.current_policy,
    )


def _allowed(value: Any, args: Sequence[Any]) -> str | None:
    return check_return("policy", value, allowed=args[0].actions)


class DispatchFilePolicy:
    """A `twinflow.policy.Policy` that asks a dispatch policy file's `decide(state)`."""

    def __init__(self, extension: Extension) -> None:
        self.extension = extension
        self.path = extension.file

    def decide(
        self, observation: PolicyObservation, allowed_actions: tuple[DispatchAction, ...]
    ) -> DispatchAction:
        allowed = {action.policy: action for action in allowed_actions}
        state = dataclasses.replace(observed_state(observation), actions=tuple(allowed))
        try:
            choice = call_checked(self.extension, (state,), validate=_allowed)
        except ExtensionFailed as failed:
            error = MaskedActionError if failed.problem.code == "bad_action" else PolicyError
            raise error(str(failed.problem)) from failed
        return allowed[choice]


def _problem(file: Path, code: str, message: str, fix: str, line: int | None = None) -> Problem:
    return Problem("policy", code, message, fix, file, line)


def load_dispatch_file(path: Path | str) -> Extension:
    """Load one dispatch policy file with the extension loader; raises `PolicyFileError`."""
    file = Path(path).resolve()
    if not file.is_file():
        raise PolicyFileError(
            _problem(file, "no_file", "The policy file was not found.", "Check the path.")
        )
    if file.suffix != ".py":
        raise PolicyFileError(
            _problem(file, "bad_file", "A policy file must end in .py.", "Rename it <name>.py.")
        )
    loaded = load_file(file, KINDS["policy"])
    if isinstance(loaded, Problem):
        raise PolicyFileError(loaded)
    if loaded.decision != DISPATCH:
        declared = loaded.target.__globals__.get("DECISION")
        found = "no DECISION" if declared is None else f"DECISION = {declared!r}"
        raise PolicyFileError(
            _problem(
                file,
                "bad_decision",
                f"This is not a dispatch policy ({found}).",
                'Add the line DECISION = "dispatch" at the top of the file.',
            )
        )
    return loaded


def des_runtime(path: Path | str, *, fallback: DispatchName = "fifo") -> PolicyRuntime:
    """A `PolicyRuntime` that runs one dispatch policy file (in-process Python only).

    Raises `PolicyFileError` when the file cannot load or is not a dispatch policy. The
    runtime's `policy_artifact` is `policy_file:<name>:sha256:<file digest>`.
    """
    extension = load_dispatch_file(path)
    digest = hashlib.sha256(extension.file.read_bytes()).hexdigest()
    return PolicyRuntime(
        DispatchFilePolicy(extension),
        f"policy_file:{extension.file.name}:sha256:{digest}",
        fallback=fallback,
    )


__all__ = ["DispatchFilePolicy", "PolicyFileError", "des_runtime", "load_dispatch_file"]
