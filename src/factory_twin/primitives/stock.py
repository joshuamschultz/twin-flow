"""COMP-006 Stock — a material level pulled from by name; blocks when empty."""

from __future__ import annotations

from collections.abc import Generator

import simpy

from factory_twin.primitives.bundle import Bundle


class Stock:
    """pull/put over a material level; stamps material_ready_time; blocks, never negative.

    Backed by `simpy.Container`, which provides the blocking-until-available and
    never-negative guarantees natively: a `get(qty)` request queues until enough
    level exists, and queued requests are only satisfied in order as level permits
    (structure.md primitives contract, COMP-006).
    """

    def __init__(
        self,
        thing: str,
        uom: str,
        env: simpy.Environment,
        initial_qty: float = 0.0,
    ) -> None:
        self.thing = thing
        self.uom = uom
        self.env = env
        self.material_ready_time: float | None = None
        self._container = simpy.Container(env, init=initial_qty)

    @property
    def level(self) -> float:
        """Current qty on hand; never negative."""
        return self._container.level

    def pull(self, thing: str, qty: float, uom: str) -> Generator[simpy.Event, None, Bundle]:
        """Blocking generator; drive with `yield from` (mirrors ResourceAcquirer.acquire).

        Raises ValueError on the first drive, before any blocking wait is created, if
        thing/uom do not match this stock's identity. Otherwise blocks until qty is
        available, then returns a Bundle(qty, thing, uom).
        """
        if thing != self.thing or uom != self.uom:
            raise ValueError(
                f"pull identity mismatch: requested ({thing!r}, {uom!r}), "
                f"stock holds ({self.thing!r}, {self.uom!r})"
            )
        yield self._container.get(qty)
        return Bundle(qty=qty, thing=thing, uom=uom)

    def put(self, bundle: Bundle) -> None:
        """Raises ValueError on identity mismatch; otherwise replenishes the level.

        Stamps material_ready_time = env.now on every call (D-034 wait classification).
        """
        if bundle.thing != self.thing or bundle.uom != self.uom:
            raise ValueError(
                f"put identity mismatch: bundle is ({bundle.thing!r}, {bundle.uom!r}), "
                f"stock holds ({self.thing!r}, {self.uom!r})"
            )
        self._container.put(bundle.qty)
        self.material_ready_time = self.env.now
