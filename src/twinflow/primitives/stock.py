"""COMP-006 Stock — a material level pulled from by name; blocks when empty."""

from __future__ import annotations

from collections.abc import Callable, Generator

import simpy

from twinflow.primitives.bundle import Bundle


class Stock:
    """pull/put over a material level; stamps material_ready_time; blocks, never negative.

    Backed by `simpy.Container`, which provides the blocking-until-available and
    never-negative guarantees natively: a `get(qty)` request queues until enough
    level exists, and queued requests are only satisfied in order as level permits
    (structure.md primitives contract, COMP-006).

    An optional `on_change` observer is invoked at the start of every `pull`,
    before the (possibly blocking) `get`. The primitive itself knows nothing about
    reorder points (D-044); the run driver uses this hook to implement reorder-point
    replenishment as a pure Layer-2/3 policy, keeping domain logic out of the
    primitive. Invoking it *before* the get lets a low stock place a replenishment
    order before it runs dry.

    A second optional `on_level_change(stock, delta, kind)` observer fires AFTER the
    level actually changes — a negative `delta`/`"consume"` after a `get`, a positive
    `delta`/`"replenish"` after a `put`. The driver uses it to record the inventory
    log; the primitive stays domain-agnostic.
    """

    def __init__(
        self,
        thing: str,
        uom: str,
        env: simpy.Environment,
        initial_qty: float = 0.0,
        on_change: Callable[[Stock], None] | None = None,
        on_level_change: Callable[[Stock, float, str], None] | None = None,
    ) -> None:
        self.thing = thing
        self.uom = uom
        self.env = env
        self.material_ready_time: float | None = None
        self._container = simpy.Container(env, init=initial_qty)
        self._on_change = on_change
        self._on_level_change = on_level_change

    def set_reorder_hook(self, on_change: Callable[[Stock], None] | None) -> None:
        """Set (or clear) the pre-pull reorder observer after construction — used by
        the driver so a multi-echelon hook can reference the whole stocks map, which
        only exists once every stock has been built."""
        self._on_change = on_change

    def review_reorder(self) -> None:
        """Run the reorder observer now, against the current level. The pre-pull
        review sees the level BEFORE a draw; an upstream echelon drawn down by a
        downstream order needs a review AFTER the draw to notice it fell below its
        own reorder point (a continuous-review top-up for infrequently-pulled stock)."""
        if self._on_change is not None:
            self._on_change(self)

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
        if self._on_change is not None:
            self._on_change(self)  # reorder-point policy places an order before a blocking get
        yield self._container.get(qty)
        if self._on_level_change is not None:
            self._on_level_change(self, -qty, "consume")
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
        if self._on_level_change is not None:
            self._on_level_change(self, bundle.qty, "replenish")
