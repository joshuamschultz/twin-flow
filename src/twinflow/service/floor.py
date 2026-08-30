"""Floor graph + lever suggestions — the visual model the front end draws and tunes.

`build_floor` turns a `CompiledModel` into a node/edge graph a React canvas can lay out:
one node per work center and per stock, edges along each part's routing plus the stock
feeds a center draws from. `suggest_levers` reads the same compiled model for the knobs
a planner actually turns — pool headcounts and per-center machine capacity — each with
its current value and a sensible search range, so the UI can offer them without the
user hand-typing dot-paths.

Everything returned here is plain JSON-serialisable data (dicts/lists of primitives),
so the API layer never has to know the engine's dataclasses.
"""

from __future__ import annotations

from typing import Any

from twinflow.model import CompiledModel


def build_floor(compiled: CompiledModel) -> dict[str, Any]:
    """A `{nodes, edges, parts}` graph for the whole floor.

    Nodes: every work center (with its capacity, labor skill, and time-model kind)
    and every declared stock. Edges: each part's routing step-to-step (labelled by
    part), plus a feed edge from a stock to the first center that consumes it.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for spec in compiled.locations:
        nodes.append(
            {
                "id": spec.location_id,
                "kind": "location",
                "label": spec.location_id,
                "capacity": spec.capacity,
                "labor_skill": spec.labor_skill,
                "machine": spec.machine.machine_id,
                "time_model": spec.time_model.kind,
            }
        )

    for stock in compiled.stocks:
        nodes.append(
            {"id": f"stock:{stock.name}", "kind": "stock", "label": stock.name, "uom": stock.uom}
        )

    consumed_things = _consumed_things(compiled)
    stock_names = {stock.name for stock in compiled.stocks}
    for stock_name in stock_names:
        target = consumed_things.get(stock_name)
        if target is not None:
            edges.append(
                {"source": f"stock:{stock_name}", "target": target, "label": stock_name}
            )

    for part, steps in compiled.routing.items():
        for current_id, next_id in zip(steps, steps[1:], strict=False):
            edges.append({"source": current_id, "target": next_id, "label": part})

    parts = [
        {"part": part, "steps": list(steps)} for part, steps in sorted(compiled.routing.items())
    ]
    return {"nodes": nodes, "edges": edges, "parts": parts}


def suggest_levers(compiled: CompiledModel) -> list[dict[str, Any]]:
    """The tunable knobs the UI offers, each as `{path, label, current, min, max}`.

    Pool headcounts (`labor.pools[i].headcount`, searched from 1 to current+3) and
    every work center's machine capacity (`locations[name].capacity`, 1 to
    current+2). The dot-paths follow the same lever scheme sweeps and the scoring
    surface already use.
    """
    levers: list[dict[str, Any]] = []

    for index, pool in enumerate(compiled.labor_pools):
        levers.append(
            {
                "path": f"labor.pools[{index}].headcount",
                "label": f"{pool.name} headcount",
                "current": pool.headcount,
                "min": 1,
                "max": pool.headcount + 3,
            }
        )

    for spec in compiled.locations:
        levers.append(
            {
                "path": f"locations[{spec.location_id}].capacity",
                "label": f"{spec.location_id} machines",
                "current": spec.capacity,
                "min": 1,
                "max": spec.capacity + 2,
            }
        )

    return levers


def _consumed_things(compiled: CompiledModel) -> dict[str, str]:
    """Map each consumed `thing` to the first location that consumes it (routing
    order), so a stock feed edge points at where the material actually enters."""
    consumed: dict[str, str] = {}
    for steps in compiled.routing.values():
        for location_id in steps:
            spec = next(s for s in compiled.locations if s.location_id == location_id)
            for thing in spec.pull_rule.setup_key_of:
                consumed.setdefault(thing, location_id)
    return consumed
