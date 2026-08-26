"""COMP-027 KpiJsonSidecar — machine-readable JSON of every KPI, a versioned contract.

Serializes the objective-agnostic minimum set (D-042, tech.md): signed lateness
per order, labor hours by pool and skill, machine hours by machine, setup
hours reported SEPARATELY from run hours, and WIP over time. JSON has no tuple
keys, so `KpiSet.labor_hours_by_pool_skill`'s `(pool, skill)` tuple key becomes
two nesting levels; `KpiSet.wip_by_location`'s Polars rows become a flat list
of dicts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from factory_twin.instrumentation.kpis import KpiSet


class KpiJsonSidecar:
    """Writes a versioned JSON sidecar carrying every objective-agnostic KPI field."""

    def write(self, kpis: KpiSet, schema_version: int, out_path: str | Path) -> Path:
        """Serialize `kpis` to `out_path` as versioned JSON and return the path."""
        path = Path(out_path)
        payload: dict[str, Any] = {
            "schema_version": schema_version,
            "lateness_by_order": dict(kpis.lateness_by_order),
            "labor_hours_by_pool_skill": self._nest_labor_hours(kpis.labor_hours_by_pool_skill),
            "machine_hours_by_machine": dict(kpis.machine_hours_by_machine),
            "run_hours": kpis.run_hours,
            "setup_hours": kpis.setup_hours,
            "wip_over_time": kpis.wip_by_location.to_dicts(),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _nest_labor_hours(
        labor_hours_by_pool_skill: dict[tuple[str, str], float],
    ) -> dict[str, dict[str, float]]:
        """`(pool, skill) -> hours` becomes `{pool: {skill: hours}}` — JSON has
        no tuple keys, so the tuple key becomes two nesting levels."""
        nested: dict[str, dict[str, float]] = {}
        for (pool, skill), hours in labor_hours_by_pool_skill.items():
            nested.setdefault(pool, {})[skill] = hours
        return nested
