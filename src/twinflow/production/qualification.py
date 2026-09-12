"""Bounded setup-sample qualification state machine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import count
from math import isfinite
from threading import RLock
from types import MappingProxyType
from typing import Any


class QualificationStateError(ValueError):
    """A qualification transition is invalid for the current state."""


class UnknownTrialError(KeyError):
    """A result references a trial that is not awaiting a result."""


class QualificationApprovalError(ValueError):
    """A green shortcut is missing explicit approval evidence."""


class QualificationLimitError(ValueError):
    """The configured qualification iteration limit has been reached."""


_CONTEXT_COUNTER = count(1)


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Copy a mapping so later caller mutation cannot alter a run's rules."""

    return MappingProxyType({key: _freeze_value(item) for key, item in (value or {}).items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class QualificationPlan:
    part_id: str
    machine_id: str
    recipe_revision: str
    sample_qty: float
    test_route: tuple[str, ...]
    acceptance_spec: Mapping[str, Any]
    max_iterations: int
    green_shortcut: Mapping[str, Any] | None = None
    context_id: str | None = None

    def __post_init__(self) -> None:
        if not self.part_id or not self.machine_id or not self.recipe_revision:
            raise ValueError("part_id, machine_id, and recipe_revision are required")
        if (
            not isinstance(self.sample_qty, (int, float))
            or isinstance(self.sample_qty, bool)
            or not isfinite(float(self.sample_qty))
            or self.sample_qty <= 0
        ):
            raise ValueError("sample_qty must be positive")
        if not self.test_route:
            raise ValueError("test_route must contain at least one operation")
        if not isinstance(self.acceptance_spec, Mapping) or not self.acceptance_spec:
            raise ValueError("acceptance_spec must contain at least one criterion")
        for name, bounds in self.acceptance_spec.items():
            if not isinstance(name, str) or not name:
                raise ValueError("acceptance_spec keys must be non-empty strings")
            if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
                raise ValueError(f"acceptance_spec[{name!r}] must contain lower and upper bounds")
            lower, upper = bounds
            if any(
                isinstance(bound, bool)
                or not isinstance(bound, (int, float))
                or not isfinite(float(bound))
                for bound in (lower, upper)
            ) or float(lower) > float(upper):
                raise ValueError(f"acceptance_spec[{name!r}] must be finite and ordered")
        if (
            not isinstance(self.max_iterations, int)
            or isinstance(self.max_iterations, bool)
            or self.max_iterations <= 0
        ):
            raise ValueError("max_iterations must be positive")
        object.__setattr__(self, "test_route", tuple(self.test_route))
        object.__setattr__(self, "acceptance_spec", _freeze_mapping(self.acceptance_spec))
        object.__setattr__(
            self,
            "green_shortcut",
            _freeze_mapping(self.green_shortcut) if self.green_shortcut else None,
        )
        if self.context_id is not None and not self.context_id.strip():
            raise ValueError("context_id must be non-empty when supplied")


@dataclass(frozen=True, slots=True)
class QualificationTrial:
    sample_lot_id: str
    iteration: int
    test_route: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QualificationEvent:
    kind: str
    sample_lot_id: str
    iteration: int
    measurements: Mapping[str, Any]
    passed: bool


class QualificationState:
    """Per-run, thread-safe qualification transitions."""

    def __init__(self, plan: QualificationPlan) -> None:
        self._plan = plan
        self._context_id = plan.context_id or f"context-{next(_CONTEXT_COUNTER)}"
        self._lock = RLock()
        self._status = "unqualified"
        self._iteration = 0
        self._pending: QualificationTrial | None = None
        self._events: list[QualificationEvent] = []
        self._shortcut_evidence_id: str | None = None
        self._approved_revision: str | None = None

    @property
    def plan(self) -> QualificationPlan:
        return self._plan

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def iteration(self) -> int:
        with self._lock:
            return self._iteration

    @property
    def ready_for_production(self) -> bool:
        with self._lock:
            return self._status == "approved"

    @property
    def approved_revision(self) -> str | None:
        with self._lock:
            return self._approved_revision

    @property
    def shortcut_evidence_id(self) -> str | None:
        with self._lock:
            return self._shortcut_evidence_id

    @property
    def sample_events(self) -> tuple[QualificationEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def start_trial(self) -> QualificationTrial:
        with self._lock:
            if self._status == "approved":
                raise QualificationStateError("qualification is already approved")
            if self._pending is not None:
                raise QualificationStateError("a trial is already awaiting its result")
            if self._iteration >= self._plan.max_iterations:
                self._status = "unresolved"
                raise QualificationLimitError("qualification iteration limit reached")
            self._iteration += 1
            trial = QualificationTrial(
                sample_lot_id=(
                    f"sample:{self._plan.part_id}:{self._plan.machine_id}:"
                    f"{self._plan.recipe_revision}:{self._context_id}:{self._iteration}"
                ),
                iteration=self._iteration,
                test_route=self._plan.test_route,
            )
            self._pending = trial
            self._status = "awaiting_test"
            return trial

    def record_result(
        self,
        sample_lot_id: str,
        passed: bool,
        measurements: Mapping[str, Any],
    ) -> QualificationEvent:
        with self._lock:
            if self._pending is None or self._pending.sample_lot_id != sample_lot_id:
                raise UnknownTrialError(sample_lot_id)
            trial = self._pending
            if not isinstance(passed, bool):
                raise TypeError("passed must be a bool")
            if not isinstance(measurements, Mapping):
                raise TypeError("measurements must be a mapping")
            copied_measurements = _freeze_mapping(measurements)
            in_spec = self._measurements_in_spec(copied_measurements)
            # Keep the trial pending when input validation fails; callers may retry
            # with corrected evidence without consuming an iteration.
            if any(isinstance(value, bool) for value in copied_measurements.values()):
                raise TypeError("measurement values must be numeric, not bool")
            self._pending = None
            approved = passed and in_spec
            if approved:
                self._status = "approved"
                self._approved_revision = self.plan.recipe_revision
                kind = "approved"
            elif trial.iteration >= self._plan.max_iterations:
                self._status = "unresolved"
                kind = "unresolved"
            else:
                self._status = "adjusting"
                kind = "adjustment"
            event = QualificationEvent(
                kind, sample_lot_id, trial.iteration, copied_measurements, approved
            )
            self._events.append(event)
            return event

    def apply_green_shortcut(self) -> None:
        with self._lock:
            shortcut = self.plan.green_shortcut
            if (
                not shortcut
                or shortcut.get("approved") is not True
                or not shortcut.get("evidence_id")
            ):
                raise QualificationApprovalError("green shortcut requires approved evidence_id")
            if self._pending is not None:
                raise QualificationStateError("cannot shortcut a trial awaiting test")
            self._status = "approved"
            self._approved_revision = self.plan.recipe_revision
            self._shortcut_evidence_id = str(shortcut["evidence_id"])

    def _measurements_in_spec(self, measurements: Mapping[str, Any]) -> bool:
        for key, bounds in self._plan.acceptance_spec.items():
            if key not in measurements or not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
                return False
            value = measurements[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or not (bounds[0] <= value <= bounds[1])
            ):
                return False
        return True
