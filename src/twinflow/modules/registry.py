"""A tiny, typed plugin registry — the shared discovery surface for every module kind.

Objectives, cost functions, optimizers, and surrogate models all register the same
way: a name maps to a zero-or-more-argument factory. The registry never imports the
things it stores, so a new plugin is added by calling `register`, never by editing a
central switch. This is the module-system analogue of the compiler's single growth
point (D-044): capability is added at one seam, not branched through the core.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    """A name -> factory table for one module kind.

    A factory is any callable returning a `T`; it may take keyword arguments so a
    plugin can be parameterised at creation time (`create("weighted", weights=...)`).
    Registration is fail-closed: a duplicate name raises rather than silently
    shadowing, so two plugins can never quietly claim the same identity.
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._factories: dict[str, Callable[..., T]] = {}

    def register(self, name: str, factory: Callable[..., T]) -> Callable[..., T]:
        """Register `factory` under `name`; returns the factory so it doubles as a
        decorator. Raises `ValueError` on a duplicate name (fail closed)."""
        if name in self._factories:
            raise ValueError(f"{self._kind} {name!r} is already registered")
        self._factories[name] = factory
        return factory

    def create(self, name: str, **kwargs: object) -> T:
        """Build the plugin registered under `name`, passing `kwargs` to its factory.
        Raises `KeyError` naming the kind and the available names on a miss."""
        if name not in self._factories:
            available = ", ".join(self.names()) or "(none)"
            raise KeyError(f"unknown {self._kind} {name!r}; available: {available}")
        return self._factories[name](**kwargs)

    def names(self) -> list[str]:
        """Every registered name, sorted — the discovery list an API/CLI/UI shows."""
        return sorted(self._factories)

    def __contains__(self, name: object) -> bool:
        return name in self._factories
