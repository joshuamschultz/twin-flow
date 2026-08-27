"""CNC SHOP worked example -- integration test.

`examples/cnc-shop/model.yaml` + `examples/cnc-shop/plan.csv` is a machine shop
floor expressed purely as config, no Python: two part families (`shaft`,
`bracket`) each run raw stock -> saw -> CNC mill -> deburr -> inspect (scrap
rate) -> finished, through their own dedicated single-work-center locations
per step (`saw_shaft`/`saw_bracket`, `mill_shaft`/`mill_bracket`, ...), each
tagged with a distinct `setup_key` (D-044 plain-language config sugar) so the
two families never share a setup/fixture group.

The CNC mill (`mill_shaft`/`mill_bracket`) is deliberately tuned far slower
(rate 0.03 piece/s) than every other step (0.5-2.0 piece/s), and `plan.csv`
releases its 10 orders on a tight cadence with tight due dates -- so the
mill's queue backs up, both mills report the floor's highest utilization,
and several orders finish late. See `examples/cnc-shop/README.md` "What
this shows".

Every location declares no `batch_size`, so `PullRule` applies its default:
one work center pulls the NEXT job only (one bundle, arrival order), never
the whole queue -- a real queue forms and is visible in the wait-time
breakdown (`starved` = center idle waiting for work), not in firing counts:
every location fires exactly once per order regardless of how deep its
queue gets (see `test_run_writes_a_readable_event_log_and_kpis_json`).

This file exercises the example exactly as a client would run it:
`load_model` -> `validate_model` -> `twinflow.cli.main.main(["run", ...])`
(the same path `twinflow run` takes on the command line) -> a readable event
log and `kpis.json`, mirroring `tests/integration/test_spring_example.py`'s
established pattern for a config-only worked example -- plus a direct
`RunDriver` + `compute_kpis` check (mirroring `plan/driver.py` and
`instrumentation/sweep.py`) that asserts the bottleneck itself: the mill is
the max-utilization work center, several orders finish late, and the
centers the mill FEEDS starve for work while the mill itself stays busy.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from twinflow.cli.main import main
from twinflow.instrumentation import compute_kpis
from twinflow.instrumentation.sweep import orders_frame
from twinflow.model import load_model, validate_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.part import PartTypeRegistry

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "cnc-shop"
MODEL_PATH = EXAMPLE_DIR / "model.yaml"
PLAN_PATH = EXAMPLE_DIR / "plan.csv"

# Matches model.yaml's `saw_shaft` recipe: 1 ft of round bar (the basis) -> 4
# shaft blanks, an ordinary `emits` ratio (no scrap block on this step).
BLANKS_PER_FT = 4.0

# plan.csv work orders, by finished part.
SHAFT_QTYS = (15, 25, 30, 12, 20)
BRACKET_QTYS = (20, 10, 18, 22, 16)

ALL_LOCATIONS = {
    "saw_shaft",
    "mill_shaft",
    "deburr_shaft",
    "inspect_shaft",
    "saw_bracket",
    "mill_bracket",
    "deburr_bracket",
    "inspect_bracket",
}

MILL_LOCATIONS = {"mill_shaft", "mill_bracket"}


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run inside tmp_path so `runs/<run-id>/` never touches the repo tree."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_dir_names(cwd: Path) -> set[str]:
    runs_dir = cwd / "runs"
    if not runs_dir.exists():
        return set()
    return {entry.name for entry in runs_dir.iterdir() if entry.is_dir()}


def test_model_validates_with_no_errors() -> None:
    errors = validate_model(str(MODEL_PATH))

    assert errors == []


def test_model_loads_and_routes_both_families_to_their_finished_part() -> None:
    compiled = load_model(str(MODEL_PATH))

    assert compiled.routing["shaft"] == [
        "saw_shaft",
        "mill_shaft",
        "deburr_shaft",
        "inspect_shaft",
    ]
    assert compiled.routing["bracket"] == [
        "saw_bracket",
        "mill_bracket",
        "deburr_bracket",
        "inspect_bracket",
    ]
    # BOM rolls each chain up to its own raw material only -- every
    # intermediate part resolves away.
    assert set(compiled.bom["shaft"]) == {"round_bar"}
    assert set(compiled.bom["bracket"]) == {"square_billet"}


def test_the_two_families_carry_distinct_setup_keys_at_every_step() -> None:
    """Each family's location is its own single work center with its own
    `setup_key` -- the plain-config declaration of a changeover/fixture
    group, never shared between `shaft` and `bracket` steps."""
    compiled = load_model(str(MODEL_PATH))
    setup_key_by_location = {
        loc.location_id: next(iter(loc.setup_policy.setup_key_of.values()))
        for loc in compiled.locations
    }

    for step in ("saw", "mill", "deburr", "inspect"):
        shaft_key = setup_key_by_location[f"{step}_shaft"]
        bracket_key = setup_key_by_location[f"{step}_bracket"]
        assert shaft_key != bracket_key


def test_saw_shaft_emits_the_expected_blank_count_from_a_continuous_bar() -> None:
    """Proves the recipe's own math directly against the compiled Transform:
    a continuous 8 ft of round bar in yields the expected blank COUNT
    (32 piece), an ordinary `emits` ratio (D-043/D-044)."""
    compiled = load_model(str(MODEL_PATH))
    saw_shaft = next(loc for loc in compiled.locations if loc.location_id == "saw_shaft")

    inputs = [Bundle(qty=8.0, thing="round_bar", uom="ft")]
    outputs = saw_shaft.transform.apply(inputs, compiled.registry)

    by_thing = {bundle.thing: bundle for bundle in outputs}
    assert set(by_thing) == {"shaft_blank"}
    assert by_thing["shaft_blank"].qty == pytest.approx(8.0 * BLANKS_PER_FT)
    assert by_thing["shaft_blank"].uom == "piece"


def test_inspect_bracket_splits_good_and_scrap_by_the_declared_rate() -> None:
    """Proves the scrap synthesis directly against the compiled Transform:
    100 deburred brackets in yields 95 good + 5 scrap (rate 0.05)."""
    compiled = load_model(str(MODEL_PATH))
    inspect_bracket = next(
        loc for loc in compiled.locations if loc.location_id == "inspect_bracket"
    )

    inputs = [Bundle(qty=100.0, thing="bracket_deburred", uom="piece")]
    outputs = inspect_bracket.transform.apply(inputs, compiled.registry)

    by_thing = {bundle.thing: bundle for bundle in outputs}
    assert set(by_thing) == {"bracket", "bracket_scrap"}
    assert by_thing["bracket"].qty == pytest.approx(95.0)
    assert by_thing["bracket_scrap"].qty == pytest.approx(5.0)


def test_run_writes_a_readable_event_log_and_kpis_json(isolated_cwd: Path) -> None:
    before = _run_dir_names(isolated_cwd)

    exit_code = main(["run", str(MODEL_PATH), "--plan", str(PLAN_PATH), "--reps", "1"])

    assert exit_code == 0
    after = _run_dir_names(isolated_cwd)
    added = after - before
    assert len(added) == 1, f"expected exactly one new run directory, found {added}"
    run_dir = isolated_cwd / "runs" / next(iter(added))

    events_path = run_dir / "events.parquet"
    kpis_path = run_dir / "kpis.json"
    assert events_path.is_file()
    assert kpis_path.is_file()

    df = pl.read_parquet(events_path)
    assert df.height > 0
    assert set(df["location_id"].unique()) == ALL_LOCATIONS

    kpis = json.loads(kpis_path.read_text(encoding="utf-8"))
    assert "run_hours" in kpis
    assert "setup_hours" in kpis

    # Every work order in plan.csv releases exactly once at `saw` (one
    # release bundle per order, per plan/driver.py) -- `saw` never queues,
    # so its firing count always matches the order count exactly.
    saw_shaft_rows = df.filter(pl.col("location_id") == "saw_shaft")
    saw_bracket_rows = df.filter(pl.col("location_id") == "saw_bracket")
    assert saw_shaft_rows.height == len(SHAFT_QTYS)
    assert saw_bracket_rows.height == len(BRACKET_QTYS)

    # No location declares `batch_size`, so `PullRule` applies its default:
    # a work center pulls the NEXT job only (one bundle, arrival order),
    # never the whole queue. That holds regardless of how deep a queue
    # gets, so EVERY location -- including the mill sitting behind a real
    # backlog -- fires exactly once per order, same as `saw`. The backlog
    # shows up as wait time (see the starved-vs-mill assertion below), not
    # as fewer/bigger firings.
    for step in ("mill", "deburr", "inspect"):
        shaft_rows = df.filter(pl.col("location_id") == f"{step}_shaft")
        bracket_rows = df.filter(pl.col("location_id") == f"{step}_bracket")
        assert shaft_rows.height == len(SHAFT_QTYS)
        assert bracket_rows.height == len(BRACKET_QTYS)

    # The event log's `qty` is what a firing CONSUMED (D-017): the BOM
    # rollup (model/compile.py) inflates every raw-material release just
    # enough to cover the declared scrap loss, so `inspect`'s CONSUMED qty
    # times (1 - scrap rate) reproduces the ordered qty exactly -- the same
    # scrap-rate split proven directly against the compiled Transform above.
    inspect_shaft_rows = df.filter(pl.col("location_id") == "inspect_shaft")
    inspect_bracket_rows = df.filter(pl.col("location_id") == "inspect_bracket")
    assert inspect_shaft_rows["qty"].sum() * (1 - 0.03) == pytest.approx(float(sum(SHAFT_QTYS)))
    assert inspect_bracket_rows["qty"].sum() * (1 - 0.05) == pytest.approx(float(sum(BRACKET_QTYS)))


def test_the_mill_is_the_bottleneck_and_orders_finish_late() -> None:
    """The scenario this example exists to show: run the plan through
    `RunDriver` and `compute_kpis` directly (the same in-process path
    `twinflow.cli.main._handle_run` and `instrumentation/sweep.py` use) and
    assert the three facts a planner would read straight off the report --

    1. a CNC mill (`mill_shaft` or `mill_bracket`) has the single highest
       `utilization_by_cell` on the floor, and it is pinned near-max
       (>= 0.85) -- the floor's capacity constraint is visibly the mill,
       not any other station.
    2. `on_time_pct` is well below 100 and several orders have strictly
       positive `lateness_by_order` -- the mill's queue is long enough that
       real orders slip their due dates, not just a theoretical risk.
    3. the wait-time breakdown reads as a work-CENTER view (D-034): the
       mill itself shows low `starved` (it's busy), while the very next
       station in its own family's chain -- fed exclusively by the mill --
       shows high `starved` (idle, waiting on the mill's backlog). That is
       the report's bottleneck fingerprint: a busy constraint starving the
       stations downstream of it.
    """
    compiled = load_model(str(MODEL_PATH))
    work_orders = load_plan(str(PLAN_PATH), compiled.registry)

    result = RunDriver(compiled).run(work_orders, seed=0, replication_index=0)
    orders = orders_frame(work_orders, compiled, result.event_log_path)
    kpis = compute_kpis(result.event_log_path, orders, result.horizon)

    busiest_location = max(kpis.utilization_by_cell, key=lambda loc: kpis.utilization_by_cell[loc])
    assert busiest_location in MILL_LOCATIONS
    assert kpis.utilization_by_cell[busiest_location] >= 0.85

    non_mill_utilization = [
        utilization
        for location_id, utilization in kpis.utilization_by_cell.items()
        if location_id not in MILL_LOCATIONS
    ]
    assert non_mill_utilization
    assert kpis.utilization_by_cell[busiest_location] > max(non_mill_utilization)

    assert kpis.on_time_pct < 100.0
    late_orders = [
        work_order_id
        for work_order_id, lateness in kpis.lateness_by_order.items()
        if lateness is not None and lateness > 0
    ]
    assert len(late_orders) >= 3, f"expected several late orders, found {late_orders}"

    # Downstream-starved fingerprint: the mill's own family's next station
    # (fed only by the mill) starves for work far more than the mill itself
    # sits idle.
    family = busiest_location.split("_", 1)[1]
    downstream_location = f"deburr_{family}"
    mill_starved = kpis.wait_seconds_by_location[busiest_location]["starved"]
    downstream_starved = kpis.wait_seconds_by_location[downstream_location]["starved"]
    assert downstream_starved > mill_starved


def test_registry_declares_every_part_in_both_chains() -> None:
    compiled = load_model(str(MODEL_PATH))
    registry: PartTypeRegistry = compiled.registry

    for part in (
        "round_bar",
        "shaft_blank",
        "shaft_milled",
        "shaft_deburred",
        "shaft",
        "shaft_scrap",
        "square_billet",
        "bracket_blank",
        "bracket_milled",
        "bracket_deburred",
        "bracket",
        "bracket_scrap",
    ):
        registry.uom(part)  # raises KeyError if undeclared
