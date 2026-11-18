"""`twinflow run --orders` prints when each order finishes and how often it is late.

A job-shop planner wants one line per order: due, typical finish, bad-case finish and the
share of runs in which it was late. Then the busiest work centers, to find the bottleneck.
With a work calendar, the times print as dates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from twinflow.cli.main import main

ROOT = Path(__file__).resolve().parents[2]
CALENDAR = ROOT / "examples" / "hmlv-calendar"
SHOP = ROOT / "examples" / "cnc-shop"


def _run(
    folder: Path, reps: int, capsys: pytest.CaptureFixture[str], tmp: Path, mp: pytest.MonkeyPatch
) -> str:
    mp.chdir(tmp)
    code = main(
        [
            "run",
            str(folder / "model.yaml"),
            "--plan",
            str(folder / "plan.csv"),
            "--reps",
            str(reps),
            "--orders",
        ]
    )
    assert code == 0
    return capsys.readouterr().out


def test_orders_table_without_a_calendar_uses_hours(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    printed = _run(SHOP, 3, capsys, tmp_path, monkeypatch)
    lines = printed.splitlines()

    start = lines.index("Orders over 3 runs (hours from the start):")
    header = lines[start + 1].split()
    assert header[:4] == ["Order", "Part", "Qty", "Due"]
    assert lines[start + 2].split()[:3] == ["wo-1", "shaft", "15"]
    assert any(line.split()[0] == "wo-10" for line in lines[start + 2 : start + 12])
    assert any(line.startswith("Busiest work centers:") and "mill_shaft" in line for line in lines)
    # The single-run summary is still there.
    assert any(line.strip().startswith("orders:") for line in lines)


def test_orders_table_with_a_calendar_prints_dates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    printed = _run(CALENDAR, 1, capsys, tmp_path, monkeypatch)
    lines = printed.splitlines()

    start = lines.index("Orders over 1 run:")
    row = lines[start + 2]
    # Order A is due 86400 s after Mon Mar 02 07:00, which is Tue Mar 03 07:00.
    assert row.split()[:7] == ["A", "finished", "1", "Tue", "Mar", "03", "07:00"]
    assert row.rstrip().endswith("0 of 1")
