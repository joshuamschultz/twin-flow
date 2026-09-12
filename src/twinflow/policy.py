"""Recorded local policies at real simulator dispatch boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

DispatchName = Literal["fifo", "edd", "spt", "critical_ratio"]
_DISPATCH_NAMES: tuple[DispatchName, ...] = ("fifo", "edd", "spt", "critical_ratio")


class PolicyError(ValueError):
    """Base error for rejected policy interactions."""


class StaleStateError(PolicyError):
    """An action references a state version that is no longer current."""


class MaskedActionError(PolicyError):
    """An action is outside the current boundary's action mask."""


class ReplayMismatchError(PolicyError):
    """Recorded and current observable state differ."""


@dataclass(frozen=True, slots=True)
class QueueItem:
    """Observable job fields available to a dispatch policy."""

    order_id: str | None
    thing: str
    qty: float
    due_date: float | None
    priority: float
    arrival_time: float


@dataclass(frozen=True, slots=True)
class PolicyObservation:
    """Observable state at one real dispatch boundary."""

    state_version: int
    location_id: str
    simulation_time: float
    current_policy: DispatchName
    queue: tuple[QueueItem, ...]

    @property
    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class DispatchAction:
    """Change dispatch ordering for this and future eligible selections."""

    location_id: str
    policy: DispatchName


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Persistable observation/action evidence for one boundary."""

    state_version: int
    observation_digest: str
    location_id: str
    simulation_time: float
    action: DispatchAction
    fallback_reason: str | None
    policy_artifact: str


class Policy(Protocol):
    """A local policy chooses one masked action using observable state only."""

    def decide(
        self, observation: PolicyObservation, allowed_actions: tuple[DispatchAction, ...]
    ) -> DispatchAction: ...


class PolicyRuntime:
    """State-versioned dispatch boundary runtime with retained decision trace."""

    def __init__(
        self,
        policy: Policy,
        policy_artifact: str,
        *,
        fallback: DispatchName = "fifo",
        fallback_on_error: bool = True,
    ) -> None:
        self._policy = policy
        self.policy_artifact = policy_artifact
        self.fallback = fallback
        self.fallback_on_error = fallback_on_error
        self._version = 0
        self._observation: PolicyObservation | None = None
        self._allowed: tuple[DispatchAction, ...] = ()
        self._trace: list[DecisionRecord] = []

    def reset(self) -> None:
        self._version = 0
        self._observation = None
        self._allowed = ()
        self._trace.clear()

    def observe(self) -> PolicyObservation:
        if self._observation is None:
            raise PolicyError("no active decision boundary")
        return self._observation

    def available_actions(self) -> tuple[DispatchAction, ...]:
        return self._allowed

    def act(self, action: DispatchAction, expected_state_version: int) -> DispatchAction:
        observation = self.observe()
        if expected_state_version != observation.state_version:
            raise StaleStateError(
                f"expected state {expected_state_version}, "
                f"current state is {observation.state_version}"
            )
        if action not in self._allowed:
            raise MaskedActionError(f"action {action!r} is not available")
        return action

    @property
    def trace(self) -> tuple[DecisionRecord, ...]:
        return tuple(self._trace)

    def choose(
        self,
        location_id: str,
        simulation_time: float,
        current_policy: str,
        queue: tuple[QueueItem, ...],
    ) -> DispatchName:
        """Open a boundary, invoke the local policy, validate, and retain evidence."""
        self._version += 1
        typed_current = _as_dispatch(current_policy)
        observation = PolicyObservation(
            self._version, location_id, simulation_time, typed_current, queue
        )
        allowed = tuple(DispatchAction(location_id, name) for name in _DISPATCH_NAMES)
        self._observation = observation
        self._allowed = allowed
        fallback_reason: str | None = None
        try:
            proposed = self._policy.decide(observation, allowed)
            action = self.act(proposed, observation.state_version)
        except PolicyError as exc:
            if not self.fallback_on_error:
                raise
            action = DispatchAction(location_id, self.fallback)
            fallback_reason = str(exc)
        self._trace.append(
            DecisionRecord(
                observation.state_version,
                observation.digest,
                location_id,
                simulation_time,
                action,
                fallback_reason,
                self.policy_artifact,
            )
        )
        return action.policy


class ReplayPolicy:
    """Replay exact recorded decisions and reject observable-state drift."""

    def __init__(self, trace: tuple[DecisionRecord, ...]) -> None:
        self._trace = trace
        self._index = 0

    def decide(
        self, observation: PolicyObservation, allowed_actions: tuple[DispatchAction, ...]
    ) -> DispatchAction:
        del allowed_actions
        if self._index >= len(self._trace):
            raise ReplayMismatchError("replay trace exhausted")
        expected = self._trace[self._index]
        self._index += 1
        if expected.observation_digest != observation.digest:
            raise ReplayMismatchError(f"observation mismatch at state {observation.state_version}")
        return expected.action


def _as_dispatch(value: str) -> DispatchName:
    if value not in _DISPATCH_NAMES:
        raise MaskedActionError(f"unsupported dispatch policy {value!r}")
    return value
