"""Pareto menu: non-dominated candidates and the supported flag (COMP-021, REQ-116, D-078).

Pinedo p.81-83, 418-419. Every axis is compared signed (`objectives.signed`: maximize-sense
kinds negated), so all axes are minimized.

- `non_dominated(points, axes)`: ids of the points no other point weakly dominates. Of points
  equal on every axis only the lowest id is kept. Order: signed values axis by axis, then id.
- `supported(points, axes)`: for a non-dominated set, whether some lambda > 0 (sum 1) makes a
  point optimal for the weighted sum of the signed axis values, each axis scaled by its range
  (ties count as optimal, so collinear points are supported). Two axes: the lower convex
  hull, a point is on it when the steepest chord from a point on its left is no steeper than
  the flattest chord to a point on its right. Three axes: an LP (`scipy.optimize.linprog`)
  feasibility check of lambda >= 1 (equivalent to lambda > 0 after rescaling) with
  lambda . (p - q) <= 0 for every other point q.
- `menu(points, axes)`: one `MenuEntry` per non-dominated id, raw values of the axes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from scipy.optimize import linprog  # type: ignore[import-untyped]  # SciPy ships no stubs

from twinflow.scheduling.model import MenuEntry, ObjectiveKind
from twinflow.scheduling.objectives import signed

Points = Mapping[str, Mapping[ObjectiveKind, float]]
Axes = Sequence[ObjectiveKind]
Signed = tuple[float, ...]

TOLERANCE = 1e-9
"""Slack on range-scaled values: a point this close to the hull or the LP face is supported."""


def non_dominated(points: Points, axes: Axes) -> tuple[str, ...]:
    """Ids of the non-dominated points in (signed values, id) order (module docstring)."""
    ordered = sorted(points, key=lambda cid: (_signed(points[cid], axes), cid))
    kept: list[tuple[str, Signed]] = []
    for cid in ordered:
        values = _signed(points[cid], axes)
        if not any(_weakly_dominates(other, values) for _, other in kept):
            kept.append((cid, values))
    return tuple(cid for cid, _ in kept)


def _signed(values: Mapping[ObjectiveKind, float], axes: Axes) -> Signed:
    return tuple(signed(kind, values[kind]) for kind in axes)


def _weakly_dominates(a: Signed, b: Signed) -> bool:
    """a is no worse than b on every axis (equal points included)."""
    return all(x <= y for x, y in zip(a, b, strict=True))


def supported(points: Points, axes: Axes) -> Mapping[str, bool]:
    """Supported flag per id of a non-dominated set (2 axes: hull; 3 axes: LP)."""
    scaled = _scaled(points, axes)
    if len(axes) == 2:
        return _hull_flags(scaled)
    return {cid: _lp_supported(cid, scaled) for cid in scaled}


def _scaled(points: Points, axes: Axes) -> dict[str, Signed]:
    """Signed values shifted to 0 and divided by each axis's range (1 when the range is 0)."""
    signed_points = {cid: _signed(values, axes) for cid, values in points.items()}
    columns = list(zip(*signed_points.values(), strict=True)) if signed_points else []
    lows = [min(column) for column in columns]
    spans = [(max(column) - min(column)) or 1.0 for column in columns]
    return {
        cid: tuple((v - lo) / span for v, lo, span in zip(values, lows, spans, strict=True))
        for cid, values in signed_points.items()
    }


def _hull_flags(scaled: Mapping[str, Signed]) -> dict[str, bool]:
    """Lower-convex-hull membership of 2-axis points sorted by the first axis."""
    order = sorted(scaled, key=lambda cid: (scaled[cid], cid))
    flags: dict[str, bool] = {}
    for i, cid in enumerate(order):
        steepest_left = max((_slope(scaled[j], scaled[cid]) for j in order[:i]), default=None)
        flattest_right = min((_slope(scaled[cid], scaled[k]) for k in order[i + 1 :]), default=None)
        flags[cid] = (
            steepest_left is None
            or flattest_right is None
            or steepest_left <= flattest_right + TOLERANCE
        )
    return flags


def _slope(a: Signed, b: Signed) -> float:
    """Slope of the chord from a to b; non-dominated points have distinct first values."""
    return (b[1] - a[1]) / (b[0] - a[0])


def _lp_supported(cid: str, scaled: Mapping[str, Signed]) -> bool:
    """Whether some lambda >= 1 has lambda . (p - q) <= 0 for every other point q.

    lambda > 0 with sum 1 exists exactly when lambda >= 1 does (rescale); bounding the
    weights below by 1 keeps the solver's absolute feasibility tolerance small relative to
    them, so a point off the face cannot pass on near-zero weights.
    """
    p = scaled[cid]
    others = [q for other, q in scaled.items() if other != cid]
    if not others:
        return True
    rows = [[pk - qk for pk, qk in zip(p, q, strict=True)] for q in others]
    result = linprog(
        [1.0] * len(p),
        A_ub=rows,
        b_ub=[TOLERANCE] * len(others),
        bounds=[(1.0, None)] * len(p),
        method="highs",
    )
    return bool(result.status == 0)


def menu(points: Points, axes: Axes) -> tuple[MenuEntry, ...]:
    """The Pareto menu: non-dominated ids in order, supported flags, raw axis values."""
    ids = non_dominated(points, axes)
    flags = supported({cid: points[cid] for cid in ids}, axes)
    return tuple(
        MenuEntry(
            candidate_id=cid,
            supported=flags[cid],
            values={kind: points[cid][kind] for kind in axes},
        )
        for cid in ids
    )
