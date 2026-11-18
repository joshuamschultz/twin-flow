"""A toy engine: a small bakery with two breads. Copy it and swap in your own world.

An engine is a class that plays a world one step at a time. The race and the checker call
the same four methods on every engine: `start`, `actions`, `step` and `done`.
"""

import numpy

from twinflow.state.records import Ledger, Move, Snapshot

NAME = "bakery"  # the capsule says `model.engine: bakery` to pick this file


class Engine:
    name = "bakery"

    def __init__(self, model):
        # `model` is the `model:` section of the capsule, as a plain dict.
        self.horizon = int(model.get("horizon", 14))
        self.products = list(model.get("products", ["rye", "white"]))
        self.price = float(model.get("price", 4.0))
        self.cost = float(model.get("unit_cost", 1.5))

    def facts(self):
        # Plain facts that policies can read, like `state.demand_rate`.
        rates = {p: 3.0 for p in self.products}
        return {"products": tuple(self.products), "demand_rate": rates, "horizon": self.horizon}

    def start(self, seed, from_state=None):
        # The same seed must give the same dice. That keeps races fair.
        self.rng = numpy.random.default_rng(seed)
        stock = {p: 6.0 for p in self.products}
        self.state = Snapshot(t=0.0, period=0, stock=stock, setup={"oven": self.products[0]})
        return self.state

    def actions(self, state):
        # What the baker may do today: bake one product, or rest.
        return (*self.products, "idle")

    def step(self, action):
        before = self.state
        stock = dict(before.stock)
        baked = 0.0
        if action != "idle":
            stock[action] += 8
            baked = 8.0
        demand = {p: int(self.rng.poisson(3.0)) for p in self.products}
        sold = {p: min(stock[p], demand[p]) for p in self.products}
        for p in self.products:
            stock[p] -= sold[p]
        oven = before.setup["oven"] if action == "idle" else action
        after = Snapshot(
            t=before.t + 1, period=before.period + 1, stock=stock, setup={"oven": oven}
        )
        events = {f"demand.{p}": float(demand[p]) for p in self.products}
        events.update({f"sold.{p}": float(sold[p]) for p in self.products})
        self.state = after
        # Dollars by cause. The race ranks by revenue less every cost.
        ledger = Ledger(
            revenue=self.price * sum(sold.values()),
            production=self.cost * baked,
            holding=0.05 * sum(stock.values()),
        )
        return Move(before.period, before, action, "oven", "period", events, after, ledger)

    def done(self):
        return self.state.period >= self.horizon
