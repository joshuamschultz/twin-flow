"""B-26 G6: sweep points as plain-word changes ("Add a second shaft mill"), best first.

A sweep (`twinflow balance`) writes `summary.json` (one row per point) and `sweep_meta.json`
(the base run it belongs to and each lever's value today). `lever_rows` turns the two into
the `levers` list the run viewer and the sweep view share.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_PATH = re.compile(r"^(?P<section>[a-z_]+)\[(?P<name>[^\]]+)\]\.(?P<field>.+)$")
_POOL = re.compile(r"^labor\.pools\[(?P<name>[^\]]+)\]\.(?P<field>.+)$")
_ORDINALS = {
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
}
_DISPATCH = {
    "fifo": "in arrival order",
    "edd": "by due date",
    "spt": "shortest job first",
    "critical_ratio": "by how close each job is to late",
}
SMALL_GAIN = 0.02
"""Two changes on the same lever whose on-time shares differ by less than this: the smaller
change ranks first and the bigger one says it is not worth it."""


def plain(name: str) -> str:
    """G9 fallback: the id with underscores as spaces."""
    return name.replace("_", " ")


def _lower(label: str) -> str:
    return label[:1].lower() + label[1:] if label[:2] != label[:2].upper() else label


def lever_label(
    path: str, value: Any, base: Any, centre_labels: Mapping[str, str] | None = None
) -> str:
    """One lever value in plain words."""
    centre_labels = centre_labels or {}
    pool = _POOL.match(path)
    if pool:
        if pool["field"] == "headcount":
            return f"{value} people in {plain(pool['name'])}"
        return f"Set {plain(pool['field'])} of {plain(pool['name'])} to {value}"
    match = _PATH.match(path)
    if match and match["section"] == "locations":
        name = centre_labels.get(match["name"], plain(match["name"]))
        field = match["field"]
        if field == "capacity" and isinstance(value, int):
            today = base if isinstance(base, int) else 1
            if value == today:
                return f"Keep {value} {_lower(name)}" if value != 1 else f"Keep one {_lower(name)}"
            if value > today and value in _ORDINALS:
                return f"Add a {_ORDINALS[value]} {_lower(name)}"
            return f"Run {value} of the {_lower(name)}"
        if field == "dispatch":
            return f"{name}: run jobs {_DISPATCH.get(str(value), str(value))}"
        if field == "time_model.rate" and isinstance(value, (int, float)) and base:
            change = (float(value) / float(base) - 1.0) * 100.0
            word = "faster" if change > 0 else "slower"
            return f"Make the {_lower(name)} {abs(change):.0f}% {word}"
    return f"Set {path} to {value}"


def point_label(
    levers: Mapping[str, Any], base: Mapping[str, Any], names: Mapping[str, str]
) -> str:
    changed = [(p, v) for p, v in levers.items() if base.get(p, _MISSING) != v]
    parts = [lever_label(p, v, base.get(p), names) for p, v in changed]
    if not parts:
        return "What you do today"
    text = " and ".join([parts[0]] + [p[:1].lower() + p[1:] for p in parts[1:]])
    return text


_MISSING = object()


def _size(levers: Mapping[str, Any], base: Mapping[str, Any]) -> float:
    total = 0.0
    for path, value in levers.items():
        today = base.get(path)
        if isinstance(value, (int, float)) and isinstance(today, (int, float)):
            total += abs(float(value) - float(today))
        elif today != value:
            total += 1.0
    return total


def _late_note(today: float | None, late: float | None) -> str:
    if today is None or late is None:
        return ""
    if late < today:
        return f"Late orders drop from {today:.1f} to {late:.1f} per run."
    if late > today:
        return f"Late orders rise from {today:.1f} to {late:.1f} per run."
    return f"Late orders stay at {late:.1f} per run."


def lever_rows(
    summary: Sequence[Mapping[str, Any]],
    base_values: Mapping[str, Any],
    *,
    centre_labels: Mapping[str, str] | None = None,
    today_on_time: float | None = None,
    today_late: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tested levers best first, and the "today" figures they are measured against.

    The sweep point equal to today's model is the base when the sweep has one (same test
    runs, so a fair comparison); otherwise the run's own figures are the base.
    """
    names = centre_labels or {}
    today_point = next(
        (
            r
            for r in summary
            if all(base_values.get(p, _MISSING) == v for p, v in r["levers"].items())
        ),
        None,
    )
    if today_point is not None:
        today_on_time = float(today_point["on_time_pct"]) / 100.0
        today_late = _opt(today_point.get("late_orders_per_run"), today_late)
    rows: list[dict[str, Any]] = []
    for point in summary:
        if point is today_point:
            continue
        on_time = float(point["on_time_pct"]) / 100.0
        late = _opt(point.get("late_orders_per_run"), None)
        gain = None if today_on_time is None else round((on_time - today_on_time) * 100.0, 1)
        rows.append(
            {
                "id": ",".join(f"{p}={v}" for p, v in point["levers"].items()),
                "label": point_label(point["levers"], base_values, names),
                "tested": True,
                "on_time": round(on_time, 3),
                "gain_points": gain,
                "late_orders_per_run": None if late is None else round(late, 1),
                "note": _late_note(today_late, late)
                if gain is None or gain > 0
                else "This does not help. It makes on time worse or the same.",
                "_size": _size(point["levers"], base_values),
                "_paths": tuple(sorted(point["levers"])),
            }
        )
    rows.sort(key=lambda r: (-r["on_time"], r["_size"]))
    _prefer_small_changes(rows)
    for row in rows:
        del row["_size"], row["_paths"]
    today = {
        "on_time": None if today_on_time is None else round(today_on_time, 3),
        "late_orders_per_run": None if today_late is None else round(today_late, 1),
    }
    return rows, today


def _prefer_small_changes(rows: list[dict[str, Any]]) -> None:
    """Move a smaller change ahead of a bigger one on the same lever when the bigger one
    gains almost nothing more, and say so on the bigger one."""
    changed = True
    while changed:
        changed = False
        for i in range(len(rows) - 1):
            big, small = rows[i], rows[i + 1]
            if (
                big["_paths"] == small["_paths"]
                and small["_size"] < big["_size"]
                and big["on_time"] - small["on_time"] < SMALL_GAIN
                and (small["gain_points"] or 0) > 0
            ):
                rows[i], rows[i + 1] = small, big
                points = round((big["on_time"] - small["on_time"]) * 100.0)
                unit = "point" if points == 1 else "points"
                big["note"] = (
                    f"Only {points} {unit} better than: {small['label'].lower()}. "
                    "Not worth the extra."
                    if points >= 1
                    else f"No better than: {small['label'].lower()}. Not worth the extra."
                )
                changed = True


def _opt(value: Any, default: float | None) -> float | None:
    return float(value) if isinstance(value, (int, float)) else default


__all__ = ["lever_label", "lever_rows", "plain", "point_label"]
