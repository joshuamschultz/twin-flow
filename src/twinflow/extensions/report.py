"""COMP-158: the race report, one self-contained HTML page (REQ-207, D-152, TF-006 B-33).

`race.html` is the decision view (DESIGN.md section 3): `race_report_model` builds the race
view model (`twinflow.extensions.report_model`) and `twinflow.report.decision` writes it into
the committed offline template. The page needs no network and holds its own JSON.

The old Plotly and Mermaid page is gone. Its sections moved (BUILD-PLAN B-33): the winner to
the hero, the ranking to All rules and Details, profit over time to "When does it pull ahead",
where the money goes to the waterfall and Compare, rank by day to the timing facts, tuning to
Tuning, profit by view to Details, the Markov maps to the State map, money leaks to Money
leaks and problems to Details.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from twinflow.extensions.race import RaceResult


def race_report_model(result: RaceResult, *, long_check: bool = True) -> dict[str, Any]:
    """The race view model (TF-006 T-270). With `long_check`, the pick and the baseline also
    run for 180 days so the yearly figure can be checked on the first screen (owner
    decision 1)."""
    from twinflow.extensions.report_model import horizon_check, race_view_model

    check = horizon_check(result.project, result) if long_check else None
    return race_view_model(result, result.project, long_check=check)


def write_race_report(
    result: RaceResult,
    path: Path | str,
    *,
    long_check: bool = True,
    model: dict[str, Any] | None = None,
) -> Path:
    """Write the race as the decision view (B-14): `race.html` next to `race.json`.
    Pass `model` when it is already built, so the long check runs once."""
    from twinflow.report import decision

    view = model if model is not None else race_report_model(result, long_check=long_check)
    return decision.write(path, view)


__all__ = ["race_report_model", "write_race_report"]
