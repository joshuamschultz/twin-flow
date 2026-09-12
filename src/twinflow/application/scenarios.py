"""The only workspace adapter that knows capsule and simulation implementation details."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from twinflow.domain import registry
from twinflow.instrumentation import compute_kpis
from twinflow.instrumentation.aggregate import aggregate_kpis
from twinflow.instrumentation.sweep import orders_frame
from twinflow.plan.driver import RunDriver
from twinflow.scenario import CapsuleValidationError, ScenarioCapsule, import_legacy, schema


def capsule_schema() -> dict[str, Any]:
    return schema()


def validate_content(content: str) -> ScenarioCapsule:
    from twinflow.scenario import loads

    capsule = loads(content)
    issues = capsule.validate()
    if issues:
        raise CapsuleValidationError(issues)
    return capsule


def example_capsule(model: Path, plan: Path) -> ScenarioCapsule:
    return import_legacy(model, plan)


def describe(capsule: ScenarioCapsule) -> dict[str, Any]:
    domain = capsule.model.get("domain", "manufacturing")
    if domain != "manufacturing":
        return describe_domain(capsule, str(domain))
    compiled = capsule.compile()
    nodes = [
        {
            "id": spec.location_id,
            "kind": "process",
            "label": spec.location_id,
            "capacity": spec.capacity,
            "skill": spec.labor_skill,
        }
        for spec in compiled.model.locations
    ]
    nodes += [
        {"id": f"stock:{stock.name}", "kind": "stock", "label": stock.name, "unit": stock.uom}
        for stock in compiled.model.stocks
    ]
    edges = [
        {"source": a, "target": b, "label": part}
        for part, steps in compiled.model.routing.items()
        for a, b in zip(steps, steps[1:], strict=False)
    ]
    return {
        "domain": "manufacturing",
        "nodes": nodes,
        "edges": edges,
        "orders": len(compiled.plan),
        "processes": len(compiled.model.locations),
        "stocks": len(compiled.model.stocks),
    }


def evaluate_capsule(
    data: dict[str, Any],
    out: Path,
    reps: int,
    seed: int,
    canceled: Callable[[], bool],
) -> dict[str, Any]:
    """Sequential bounded replications; no thread-global paths or nested CPU pools."""
    import time

    capsule = ScenarioCapsule.from_dict(data)
    domain = str(capsule.model.get("domain", "manufacturing"))
    if domain != "manufacturing":
        return evaluate_domain(capsule, domain, out, reps, seed, canceled)
    compiled = capsule.compile()
    per_rep = []
    outcomes = []
    out.mkdir(parents=True, exist_ok=True)
    (out / "scenario.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    start = time.monotonic()
    for index in range(reps):
        if canceled():
            raise InterruptedError("Experiment canceled")
        remaining = 120 - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError("Experiment exceeded 120 second compute budget")
        run = RunDriver(compiled.model).run(
            compiled.plan,
            seed=seed,
            replication_index=index,
            artifact_dir=out / f"rep-{index:04}",
            max_sim_time=31_536_000,
            max_events=1_000_000,
            max_wall_seconds=min(remaining, 30),
        )
        kpi = compute_kpis(
            run.event_log_path,
            orders_frame(compiled.plan, compiled.model, run.event_log_path),
            run.horizon,
        )
        per_rep.append(kpi)
        outcomes.append(
            {
                "replication": index,
                "outcome": run.outcome,
                "termination_reason": run.termination_reason,
            }
        )
    aggregate = aggregate_kpis(per_rep)
    result = {
        "schema_version": "1.0",
        "domain": "manufacturing",
        "replications": reps,
        "seed": seed,
        "snapshot": capsule.snapshot,
        "assumptions": capsule.assumptions,
        "intervals": asdict(aggregate),
        "outcomes": outcomes,
        "metrics": {
            "on_time_pct": aggregate.on_time_pct.mean,
            "run_hours": sum(k.run_hours for k in per_rep) / reps,
            "setup_hours": sum(k.setup_hours for k in per_rep) / reps,
        },
        "per_replication": [
            {"on_time_pct": k.on_time_pct, "completion_by_order": k.completion_by_order}
            for k in per_rep
        ],
        "validity": "provisional",
        "interpretation": (
            "Simulation outcomes conditional on model assumptions; not customer commitments."
        ),
    }
    (out / "result.json").write_text(
        json.dumps(result, indent=2, allow_nan=False), encoding="utf-8"
    )
    return result


def describe_domain(capsule: ScenarioCapsule, domain: str) -> dict[str, Any]:
    description: dict[str, Any] = dict(registry.describe(domain, capsule.model, capsule.snapshot))
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    if domain == "supply_chain":
        for item in capsule.model.get("items", []):
            nodes.append({"id": item["id"], "label": item["id"], "kind": item["kind"]})
        for bom in capsule.model.get("bom_revisions", []):
            for line in bom.get("lines", []):
                edges.append(
                    {
                        "source": line["item_id"],
                        "target": bom["assembly_item_id"],
                        "label": str(line["quantity"]) + " " + line["uom"],
                    }
                )
        count = len(capsule.snapshot.get("orders", []))
    else:
        for node in description.get("nodes", []):
            nodes.append({**node, "label": node["id"], "kind": "task", "skill": node.get("role")})
        for edge in description.get("edges", []):
            edges.append({"source": edge["from"], "target": edge["to"], "label": "requires"})
        count = len(capsule.snapshot.get("cases", []))
    return {
        "domain": domain,
        "nodes": nodes,
        "edges": edges,
        "orders": count,
        "processes": len(nodes),
        "stocks": len(capsule.snapshot.get("inventory", [])),
        "description": description,
    }


def evaluate_domain(
    capsule: ScenarioCapsule,
    domain: str,
    out: Path,
    reps: int,
    seed: int,
    canceled: Callable[[], bool],
) -> dict[str, Any]:
    import time

    out.mkdir(parents=True, exist_ok=True)
    (out / "scenario.json").write_text(json.dumps(capsule.to_dict(), indent=2), encoding="utf-8")
    samples: list[dict[str, Any]] = []
    started = time.monotonic()
    for index in range(reps):
        if canceled():
            raise InterruptedError("Experiment canceled")
        remaining = 120 - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Experiment exceeded 120 second compute budget")
        sample = registry.evaluate(
            domain,
            capsule.model,
            capsule.snapshot,
            seed=seed + index,
            artifact_dir=str(out / f"rep-{index:04}"),
            limits={
                "replications": 1,
                "max_events": 1_000_000,
                "max_sim_time": 31_536_000,
                "max_wall_seconds": min(remaining, 30),
            },
        )
        samples.append(sample)
    keys = samples[0].get("metrics", {}).keys()
    metrics = {key: sum(float(sample["metrics"][key]) for sample in samples) / reps for key in keys}
    result = {
        "schema_version": "1.0",
        "domain": domain,
        "replications": reps,
        "seed": seed,
        "snapshot": capsule.snapshot,
        "assumptions": capsule.assumptions,
        "metrics": metrics,
        "intervals": {},
        "per_replication": samples,
        "outcomes": [
            {"replication": i, "outcome": s["status"], "termination_reason": None}
            for i, s in enumerate(samples)
        ],
        "validity": "provisional",
        "interpretation": "Outcomes conditional on supplied model assumptions; not commitments.",
    }
    (out / "result.json").write_text(
        json.dumps(result, indent=2, allow_nan=False), encoding="utf-8"
    )
    return result
