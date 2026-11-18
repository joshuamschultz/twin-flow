"""B-35: the sweep view model. `twinflow balance` and `twinflow optimize` write `sweep.html`.

Reads `summary.json` (one row per tested point) and `sweep_meta.json` (lever values today,
labels, the base run) from one sweep folder. Uses the same `lever_rows` as the run viewer,
so both pages rank and word the changes the same way (DESIGN.md 4.4).
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any

from twinflow.report.levers import lever_rows, point_label

SWEEP_REPORT = "sweep.html"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _base_run_today(sweep_dir: Path, run_id: str | None) -> tuple[float | None, float | None]:
    """On-time share and late orders per run of the linked base run, when it is there."""
    if not run_id:
        return None, None
    run_dir = sweep_dir.parent / run_id
    intervals = _read(run_dir / "intervals.json") or {}
    detail = _read(run_dir / "run_detail.json") or {}
    on = (intervals.get("on_time_pct") or {}).get("mean")
    late = ((detail.get("per_rep") or {}).get("late_orders")) or []
    return (
        None if on is None else round(float(on) / 100.0, 3),
        round(mean(late), 1) if late else None,
    )


def _pct(share: float) -> str:
    return f"{round(share * 100)}%"


def sweep_view_model(sweep_dir: str | Path) -> dict[str, Any]:
    """The sweep page's data from one sweep folder."""
    folder = Path(sweep_dir)
    summary: list[dict[str, Any]] = list(_read(folder / "summary.json") or [])
    meta: dict[str, Any] = dict(_read(folder / "sweep_meta.json") or {})
    base_values = dict(meta.get("base_values") or {})
    labels = dict((meta.get("labels") or {}).get("centres") or {})
    run_on, run_late = _base_run_today(folder, meta.get("base_run_id"))
    levers, today = lever_rows(
        summary,
        base_values,
        centre_labels=labels,
        today_on_time=run_on,
        today_late=run_late,
    )
    points: list[dict[str, Any]] = [
        {
            "id": str(row.get("sweep_point", i)),
            "levers": {str(k): v for k, v in row["levers"].items()},
            "label": point_label(row["levers"], base_values, labels),
            "today": all(base_values.get(k) == v for k, v in row["levers"].items()),
            "reps": int(row.get("reps") or 0),
            "on_time": round(float(row["on_time_pct"]) / 100.0, 3),
            "late_orders_per_run": row.get("late_orders_per_run"),
            "busy_hours": row.get("busy_hours"),
        }
        for i, row in enumerate(summary)
    ]
    best = levers[0] if levers and (levers[0]["gain_points"] or 0) > 0 else None
    if best is None and levers and today["on_time"] is None:
        best = levers[0]
    on_today = today["on_time"]
    if best is not None and on_today is not None:
        headline = f"{best['label']}. {_pct(on_today)} to {_pct(best['on_time'])} on time."
    elif best is not None:
        headline = f"{best['label']}. {_pct(best['on_time'])} on time."
    elif levers:
        headline = "No tested change helps."
    else:
        headline = "Nothing was tested."
    reps = int(meta.get("reps") or (points[0]["reps"] if points else 0))
    if best is not None:
        lede = " ".join(
            p
            for p in (
                f"{len(levers)} changes were tested on the same {reps} runs as today's plan.",
                best["note"],
            )
            if p
        )
    else:
        lede = (
            f"{len(levers)} changes were tested on the same {reps} runs. None beat today's plan."
            if levers
            else "This sweep has no points."
        )
    kind = str(meta.get("kind") or "balance")
    project = str(meta.get("project") or folder.name)
    model: dict[str, Any] = {
        "view": "sweep",
        "schema_version": 1,
        "about": {
            "title": f"Sweep: {project}",
            "project": project,
            "sweep_id": folder.name,
            "kind": kind,
            "base_run_id": meta.get("base_run_id"),
            "reps": reps,
            "levers": sorted({k for p in points for k in p["levers"]}),
            "objective": meta.get("objective"),
            "method": (
                "Each change was run on the same test runs as today's plan, so the "
                "differences come from the change, not from luck."
            ),
        },
        "answer": {"headline": headline, "lede": lede, "today": today, "best": best},
        "levers": levers,
        "points": points,
        "agent_summary": [headline, lede],
        "warnings": []
        if meta.get("base_run_id") or any(p["today"] for p in points)
        else ["No point matches today's plan, and no base run is linked, so gains are not shown."],
        "problems": [],
    }
    return model


__all__ = ["SWEEP_REPORT", "sweep_view_model"]
