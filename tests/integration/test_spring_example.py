"""T-050 SPRING worked example — integration test.

`examples/spring/model.yaml` + `examples/spring/plan.csv` is a client floor
expressed purely as config, no Python: wire -> cut -> form -> stress-relief
(batch) -> grind -> coat -> coated spring. `cut` is the continuous-to-discrete
step: it consumes wire by the foot and emits an expected blank COUNT plus a
REMAINDER bundle of leftover wire, both ordinary `emits` entries against the
same `consumes[0]` basis (D-043/D-044: a remainder is never a special case).

This file exercises the example exactly as a client would run it:
`load_model` -> `validate_model` -> `twinflow.cli.main.main(["run", ...])`
(the same path `twinflow run` takes on the command line) -> a readable event
log and `kpis.json`. It also proves the cut recipe's own math directly
against the compiled `Transform` (cheap, real collaborators, no mocks) since
the remainder bundle is never consumed downstream and so never appears as a
job in the event log itself; the expected BLANK COUNT, by contrast, *is*
provable from the event log, because `form`'s consumed qty is exactly what
`cut` emitted (RunDriver's routing wiring, `plan/driver.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from twinflow.cli.main import main
from twinflow.model import load_model, validate_model
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.part import PartTypeRegistry

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "spring"
MODEL_PATH = EXAMPLE_DIR / "model.yaml"
PLAN_PATH = EXAMPLE_DIR / "plan.csv"

# Matches model.yaml's `cut` recipe: 1 ft of wire (the basis) -> 5 blank piece
# + 0.16667 ft of leftover wire, both ordinary `emits` ratios.
BLANKS_PER_FT = 5.0
REMAINDER_FT_PER_FT = 0.16667


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


def test_model_loads_and_routes_to_the_coated_finished_part() -> None:
    compiled = load_model(str(MODEL_PATH))

    assert compiled.routing["coated"] == ["cut", "form", "stress_relief", "grind", "coat"]
    # BOM rolls the whole chain up to raw `wire` only -- every intermediate
    # part (blank, formed, relieved, ground) resolves away.
    assert set(compiled.bom["coated"]) == {"wire"}


def test_cut_emits_the_expected_blank_count_plus_a_leftover_wire_remainder() -> None:
    """Proves the recipe's own math directly against the compiled Transform:
    a continuous 7 ft of wire in yields the expected blank COUNT (35 piece)
    plus a REMAINDER bundle of leftover wire (~1.167 ft) -- an ordinary
    second output, never a special case (D-043/D-044)."""
    compiled = load_model(str(MODEL_PATH))
    cut = next(loc for loc in compiled.locations if loc.location_id == "cut")

    inputs = [Bundle(qty=7.0, thing="wire", uom="ft")]
    outputs = cut.transform.apply(inputs, compiled.registry)

    by_thing = {bundle.thing: bundle for bundle in outputs}
    assert set(by_thing) == {"blank", "wire_remainder"}
    assert by_thing["blank"].qty == pytest.approx(7.0 * BLANKS_PER_FT)
    assert by_thing["blank"].uom == "piece"
    assert by_thing["wire_remainder"].qty == pytest.approx(7.0 * REMAINDER_FT_PER_FT)
    assert by_thing["wire_remainder"].uom == "ft"


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
    assert set(df["location_id"].unique()) == {"cut", "form", "stress_relief", "grind", "coat"}

    kpis = json.loads(kpis_path.read_text(encoding="utf-8"))
    assert "run_hours" in kpis
    assert "setup_hours" in kpis

    # The expected blank COUNT, proven from the event log: `form`'s consumed
    # qty (its job_bundle is `blank`, per plan/driver.py's routing wiring) is
    # exactly `BLANKS_PER_FT` times what `cut` consumed of `wire`, for both
    # work orders in plan.csv.
    cut_rows = df.filter(pl.col("location_id") == "cut").sort("queue_arrival_time")
    form_rows = df.filter(pl.col("location_id") == "form").sort("queue_arrival_time")
    assert cut_rows.height == 2
    assert form_rows.height == 2
    for wire_ft, blank_qty in zip(cut_rows["qty"], form_rows["qty"], strict=True):
        assert blank_qty == pytest.approx(wire_ft * BLANKS_PER_FT)

    # Every finished spring the plan demanded actually reached `coat`.
    coat_rows = df.filter(pl.col("location_id") == "coat")
    assert coat_rows["qty"].sum() == pytest.approx(20.0 + 30.0)


def test_registry_declares_every_part_in_the_chain() -> None:
    compiled = load_model(str(MODEL_PATH))
    registry: PartTypeRegistry = compiled.registry

    for part in ("wire", "wire_remainder", "blank", "formed", "relieved", "ground", "coated"):
        registry.uom(part)  # raises KeyError if undeclared
