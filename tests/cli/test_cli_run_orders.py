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


def test_a_due_time_of_midnight_prints_as_the_end_of_the_day_before() -> None:
    from datetime import datetime

    from twinflow.cli.order_table import _when

    origin = datetime.fromisoformat("2026-11-16T06:00:00-06:00")
    assert _when(18 * 3600, origin, due=True) == "Mon Nov 16 24:00"
    assert _when(18 * 3600, origin) == "Tue Nov 17 00:00"


_PAUSED_SHOP = """
defaults: {cycle_time_cv: 0.0}
stocks: []
part_types:
  - {name: raw, uom: piece, attributes: {}}
  - {name: done, uom: piece, attributes: {}}
machines: [{name: m}]
labor:
  pools:
    - name: p
      skills: [s]
      headcount: 1
      calendar:
        timezone: UTC
        origin: "2026-09-14T06:00:00+00:00"
        weekly:
          - {days: [mon, tue, wed, thu, fri, sat, sun], start: "06:00", end: "22:00"}
processes: []
bom: []
locations:
  - {name: cut, consumes: [{thing: raw, qty: 1, uom: piece}],
     emits: [{thing: done, qty: 1, uom: piece}], setup_key: g,
     time_model: {kind: rate_based, rate: 0.000277777777778}, machine: m, labor_skill: s,
     shift_crossing: pause}
routing:
  - {part: done, steps: [cut]}
"""

# Job A: 18 h of work from Mon 06:00, paused overnight, done Tue 08:00 (26 h).
# Job B: 2 h of work from Tue 12:00 (30 h), done Tue 14:00 (32 h, the horizon).
_PAUSED_PLAN = (
    "work_order_id,part,qty,start_date,due_date\nA,done,18,0,999999\nB,done,2,108000,999999\n"
)


def test_busiest_share_is_of_on_shift_time_not_the_paused_night(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "shop"
    folder.mkdir()
    (folder / "model.yaml").write_text(_PAUSED_SHOP, encoding="utf-8")
    (folder / "plan.csv").write_text(_PAUSED_PLAN, encoding="utf-8")
    printed = _run(folder, 1, capsys, tmp_path, monkeypatch)
    busiest = [line for line in printed.splitlines() if line.startswith("Busiest work centers:")]
    # 20 h of work in 24 h on shift is 83%. Counting the 8 h night A sat paused
    # would say 28 h busy of a 32 h clock, 87%.
    assert busiest == ["Busiest work centers: cut 83% (busy share of on-shift time)"]
