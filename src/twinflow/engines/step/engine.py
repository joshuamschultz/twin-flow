"""COMP-150: the make-to-stock step engine, ported exactly from the RL repo (REQ-195, REQ-196).

One line, N products in families, Poisson demand each day. A day is: choose a product to
run (or idle); make first (a changeover loses part of the day's output, and output that
would overflow the storage cap is not made); then sell; unmet demand is lost. The ledger is
`revenue - production - changeover - holding(end stock) - lost_sales`; the changeover dollars
go in `Ledger.setup` and `late` is always 0.

Demand copies the RL repo's numpy call exactly (D-135):
`default_rng(seed).poisson(rates, size=(episodes, horizon, products))`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import numpy.typing as npt

from twinflow.engines.step.model import Changeover, StepModel
from twinflow.state.records import Ledger, Move, Snapshot

IDLE = "idle"

DemandDraws = npt.NDArray[np.int64]


def demand_draws(model: StepModel, episodes: int, seed: int) -> DemandDraws:
    """Demand of shape (episodes, days, products). Same seed, same dice (D-135)."""
    rng = np.random.default_rng(seed)
    return rng.poisson(model.demand_rates, size=(episodes, model.horizon, len(model.products)))


def changeover_between(model: StepModel, source: int, target: int) -> Changeover:
    """The changeover kind for switching the line from product `source` to `target`."""
    same = model.families[source] == model.families[target]
    return model.within if same else model.across


def units_made(model: StepModel, inv: Sequence[int], setup: int, chosen: int) -> list[int]:
    """Output per product when running `chosen` (the idle position makes nothing).

    A changeover costs part of the day; output that would overflow the storage cap is not
    made (`factory_mdp.transition.units_produced`). Shared with the rules (D-135).
    """
    made = [0] * len(model.products)
    if chosen != len(model.products):
        product = model.products[chosen]
        lost = 0.0 if chosen == setup else changeover_between(model, setup, chosen).output_lost
        room = product.max_storage - inv[chosen]
        made[chosen] = max(0, min(round(product.run_rate * (1.0 - lost)), room))
    return made


class StepEngine:
    """Meets the `Engine` contract. Action ids are the product ids plus `"idle"`.

    With `demand` (from `demand_draws`) the `seed` given to `start` picks the episode row, so
    every policy can be run on the same dice. Without it, `start(seed)` draws one fresh
    episode from that seed (the same numbers as `demand_draws(model, 1, seed)[0]`).
    """

    name = "make_to_stock"

    def __init__(self, model: StepModel, demand: DemandDraws | None = None) -> None:
        n = len(model.products)
        if demand is not None and (demand.ndim != 3 or demand.shape[1:] != (model.horizon, n)):
            raise ValueError(
                f"demand draws must have shape (episodes, {model.horizon}, {n}), "
                f"not {tuple(demand.shape)}"
            )
        self._model = model
        self._draws = demand
        self._ids = model.product_ids
        self._index = {pid: i for i, pid in enumerate(self._ids)}
        self._holding = [model.holding_cost(p) for p in self._ids]
        self._lost_sale = [model.lost_sale_cost(p) for p in self._ids]
        self._demand: DemandDraws | None = None
        self._state: Snapshot | None = None
        self._inv: list[int] = []
        self._setup = 0

    @property
    def model(self) -> StepModel:
        return self._model

    # --- the contract ------------------------------------------------------------------------

    def start(self, seed: int, from_state: Snapshot | None = None) -> Snapshot:
        self._demand = self._episode_demand(seed)
        if from_state is None:
            self._inv, self._setup = self._initial()
            self._state = self._snapshot(0, self._inv, self._setup)
        else:
            self._inv, self._setup = self._read(from_state)
            self._state = from_state
        return self._state

    def actions(self, state: Snapshot) -> tuple[str, ...]:
        return (*self._ids, IDLE)

    def done(self) -> bool:
        return self._state is not None and self._state.period >= self._model.horizon

    def step(self, action: str) -> Move:
        before, demand = self._state, self._demand
        if before is None or demand is None:
            raise ValueError("call start() before step()")
        if self.done():
            raise ValueError("the episode is finished; call start() to begin another")
        if action != IDLE and action not in self._index:
            allowed = ", ".join(self.actions(before))
            raise ValueError(f"action {action!r} is not a product; choose one of {allowed}")
        day = before.period
        today = [int(x) for x in demand[day]]
        chosen = len(self._ids) if action == IDLE else self._index[action]
        made = units_made(self._model, self._inv, self._setup, chosen)
        stock = [i + m for i, m in zip(self._inv, made, strict=True)]
        sold = [min(s, d) for s, d in zip(stock, today, strict=True)]
        lost = [d - s for d, s in zip(today, sold, strict=True)]
        inv_next = [s - x for s, x in zip(stock, sold, strict=True)]
        switch = chosen != len(self._ids) and chosen != self._setup
        changeover = changeover_between(self._model, self._setup, chosen).cost if switch else 0.0
        setup_next = self._setup if chosen == len(self._ids) else chosen
        ledger = self._ledger(made, sold, lost, inv_next, changeover)
        after = self._snapshot(day + 1, inv_next, setup_next)
        events = self._events(today, made, sold, lost)
        self._inv, self._setup, self._state = inv_next, setup_next, after
        return Move(day, before, action, self._model.line_id, "period", events, after, ledger)

    # --- money and flows ---------------------------------------------------------------------

    def _ledger(
        self, made: list[int], sold: list[int], lost: list[int], inv: list[int], changeover: float
    ) -> Ledger:
        products = self._model.products
        return Ledger(
            revenue=sum(x * p.price for x, p in zip(sold, products, strict=True)),
            production=sum(m * p.unit_cost for m, p in zip(made, products, strict=True)),
            setup=changeover,
            holding=sum(h * i for h, i in zip(self._holding, inv, strict=True)),
            lost_sales=sum(c * x for c, x in zip(self._lost_sale, lost, strict=True)),
        )

    def _events(
        self, demand: list[int], made: list[int], sold: list[int], lost: list[int]
    ) -> dict[str, float]:
        events: dict[str, float] = {}
        for i, pid in enumerate(self._ids):
            events[f"demand.{pid}"] = float(demand[i])
            events[f"made.{pid}"] = float(made[i])
            events[f"sold.{pid}"] = float(sold[i])
            events[f"lost.{pid}"] = float(lost[i])
        return events

    # --- states ------------------------------------------------------------------------------

    def _episode_demand(self, seed: int) -> DemandDraws:
        if self._draws is None:
            return np.asarray(demand_draws(self._model, 1, seed)[0], dtype=np.int64)
        if not 0 <= seed < self._draws.shape[0]:
            raise ValueError(
                f"episode {seed} is not in the demand draws (they hold {self._draws.shape[0]} "
                "episodes); the seed picks the episode row"
            )
        return np.asarray(self._draws[seed], dtype=np.int64)

    def _initial(self) -> tuple[list[int], int]:
        """Day 0: `start_cover_days` of average demand in stock (to the cap); product 0 set up."""
        cover = self._model.start_cover_days
        inv = [min(p.max_storage, round(cover * p.demand_rate)) for p in self._model.products]
        return inv, 0

    def _snapshot(self, day: int, inv: list[int], setup: int) -> Snapshot:
        return Snapshot(
            t=float(day),
            period=day,
            stock={pid: float(units) for pid, units in zip(self._ids, inv, strict=True)},
            setup={self._model.line_id: self._ids[setup]},
        )

    def _read(self, state: Snapshot) -> tuple[list[int], int]:
        """The engine's own numbers from a saved snapshot, refusing one that does not fit."""
        line = self._model.line_id
        if not 0 <= state.period <= self._model.horizon:
            raise ValueError(
                f"the saved state is at period {state.period}, but this model runs "
                f"periods 0 to {self._model.horizon}"
            )
        _check_ids(state.stock, self._ids, "stock")
        if line not in state.setup:
            raise ValueError(f"the saved state has no setup for the line {line!r}")
        current = state.setup[line]
        if current not in self._index:
            raise ValueError(f"the saved setup {current!r} is not a product of this model")
        inv: list[int] = []
        for pid in self._ids:
            units = state.stock[pid]
            if units < 0 or not float(units).is_integer():
                raise ValueError(f"stock of {pid} must be a whole number of units, not {units!r}")
            inv.append(int(units))
        return inv, self._index[current]


def _check_ids(found: Mapping[str, float], ids: tuple[str, ...], what: str) -> None:
    missing = [p for p in ids if p not in found]
    unknown = [p for p in found if p not in ids]
    if missing:
        raise ValueError(f"the saved {what} has no entry for {', '.join(missing)}")
    if unknown:
        raise ValueError(f"the saved {what} names {', '.join(unknown)}, not products of this model")
