"""COMP-023 KpiEngine — unit tests (T-036, red phase).

Contract under test (`src/twinflow/instrumentation/kpis.py::KpiEngine`,
Layer 4, D-017 / D-034 / D-042):

KpiEngine computes EVERY reported KPI from one or more `events.parquet`
paths using LAZY Polars (`scan_parquet` with filters pushed to the front).
No KPI is computed anywhere outside this component (structure.md,
instrumentation module contract).

Committed API (this test file fixes it; the implementer conforms):

    KpiEngine().compute(
        event_paths: str | Path | Sequence[str | Path],
        orders: pl.DataFrame,          # columns: order_id, part_id, due_date, lot_id
        horizon: float,                # per-run horizon, sim seconds
        labor_pool_capacity: dict[str, int] | int = 1,
    ) -> KpiSet

Orders-to-events linkage (this test file's design choice — documented per
the task brief, since the schema has no order concept at all): `orders` is
a Polars DataFrame with one row per (order, lot) pair —
`["order_id", "part_id", "due_date", "lot_id"]`. `lot_id` is the join key
into the event table's `lot_id` column. An order's completion date is the
MAX `release_time` across every event row whose `lot_id` belongs to that
order (an order may span several lots; in this fixture every order maps to
exactly one lot except where a test explicitly exercises the multi-lot or
no-matching-lot case). `due_date` is a plain Float64 in sim seconds — same
unit as every other timestamp in the event table (tech.md: "Simulation time
is float seconds ... never datetime"). "signed days" in tech.md's
objective-agnostic requirement is a report-layer presentation unit; the
KpiEngine layer stays in seconds like every other KPI here.

KpiSet field names this test file pins (dict-shaped, boring names):

    completion_by_order        dict[str, float | None]   max release_time per order;
                                                           None if no event row matches
    lateness_by_order          dict[str, float | None]   completion - due_date, signed;
                                                           None mirrors completion_by_order
    on_time_pct                float                     0-100; excludes orders with no
                                                           completion (unknown != late)
    utilization_by_cell        dict[str, float]           busy_seconds / horizon, by location_id
    utilization_by_machine     dict[str, float]           SAME values as utilization_by_cell
                                                           in v1 (see V1 SIMPLIFICATION 2 below)
    wait_seconds_by_location   dict[str, dict[str, float]] {"starved", "blocked",
                                                             "material_starved"} sums per
                                                             location_id, via WaitClassifier
    labor_pool_utilization     dict[str, float]           busy_seconds / (horizon * capacity),
                                                            by pool name (v1: one "default" pool)
    wip_by_location             pl.DataFrame               columns ["location_id", "t", "wip"];
                                                            the sweep-line series
    machine_hours_by_machine   dict[str, float]           sum(actual_end - actual_start) / 3600,
                                                            by location_id
    labor_hours_by_pool_skill  dict[tuple[str, str], float]  hours, keyed (pool, skill)
    run_hours                  float                       sum(actual_end - actual_start) / 3600,
                                                            all rows
    setup_hours                float                       ALWAYS 0.0 in v1 (see V1
                                                            SIMPLIFICATION 1)
    event_counts_by_location   dict[str, int]              row COUNT per location_id via
                                                            pl.len(), never col(...).count()

V1 SIMPLIFICATION 1 (setup_hours, per task brief — resolved, not a gap):
the current EVENT_LOG_SCHEMA has no separate setup-seconds column, and the
compiler sets setup cost to 0 (no changeover matrix declared in v1). So
`setup_hours` is a distinct KpiSet field that always computes to 0.0, while
`run_hours` is the full `sum(actual_end - actual_start)`. Both are reported
separately (satisfying "setup separate from run") without a schema change.
A future schema version splits them for real once changeover is modeled.

V1 SIMPLIFICATION 2 (machine == cell, DOCUMENTED, not invented): the
committed EVENT_LOG_SCHEMA (`event_log.py`) carries exactly one resource
identifier, `location_id` — there is no separate `machine_id` or `cell_id`
column, even though structure.md's primitives model describes a cell as a
work center with possibly several machines inside it. Since the event log
cannot currently distinguish "the cell" from "the machine inside it",
`utilization_by_cell` and `utilization_by_machine` are computed from the
IDENTICAL `location_id` grouping in v1 and therefore report identical
values. This test asserts that identity directly so it is highly visible
(and clearly broken) the day a real `machine_id` column is added.

FLAGGED GAP (reported to the orchestrator, not silently invented): the
committed EVENT_LOG_SCHEMA has NO operator, labor-pool, or skill column
at all — unlike the setup-hours case, there is no existing column to
reuse. tech.md's Assumptions block documents v1's engine default as "one
undifferentiated labor pool", which resolves the DIMENSION problem (v1 has
exactly one pool, "default", and one skill, "default" — "undifferentiated"
reads as no skill distinction either) but not the CAPACITY problem: with no
operator column, every row's [actual_start, actual_end] interval is
attributed to the single pool's busy time per D-045 (operator acquired
alongside the machine, released after run/setup, so operator-hold and
machine-hold intervals coincide in v1). Utilization additionally needs a
headcount denominator that exists nowhere in the event table or in the
horizon/orders context the task brief names. This test file resolves that
by adding `labor_pool_capacity` as an explicit `compute()` parameter (a
per-run fact, not an event-table column) rather than inventing a schema
column. If COMP-023's real implementer disagrees this is the right
resolution, that is the specific thing to raise back to the spec, not to
silently reinterpret in code.

WIP-over-time sweep line (structure.md "Derived, not stored"): entries are
`queue_arrival_time` (+1), exits are `release_time` (-1), concatenated,
sorted by `(location_id, t, delta)` — `-1` sorts before `+1` on exact ties
(entries/exits at the identical timestamp at the SAME location) — then
`cum_sum().over("location_id")`. One test in this file constructs a
same-location, same-instant tie deliberately to pin that ordering rule.

Row counts (acceptance criterion 3): every count in KpiEngine MUST use
`pl.len()` (counts rows, nulls included), never `col(x).count()` (silently
skips nulls). The main fixture below includes one row with a NULL `qty` at
`cell_a`; `event_counts_by_location["cell_a"]` must still be 3, not 2.

RED-phase note: `KpiEngine.__init__` currently raises `NotImplementedError`
(T-037), so every test below fails at construction for that reason — never
an `ImportError` or a syntax error, because `kpis.py` and `WaitClassifier`
already import cleanly (`WaitClassifier` is DONE and used below, unmodified,
only to compute this file's expected wait-classification values from the
fixture rows — it is not the thing under test in this file).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from twinflow.instrumentation import EVENT_LOG_SCHEMA
from twinflow.instrumentation.kpis import KpiEngine

# ---------------------------------------------------------------------------
# Fixture rows — one hand-computable little floor: two locations (cell_a,
# cell_b), three orders (order_1/lot_1, order_2/lot_2, order_3/lot_3).
# Column order matches EVENT_LOG_SCHEMA exactly:
#   location_id, part_id, lot_id, process_name, qty,
#   queue_arrival_time, material_ready_time, actual_start, actual_end,
#   release_time, outcome, setup_seconds
# The main fixture is setup-free (setup_seconds = 0.0 on every row) so setup
# never confounds the run/utilization KPIs these rows pin; a dedicated test
# below builds its own rows with nonzero setup to exercise setup_hours.
# ---------------------------------------------------------------------------

R1 = ("cell_a", "widget", "lot_1", "transform", 5.0, 0.0, 0.0, 0.0, 10.0, 10.0, "good", 0.0)
R2 = ("cell_b", "widget", "lot_1", "transform", 5.0, 10.0, 14.0, 14.0, 24.0, 26.0, "good", 0.0)
# R3 carries a NULL qty on purpose — pins the pl.len()-not-count() rule.
R3 = ("cell_a", "widget", "lot_2", "transform", None, 5.0, 5.0, 15.0, 23.0, 25.0, "good", 0.0)
# R4 carries a NULL material_ready_time (no material gate tracked -> starved).
R4 = ("cell_b", "widget", "lot_2", "transform", 8.0, 25.0, None, 30.0, 40.0, 42.0, "good", 0.0)
# R5's queue_arrival_time (10.0) exactly matches R1's release_time (10.0) at
# the SAME location (cell_a) -> the WIP sweep-line tie this file pins.
R5 = ("cell_a", "widget", "lot_3", "transform", 6.0, 10.0, 10.0, 23.0, 30.0, 30.0, "good", 0.0)

MAIN_ROWS = [R1, R2, R3, R4, R5]

HORIZON = 100.0
LABOR_POOL_CAPACITY = 2

ORDERS_ROWS = [
    ("order_1", "widget", 50.0, "lot_1"),
    ("order_2", "widget", 30.0, "lot_2"),
    ("order_3", "widget", 40.0, "lot_3"),
]
ORDERS_SCHEMA = {"order_id": pl.Utf8, "part_id": pl.Utf8, "due_date": pl.Float64, "lot_id": pl.Utf8}


def _events_df(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=EVENT_LOG_SCHEMA, orient="row")


def _write_events(rows: list[tuple], path: Path) -> Path:
    _events_df(rows).write_parquet(path, compression="zstd")
    return path


def _orders_df(rows: list[tuple] = ORDERS_ROWS) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=ORDERS_SCHEMA, orient="row")


def _expected_wait_totals(rows: list[tuple], horizon: float) -> dict[str, dict[str, float]]:
    """Expected per-CENTER wait split under the center-idle model the KpiEngine now
    uses: busy = sum(actual_end - actual_start), blocked = sum(release_time -
    actual_end), starved = horizon - busy - blocked (idle remainder), material_starved
    = 0 in v1. A center view, not a per-job queue view - a busy bottleneck shows ~0
    starved; the centers it feeds show high starved."""
    busy: dict[str, float] = {}
    blocked: dict[str, float] = {}
    for row in rows:
        location_id = row[0]
        _qa, _material_ready, actual_start, actual_end, release_time = row[5:10]
        busy[location_id] = busy.get(location_id, 0.0) + (actual_end - actual_start)
        blocked[location_id] = blocked.get(location_id, 0.0) + (release_time - actual_end)
    return {
        location_id: {
            "starved": max(0.0, horizon - busy[location_id] - blocked[location_id]),
            "blocked": blocked[location_id],
            "material_starved": 0.0,
        }
        for location_id in busy
    }


@pytest.fixture
def main_fixture(tmp_path: Path):
    """The full 5-row / 2-location / 3-order fixture, written to one parquet file."""
    event_path = _write_events(MAIN_ROWS, tmp_path / "events.parquet")
    return {
        "event_path": event_path,
        "orders": _orders_df(),
        "horizon": HORIZON,
        "labor_pool_capacity": LABOR_POOL_CAPACITY,
    }


# ---------------------------------------------------------------------------
# Headline output 1 — completion dates + on-time %
# ---------------------------------------------------------------------------


def test_completion_by_order_is_max_release_time_across_the_orders_lot(main_fixture) -> None:
    """order_1/lot_1 -> max(release_time)=max(10,26)=26. order_2/lot_2 ->
    max(25,42)=42. order_3/lot_3 -> max(30)=30."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.completion_by_order["order_1"] == pytest.approx(26.0)
    assert result.completion_by_order["order_2"] == pytest.approx(42.0)
    assert result.completion_by_order["order_3"] == pytest.approx(30.0)


def test_lateness_by_order_is_signed_completion_minus_due_date(main_fixture) -> None:
    """order_1: 26-50=-24 (early). order_2: 42-30=+12 (late). order_3: 30-40=-10
    (early). Signed, in sim seconds like every other KpiEngine timestamp — never
    collapsed to a boolean (D-042: a boolean can't be reweighted per customer)."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.lateness_by_order["order_1"] == pytest.approx(-24.0)
    assert result.lateness_by_order["order_2"] == pytest.approx(12.0)
    assert result.lateness_by_order["order_3"] == pytest.approx(-10.0)


def test_on_time_pct_counts_lateness_at_or_below_zero_as_on_time(main_fixture) -> None:
    """2 of 3 orders on time (order_1, order_3); order_2 is late. 2/3 * 100."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.on_time_pct == pytest.approx(200.0 / 3.0, abs=0.01)


def test_order_with_no_matching_lot_reports_none_completion_and_is_excluded(
    main_fixture,
) -> None:
    """An order referencing a lot_id that never appears in the event table
    (a genuinely unfinished/undetected order) must not be silently coerced
    into "on time" or "late" -- completion and lateness are both None, and
    on_time_pct's percentage is unchanged because the unknown order is
    excluded from the denominator, not counted as either outcome."""
    orders_with_gap = pl.concat(
        [
            main_fixture["orders"],
            pl.DataFrame(
                [("order_4", "widget", 999.0, "lot_missing")], schema=ORDERS_SCHEMA, orient="row"
            ),
        ]
    )

    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=orders_with_gap,
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.completion_by_order["order_4"] is None
    assert result.lateness_by_order["order_4"] is None
    assert result.on_time_pct == pytest.approx(200.0 / 3.0, abs=0.01)


# ---------------------------------------------------------------------------
# Headline output 2 — per-cell and per-machine utilization
# ---------------------------------------------------------------------------


def test_utilization_by_cell_is_busy_seconds_over_horizon(main_fixture) -> None:
    """cell_a busy = (10-0)+(23-15)+(30-23) = 25s -> 25/100 = 0.25.
    cell_b busy = (24-14)+(40-30) = 20s -> 20/100 = 0.20."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.utilization_by_cell["cell_a"] == pytest.approx(0.25)
    assert result.utilization_by_cell["cell_b"] == pytest.approx(0.20)


def test_busy_hours_by_location_is_location_keyed(main_fixture) -> None:
    """cell_a busy = 25s -> 25/3600 h; cell_b busy = 20s -> 20/3600 h. Always
    keyed by LOCATION id (the per-stage cycle-time view depends on this, even
    when resource attribution keys machine hours by physical machine instead)."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert set(result.busy_hours_by_location) == {"cell_a", "cell_b"}
    assert result.busy_hours_by_location["cell_a"] == pytest.approx(25 / 3600.0)
    assert result.busy_hours_by_location["cell_b"] == pytest.approx(20 / 3600.0)


def test_utilization_by_machine_equals_utilization_by_cell_in_v1(main_fixture) -> None:
    """V1 SIMPLIFICATION 2: no separate machine_id column exists, so per-machine
    and per-cell utilization alias the SAME location_id grouping. This
    equality must break loudly the day a real machine_id column lands."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.utilization_by_machine == result.utilization_by_cell


# ---------------------------------------------------------------------------
# Headline output 3 — starved vs blocked vs material-starved per cell
# ---------------------------------------------------------------------------


def test_wait_seconds_by_location_center_idle_decomposition(main_fixture) -> None:
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    expected = _expected_wait_totals(MAIN_ROWS, HORIZON)
    assert result.wait_seconds_by_location["cell_a"]["starved"] == pytest.approx(
        expected["cell_a"]["starved"]
    )
    assert result.wait_seconds_by_location["cell_a"]["blocked"] == pytest.approx(
        expected["cell_a"]["blocked"]
    )
    assert result.wait_seconds_by_location["cell_a"]["material_starved"] == pytest.approx(
        expected["cell_a"]["material_starved"]
    )
    assert result.wait_seconds_by_location["cell_b"]["starved"] == pytest.approx(
        expected["cell_b"]["starved"]
    )
    assert result.wait_seconds_by_location["cell_b"]["blocked"] == pytest.approx(
        expected["cell_b"]["blocked"]
    )
    assert result.wait_seconds_by_location["cell_b"]["material_starved"] == pytest.approx(
        expected["cell_b"]["material_starved"]
    )


def test_wait_seconds_by_location_categories_are_all_represented_in_fixture(
    main_fixture,
) -> None:
    """Sanity check on the fixture design itself: every one of the three
    WaitClassifier categories is nonzero somewhere, so a KpiEngine that
    silently zeroes out one category would still be caught here even if the
    exact-value assertions above had a compensating error."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    total_starved = sum(v["starved"] for v in result.wait_seconds_by_location.values())
    total_blocked = sum(v["blocked"] for v in result.wait_seconds_by_location.values())
    total_material_starved = sum(
        v["material_starved"] for v in result.wait_seconds_by_location.values()
    )
    assert total_starved > 0
    assert total_blocked > 0
    assert total_material_starved == pytest.approx(0.0)  # v1: material gating not wired


# ---------------------------------------------------------------------------
# Headline output 4 — operator-pool utilization
# ---------------------------------------------------------------------------


def test_labor_pool_utilization_uses_capacity_from_compute_argument(main_fixture) -> None:
    """Total busy seconds across ALL rows = 45 (v1: operator held for the same
    interval as the machine per D-045). With capacity=2 and horizon=100:
    45 / (100*2) = 0.225. Documented FLAGGED GAP: no operator/pool/skill
    column exists in EVENT_LOG_SCHEMA, so v1 attributes every row's run
    interval to the single "default" pool, and capacity is supplied as an
    explicit per-run compute() argument rather than an event-table column."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.labor_pool_utilization["default"] == pytest.approx(0.225)


def test_labor_pool_utilization_default_capacity_is_one_when_unspecified(main_fixture) -> None:
    """Omitting labor_pool_capacity must not silently ignore capacity (dividing
    by 1 is a real, auditable default, not the same as skipping the divide)."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
    )

    # busy_seconds(45) / (horizon(100) * capacity(1))
    assert result.labor_pool_utilization["default"] == pytest.approx(0.45)


# ---------------------------------------------------------------------------
# Headline output 5 — WIP over time (sweep line, sort + cumsum)
# ---------------------------------------------------------------------------


def test_wip_by_location_matches_hand_computed_sweep_line_cell_b(main_fixture) -> None:
    """cell_b entries at queue_arrival_time (+1): t=10 (R2), t=25 (R4).
    Exits at release_time (-1): t=26 (R2), t=42 (R4). No ties at cell_b:
    (10,+1)->1; (25,+1)->2; (26,-1)->1; (42,-1)->0."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    cell_b = (
        result.wip_by_location.filter(pl.col("location_id") == "cell_b")
        .sort("t")
        .select("t", "wip")
    )
    assert cell_b.rows() == [(10.0, 1), (25.0, 2), (26.0, 1), (42.0, 0)]


def test_wip_sweep_line_breaks_same_instant_tie_exit_before_entry_at_cell_a(
    main_fixture,
) -> None:
    """The tie this fixture was built to exercise: R1 exits cell_a (release_time
    =10.0, -1) at the EXACT same instant R5 enters cell_a (queue_arrival_time
    =10.0, +1). structure.md pins the sort as `(t, delta)` with -1 sorting
    before +1 on ties, so WIP must read 1 (not 3, and not staying at 2)
    immediately at t=10: (0,+1)->1; (5,+1)->2; (10,-1 then +1)->1 then 2;
    (25,-1)->1; (30,-1)->0."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    cell_a = (
        result.wip_by_location.filter(pl.col("location_id") == "cell_a")
        .sort(["t", "wip"])
        .select("t", "wip")
    )
    assert cell_a.rows() == [
        (0.0, 1),
        (5.0, 2),
        (10.0, 1),
        (10.0, 2),
        (25.0, 1),
        (30.0, 0),
    ]


# ---------------------------------------------------------------------------
# Objective-agnostic minimum set: machine hours, labor hours, setup vs run
# ---------------------------------------------------------------------------


def test_machine_hours_by_machine_in_hours_not_seconds(main_fixture) -> None:
    """cell_a busy = 25s = 25/3600 h. cell_b busy = 20s = 20/3600 h."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.machine_hours_by_machine["cell_a"] == pytest.approx(25.0 / 3600.0)
    assert result.machine_hours_by_machine["cell_b"] == pytest.approx(20.0 / 3600.0)


def test_run_hours_equals_sum_of_per_machine_hours(main_fixture) -> None:
    """Cross-check: the global run_hours total must reconcile exactly with the
    per-machine breakdown -- no row silently double-counted or dropped."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.run_hours == pytest.approx(sum(result.machine_hours_by_machine.values()))
    assert result.run_hours == pytest.approx(45.0 / 3600.0)


def test_setup_hours_is_zero_for_a_setup_free_floor_and_a_separate_field(main_fixture) -> None:
    """A floor that declares no changeover charges 0 setup, so setup_hours is
    0.0 -- but it stays a DISTINCT field from run_hours ("setup separate from
    run" is satisfied by two fields, never by folding setup into run_hours)."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.setup_hours == 0.0
    assert result.run_hours != result.setup_hours
    assert result.run_hours > 0.0


def test_setup_hours_sums_the_setup_seconds_column_in_hours(tmp_path) -> None:
    """Tier 0: setup_hours is the real sum(setup_seconds)/3600 once changeover
    is compiled, reported separately from run_hours."""
    rows = [
        ("cell_a", "widget", "lot_1", "transform", 5.0, 0.0, 0.0, 0.0, 10.0, 10.0, "good", 45.0),
        ("cell_b", "widget", "lot_2", "transform", 5.0, 0.0, 0.0, 0.0, 10.0, 10.0, "good", 75.0),
    ]
    event_path = tmp_path / "events.parquet"
    _events_df(rows).write_parquet(event_path)
    orders = pl.DataFrame(
        [("order_1", "widget", 999.0, "lot_1")], schema=ORDERS_SCHEMA, orient="row"
    )

    result = KpiEngine().compute(event_paths=event_path, orders=orders, horizon=100.0)

    assert result.setup_hours == pytest.approx(120.0 / 3600.0)


def test_labor_hours_by_pool_and_skill_v1_single_undifferentiated_pool(main_fixture) -> None:
    """tech.md's documented v1 assumption: one undifferentiated labor pool.
    "Undifferentiated" reads as no skill split either, so the only key is
    ("default", "default"), with hours equal to the full run_hours total
    (operator held for the same interval as the machine per D-045)."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.labor_hours_by_pool_skill == {
        ("default", "default"): pytest.approx(45.0 / 3600.0)
    }


# ---------------------------------------------------------------------------
# Acceptance criterion 3 — pl.len() (counts rows) not col(...).count()
# (skips nulls). cell_a's R3 has a NULL qty and must still be counted.
# ---------------------------------------------------------------------------


def test_event_counts_by_location_include_rows_with_null_qty(main_fixture) -> None:
    """cell_a has 3 rows (R1, R3, R5); R3's qty is NULL. A `col("qty").count()`
    implementation would silently undercount cell_a as 2. cell_b has 2 rows
    (R2, R4), neither null, as a control that the correct rows aren't
    accidentally 3/2 by some other unrelated bug."""
    result = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert result.event_counts_by_location["cell_a"] == 3
    assert result.event_counts_by_location["cell_b"] == 2


# ---------------------------------------------------------------------------
# Adversarial addition beyond the listed acceptance criteria — multiple
# event_paths must combine as if they were one table (a run split across
# replication files, or the sweep-wide glob-scan case from structure.md).
# ---------------------------------------------------------------------------


def test_compute_accepts_a_list_of_event_paths_and_combines_them(tmp_path: Path) -> None:
    """Same 5-row fixture, but split across two independently written parquet
    files -- lot_1 has one row in each file, exercising a cross-file order
    completion join, not just a cross-file row count."""
    path_a = tmp_path / "run-a" / "events.parquet"
    path_a.parent.mkdir(parents=True, exist_ok=True)
    _write_events([R1, R3], path_a)
    path_b = tmp_path / "run-b" / "events.parquet"
    path_b.parent.mkdir(parents=True, exist_ok=True)
    _write_events([R2, R4, R5], path_b)

    result = KpiEngine().compute(
        event_paths=[path_a, path_b],
        orders=_orders_df(),
        horizon=HORIZON,
        labor_pool_capacity=LABOR_POOL_CAPACITY,
    )

    assert result.event_counts_by_location["cell_a"] == 3
    assert result.event_counts_by_location["cell_b"] == 2
    assert result.completion_by_order["order_1"] == pytest.approx(26.0)
    assert result.completion_by_order["order_2"] == pytest.approx(42.0)
    assert result.run_hours == pytest.approx(45.0 / 3600.0)


def test_compute_accepts_a_single_path_as_well_as_a_list(main_fixture) -> None:
    """The single-path convenience form (documented API) must not require
    callers to wrap one path in a list."""
    single = KpiEngine().compute(
        event_paths=main_fixture["event_path"],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )
    wrapped = KpiEngine().compute(
        event_paths=[main_fixture["event_path"]],
        orders=main_fixture["orders"],
        horizon=main_fixture["horizon"],
        labor_pool_capacity=main_fixture["labor_pool_capacity"],
    )

    assert single.run_hours == pytest.approx(wrapped.run_hours)
    assert single.event_counts_by_location == wrapped.event_counts_by_location
