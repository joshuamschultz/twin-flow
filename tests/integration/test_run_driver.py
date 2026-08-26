"""COMP-019 RunDriver — integration tests (T-032, red phase).

RunDriver (`plan/driver.py`) is the M2 milestone proof: "a client floor runs end
to end from pure config." It is the in-process entry point that:

  1. Seeds declared INITIAL WIP directly onto the named Locations.
  2. Releases work orders on their declared START DATES (subject to any declared
     WIP ceiling — not exercised here; no plan field carries one yet).
  3. Runs ONE terminating replication with NO warm-up period: `env.run()` with
     no `until=` bound, which SimPy itself terminates once no scheduled event
     remains — the natural "ends when the plan completes" behavior.
  4. Is CHEAP to call repeatedly against an ALREADY-LOADED model: a second
     `driver.run()` call must not re-parse or re-compile `model.yaml`, and must
     never spawn a subprocess (D-041; COMP-020's later multiprocessing.Pool is
     one replication PER PROCESS, never inside a single replication).

RED-phase note: `twinflow.model.load_model` currently reads:

    def load_model(path: str) -> object:
        raise NotImplementedError("T-023/T-027")

a plain function whose signature already matches every call below (one
positional `path` argument), so every test in this file fails AT THAT LINE
with `NotImplementedError` — the right reason ("feature absent"), never an
ImportError or a signature-mismatch TypeError, because `load_model` imports
cleanly today and only calling it fails. `RunDriver.__init__(self) -> None`
(no other params) would additionally raise `TypeError` on `RunDriver(compiled)`
if a test ever reached that line, which is the same "signature mismatch is
also a valid RED reason" precedent `test_location.py`/`test_compile.py` set —
moot here since `load_model` raises first on every path.

--------------------------------------------------------------------------------
COMMITTED CompiledModel SURFACE (this test file fixes the part RunDriver reads;
implementer conforms). Nothing before this task defined what `load_model`
returns beyond "CompiledModel" as a type name, so this is the first place it is
pinned down. `model/compile.py`'s `CompileResult` (already-real, T-027) already
fixes `.locations: list[LocationSpec]`, `.routing: dict[str, list[str]]`,
`.bom: dict[str, dict[str, float]]` — CompiledModel is expected to carry those
straight through (however it wraps or subclasses `CompileResult`; not asserted
by name here — only duck-typed, matching the `LocationSpecLike` precedent in
`test_compile.py`). It ADDITIONALLY needs a way for RunDriver to build fresh
per-run `LaborPool`s (COMP-012 needs `name`, `headcount`, `skills`,
`shift_calendar` — none of which `LocationCompiler.compile()` builds; it only
ever sets `material_requirement=None` and never touches the model's top-level
`labor` section). Since a `LaborPool` is env-bound and RunContext/env is built
FRESH every `run()` call (never at `load_model()` time), CompiledModel cannot
carry pre-built `LaborPool` objects — only the raw declarative shape. This test
file does not assert on that attribute's exact name or shape (that plumbing is
internal to RunDriver/load_model); it only requires that SOME such information
survives from `model.yaml`'s `labor.pools` section to `load_model()`'s return
value, which the model.yaml below supplies via a `headcount` key alongside the
already-committed `name`/`skills` keys (`tests/unit/test_validate.py`'s
baseline model only declares `name`/`skills`; `headcount` is new here and does
not conflict with any existing validator check, which only ever reads
`pool.get("skills", [])`).

--------------------------------------------------------------------------------
ROUTING/WIRING ANALYSIS (why no primitive change is needed — see task's "STOP
and report the exact gap" instruction, which does NOT apply here):

`Location._fire`'s output step (`primitives/location.py`) is:

    destination = self.spec.destinations[output_bundle.thing]
    if isinstance(destination, Stock):
        destination.put(output_bundle)
    else:
        destination.append(output_bundle)

The `else` branch is duck-typed: it never checks `isinstance(destination, list)`,
only `isinstance(destination, Stock)`. Any object exposing `.append(bundle)`
satisfies the contract. `LocationSpec` (`model/schema.py`) is a plain
(non-frozen) `@dataclass`, so `destinations` is a mutable dict RunDriver can
rewrite after `LocationCompiler.compile()` has already produced it. This means
Location -> Location routing is fully expressible today with NO change to
`primitives/location.py`, `model/compile.py`, or `model/schema.py`: RunDriver
can replace `destinations[thing]` for every non-terminal output with a
`list[Bundle]` subclass whose `append()` also calls the downstream Location's
real `.enqueue(bundle)` (which stamps `queue_arrival_time` correctly), and
leave terminal (finished-part) outputs pointed at a plain list acting as the
finished-goods sink. This is exactly the same "list sink" shape
`LocationCompiler` already produces by default (`_compile_location`:
`destinations = {thing: [] for thing in _all_output_things(loc)}`) — RunDriver
only needs to overwrite specific entries post-compile, per the compiled
`routing` graph (`CompileResult.routing`: finished part -> ordered location
ids), by pairing each step with the next step's Location instance. No gap
found; this file does not stop for one.

--------------------------------------------------------------------------------
MODEL / PLAN CONVENTIONS this test file fixes (implementer conforms):

  * `WorkOrder.start_date` (already-real `str` field, `plan/loader.py`) is
    encoded here as a plain non-negative integer string of SIMULATION SECONDS
    offset from run start (t=0) — the simplest calendar-free reading consistent
    with tech.md ("Simulation time is float seconds ... there is no calendar or
    timezone in a sim clock"). `"0"` releases immediately; `"1000"` releases
    1000 simulated seconds after run start. This is a documented design choice,
    not a settled spec fact — flagged in this task's final report as an
    assumption for the orchestrator/implementer to confirm or override.
  * Initial WIP is seeded as `Location.enqueue(Bundle(qty=<initial_wip_qty>,
    thing=<the receiving location's own consumed thing>, uom=<matching uom>))`
    at t=0, before the run advances. `initial_wip_remaining_time` is set to
    `0.0` throughout this file specifically to sidestep an unresolved question
    (Location has no "resume mid-cycle" capability today) — the WIP bundle is
    expected to run its full normal cycle at the seeded location, same as any
    other queued job.
  * A demand-only WorkOrder row (no WIP) carries `initial_wip_*` fields as
    `None` (the dataclass default). A WIP-only row here carries `qty=0` so
    that, however RunDriver later decides to treat a WIP row's own demand
    fields, this file's conservation assertions are unaffected by a possible
    zero-qty release no-op.

Real collaborators throughout: real YAML on disk read through the real
`load_model`, real `LocationCompiler`/`ModelValidator` (once implemented), real
SimPy, real Polars round-trip through an actual Parquet file. Mocks are used
ONLY at the two boundaries this task requires spying on (module-level
`yaml.safe_load` and `LocationCompiler.compile`, to prove no re-parse/re-compile)
and the subprocess/multiprocessing entry points (to prove in-process execution).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import polars as pl
import pytest

import twinflow.model.compile as compile_module
import twinflow.model.loader as loader_module
from twinflow.model import load_model, validate_model
from twinflow.plan.driver import RunDriver, RunResult
from twinflow.plan.loader import WorkOrder

# ---------------------------------------------------------------------------
# Fixture vocabulary
# ---------------------------------------------------------------------------

WIP_QTY = 5.0
ORDER_A_QTY = 3
ORDER_B_QTY = 2
ORDER_B_START_SECONDS = 1000.0

FIVE_TIMESTAMP_COLUMNS = (
    "queue_arrival_time",
    "material_ready_time",
    "actual_start",
    "actual_end",
    "release_time",
)

MODEL_YAML = """
stocks: []

part_types:
  - name: raw_wire
    uom: ft
    attributes: {}
  - name: blank
    uom: piece
    attributes: {}
  - name: finished
    uom: piece
    attributes: {}

machines:
  - name: cut_m
  - name: pack_m

labor:
  pools:
    - name: floor_pool
      headcount: 2
      skills: [cut_op, pack_op]

locations:
  - name: cut
    consumes:
      - thing: raw_wire
        qty: 1
        uom: ft
    emits:
      - thing: blank
        qty: 1
        uom: piece
    setup_key: grp_cut
    time_model:
      kind: rate_based
      rate: 10
    machine: cut_m
    labor_skill: cut_op

  - name: pack
    consumes:
      - thing: blank
        qty: 1
        uom: piece
    emits:
      - thing: finished
        qty: 1
        uom: piece
    setup_key: grp_pack
    time_model:
      kind: rate_based
      rate: 10
    machine: pack_m
    labor_skill: pack_op

routing:
  - part: finished
    steps: [cut, pack]

processes: []
bom: []
"""


def _write_model(tmp_path: Path) -> Path:
    model_path = tmp_path / "model.yaml"
    model_path.write_text(MODEL_YAML, encoding="utf-8")
    return model_path


def _compiled_model(tmp_path: Path) -> object:
    """Real end-to-end: real file on disk, through the real (once-implemented)
    load_model wiring (loader -> compiler -> validator)."""
    model_path = _write_model(tmp_path)
    return load_model(str(model_path))


def _plan() -> list[WorkOrder]:
    """One WIP-only row seeded directly at `pack`, plus two released orders for
    the `finished` part entering at `cut` on different start dates."""
    return [
        WorkOrder(
            work_order_id="wip-1",
            part="finished",
            qty=0,
            start_date="0",
            due_date="0",
            initial_wip_location="pack",
            initial_wip_qty=int(WIP_QTY),
            initial_wip_remaining_time=0.0,
        ),
        WorkOrder(
            work_order_id="wo-A",
            part="finished",
            qty=ORDER_A_QTY,
            start_date="0",
            due_date="100000",
        ),
        WorkOrder(
            work_order_id="wo-B",
            part="finished",
            qty=ORDER_B_QTY,
            start_date=str(int(ORDER_B_START_SECONDS)),
            due_date="100000",
        ),
    ]


def _find_record(df: pl.DataFrame, location_id: str, qty: float, tol: float = 0.01) -> dict:
    """Locate the one ProcessExecution row at `location_id` with qty ~= `qty`,
    or fail loudly naming what was actually there."""
    matches = df.filter(
        (pl.col("location_id") == location_id) & ((pl.col("qty") - qty).abs() < tol)
    )
    assert matches.height == 1, (
        f"expected exactly one {location_id!r} record with qty~={qty}, found "
        f"{matches.height}: {df.filter(pl.col('location_id') == location_id)['qty'].to_list()}"
    )
    return matches.row(0, named=True)


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run inside tmp_path so RunDriver's auto-generated `runs/<run-id>/` output
    directory (no run_dir parameter exists in the designed `run()` signature)
    never touches the real repository tree."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Import sanity
# ---------------------------------------------------------------------------


def test_run_driver_and_load_model_import_cleanly() -> None:
    assert isinstance(RunDriver, type)
    assert isinstance(RunResult, type)
    assert callable(load_model)
    assert callable(validate_model)


# ---------------------------------------------------------------------------
# Acceptance 1 — starts from initial WIP, releases orders on start dates, ends
# when the plan completes (no warm-up, no hang).
# ---------------------------------------------------------------------------


def test_run_seeds_initial_wip_releases_orders_on_start_dates_and_terminates(
    isolated_cwd: Path,
) -> None:
    compiled = _compiled_model(isolated_cwd)
    driver = RunDriver(compiled)

    result = driver.run(_plan(), seed=1, replication_index=0)

    assert isinstance(result, RunResult)
    assert result.event_log_path is not None
    assert result.run_meta is not None

    df = pl.read_parquet(result.event_log_path)

    # Conservation: every unit that entered the floor (WIP at pack + the two
    # released orders' raw material at cut) is accounted for by a `pack`
    # firing of matching qty. No units invented, none lost.
    cut_a = _find_record(df, "cut", float(ORDER_A_QTY))
    cut_b = _find_record(df, "cut", float(ORDER_B_QTY))
    pack_wip = _find_record(df, "pack", WIP_QTY)
    pack_a = _find_record(df, "pack", float(ORDER_A_QTY))
    pack_b = _find_record(df, "pack", float(ORDER_B_QTY))

    # cut only ever sees raw_wire as its consumed job bundle.
    assert cut_a["part_id"] == "raw_wire"
    assert cut_b["part_id"] == "raw_wire"
    # pack only ever sees blank as its consumed job bundle (routed from cut,
    # or seeded directly as WIP — both arrive at pack as `blank`).
    assert pack_wip["part_id"] == "blank"
    assert pack_a["part_id"] == "blank"
    assert pack_b["part_id"] == "blank"

    # WIP is present from (at/near) t=0 — seeded before the run advances,
    # never waiting on an upstream `cut` firing.
    assert pack_wip["queue_arrival_time"] == pytest.approx(0.0, abs=1.0)

    # Order A released at start_date "0": its cut-stage arrival is early.
    assert cut_a["queue_arrival_time"] == pytest.approx(0.0, abs=1.0)

    # Order B released at start_date "1000": strictly later than A's release,
    # and consistent with the declared 1000-second offset (not merely
    # "sometime after", which would also pass a same-second bug).
    assert cut_b["queue_arrival_time"] > cut_a["queue_arrival_time"]
    assert cut_b["queue_arrival_time"] >= ORDER_B_START_SECONDS - 1.0

    # The run ends when the plan completes — no artificial warm-up, no
    # indefinite hang past the last released order's processing.
    assert isinstance(result.horizon, float)
    assert ORDER_B_START_SECONDS <= result.horizon <= ORDER_B_START_SECONDS + 500.0

    # Nothing else fired: exactly 2 cut records + 3 pack records.
    assert df.height == 5


# ---------------------------------------------------------------------------
# Acceptance 2 — the M2 proof: a readable event log at the returned path,
# carrying the five committed ProcessExecution timestamp columns.
# ---------------------------------------------------------------------------


def test_run_writes_a_readable_event_log_with_the_five_timestamp_columns(
    isolated_cwd: Path,
) -> None:
    compiled = _compiled_model(isolated_cwd)
    driver = RunDriver(compiled)

    result = driver.run(_plan(), seed=1, replication_index=0)

    df = pl.read_parquet(result.event_log_path)

    assert df.height > 0
    for column in FIVE_TIMESTAMP_COLUMNS:
        assert column in df.columns, f"missing committed timestamp column {column!r}"
        assert df.schema[column] == pl.Float64

    # material_ready_time is nullable by contract (D-034); the other four are
    # always populated for a completed firing.
    for column in ("queue_arrival_time", "actual_start", "actual_end", "release_time"):
        assert df[column].null_count() == 0


# ---------------------------------------------------------------------------
# Acceptance 3a — a second run() on the same RunDriver does not re-parse or
# re-compile the already-loaded model.
# ---------------------------------------------------------------------------


def test_second_run_does_not_reparse_or_recompile_the_model(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path = _write_model(isolated_cwd)

    parse_calls: list[None] = []
    real_safe_load = loader_module.yaml.safe_load

    def spy_safe_load(*args: object, **kwargs: object) -> object:
        parse_calls.append(None)
        return real_safe_load(*args, **kwargs)

    monkeypatch.setattr(loader_module.yaml, "safe_load", spy_safe_load)

    compile_calls: list[None] = []
    real_compile = compile_module.LocationCompiler.compile

    def spy_compile(self: object, *args: object, **kwargs: object) -> object:
        compile_calls.append(None)
        return real_compile(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(compile_module.LocationCompiler, "compile", spy_compile)

    compiled = load_model(str(model_path))
    assert len(parse_calls) == 1, "load_model() itself must parse exactly once"
    assert len(compile_calls) == 1, "load_model() itself must compile exactly once"

    driver = RunDriver(compiled)
    plan = _plan()

    driver.run(plan, seed=1, replication_index=0)
    assert len(parse_calls) == 1, "run() must not re-parse the model.yaml file"
    assert len(compile_calls) == 1, "run() must not re-compile the model"

    driver.run(plan, seed=1, replication_index=1)
    assert len(parse_calls) == 1, "a SECOND run() must not re-parse the model.yaml file"
    assert len(compile_calls) == 1, "a SECOND run() must not re-compile the model"


# ---------------------------------------------------------------------------
# Acceptance 3b — run() executes in-process; it never spawns a subprocess.
# ---------------------------------------------------------------------------


def test_run_executes_in_process_with_no_subprocess_spawned(isolated_cwd: Path) -> None:
    compiled = _compiled_model(isolated_cwd)
    driver = RunDriver(compiled)
    plan = _plan()

    with (
        mock.patch("subprocess.Popen", wraps=subprocess.Popen) as popen_spy,
        mock.patch("multiprocessing.Process") as process_spy,
    ):
        driver.run(plan, seed=1, replication_index=0)

    assert popen_spy.call_count == 0, "run() spawned a subprocess via subprocess.Popen"
    assert process_spy.call_count == 0, "run() spawned a subprocess via multiprocessing.Process"


# ---------------------------------------------------------------------------
# Acceptance 4 — determinism: identical seed + replication_index produces
# identical event logs.
# ---------------------------------------------------------------------------


def test_two_runs_at_same_seed_and_replication_index_produce_identical_event_logs(
    isolated_cwd: Path,
) -> None:
    compiled = _compiled_model(isolated_cwd)
    driver = RunDriver(compiled)

    result_a = driver.run(_plan(), seed=7, replication_index=0)
    result_b = driver.run(_plan(), seed=7, replication_index=0)

    df_a = pl.read_parquet(result_a.event_log_path)
    df_b = pl.read_parquet(result_b.event_log_path)

    assert df_a.equals(df_b), "same seed + replication_index must reproduce byte-identical rows"


# ---------------------------------------------------------------------------
# Adversarial — an empty plan (no WIP, no orders) still terminates cleanly:
# no warm-up wait, no hang on a Location.run() that never receives an arrival.
# ---------------------------------------------------------------------------


def test_run_with_empty_plan_terminates_immediately_with_an_empty_event_log(
    isolated_cwd: Path,
) -> None:
    compiled = _compiled_model(isolated_cwd)
    driver = RunDriver(compiled)

    result = driver.run([], seed=1, replication_index=0)

    df = pl.read_parquet(result.event_log_path)
    assert df.height == 0
    assert result.horizon == pytest.approx(0.0)
