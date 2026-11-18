"""COMP-149: the engine contract every TF-006 engine meets (REQ-194).

An engine starts from a seed (and optionally a saved snapshot, REQ-191), lists the actions
allowed in a state, applies one action per `step` and says when the episode is `done`.
Every step returns one `Move` record of the same shape for every engine (D-130).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from twinflow.state.records import Move, Snapshot

Decide = Callable[[Snapshot], str]
"""A decision rule over the raw snapshot; policy files are wrapped into one of these."""


@runtime_checkable
class Engine(Protocol):
    """What `run_episode`, the race and the checker need from a model engine."""

    name: str

    def start(self, seed: int, from_state: Snapshot | None = None) -> Snapshot: ...

    def actions(self, state: Snapshot) -> tuple[str, ...]: ...

    def step(self, action: str) -> Move: ...

    def done(self) -> bool: ...


def run_episode(
    engine: Engine, decide: Decide, seed: int, from_state: Snapshot | None = None
) -> list[Move]:
    """Run one episode: ask `decide` for an action each step until the engine is done.

    An action the engine does not offer in that state is refused with a `ValueError` that
    names it and the allowed list, so a bad rule never runs silently.
    """
    state = engine.start(seed, from_state)
    moves: list[Move] = []
    while not engine.done():
        action = decide(state)
        allowed = engine.actions(state)
        if action not in allowed:
            raise ValueError(
                f"{engine.name}: action {action!r} is not allowed here; "
                f"choose one of {', '.join(allowed)}"
            )
        move = engine.step(action)
        moves.append(move)
        state = move.after
    return moves
