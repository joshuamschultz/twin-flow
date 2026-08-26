"""COMP-029 Cli — integration tests (T-046, red phase).

`cli/main.py` is a thin `argparse` wrapper over four subcommands. It holds NO
logic; every command sequences calls into Layer 2/3/4 entry points that
already exist and do the real work (`model.load_model`/`validate_model`,
`plan.loader.load_plan`, `plan.replication.ReplicationRunner`,
`instrumentation.sweep.SweepHarness`, `instrumentation.kpis.KpiEngine`,
`report.assumptions.AssumptionsCollector`, `report.render_html`,
`report.write_kpi_json`, `run_stamp.RunStamp`). Nothing in the package may
import `cli` (structure.md: "cli ... may import all; nothing may import it").

--------------------------------------------------------------------------------
COMMITTED CLI CONTRACT (this test file fixes it; the T-047 implementer conforms)

    def main(argv: list[str] | None = None) -> int

`main` NEVER raises `SystemExit`. This is a deliberate strengthening of plain
`argparse` default behavior (which calls `sys.exit()` on a parse error): every
failure path — an unknown subcommand, a missing required flag, a bad `--reps`
value, no subcommand at all — is caught inside `main` and converted to a
non-zero return value, so `main(argv)` is always safely callable in-process
(exactly the same "callable in-process, never a bare process boundary"
convention `plan.driver.RunDriver.run` already established, D-041). Concretely
this means the implementer configures `argparse.ArgumentParser` to not
auto-exit (`exit_on_error=False` plus catching `argparse.ArgumentError`, or an
equivalent), or wraps `parser.parse_args()` in a `try/except SystemExit`.

Four subcommands:

    ftwin validate <model>
        Runs `factory_twin.model.validate_model(model_path)` — NO plan
        argument exists on this subcommand at all (COMP-017's own contract:
        "Inputs: compiled model (no plan required)"). Prints every returned
        `ValidationError` (path + message). Returns 0 if the list is empty,
        non-zero otherwise.

    ftwin run <model> --plan <plan> --reps N
        `--plan` and `--reps` are both REQUIRED flags (missing either is an
        argument error -> non-zero, never a crash). Loads the model, loads the
        plan against the compiled model's `PartTypeRegistry`, runs `--reps`
        replications via `ReplicationRunner`, computes one `KpiSet` via
        `KpiEngine` over the combined replications' event logs, collects
        Assumptions, builds a `RunStamp`, and writes ALL FOUR run artifacts
        (see "Run artifact layout" below). Returns 0 on success.

    ftwin balance <model> --plan <plan> --sweep <sweep> --reps N
        `--plan`, `--sweep` and `--reps` are all REQUIRED. `--sweep` names a
        JSON file (see "Sweep spec file format" below), loaded and passed
        straight through to `SweepHarness.run()`'s `sweep: dict[str,
        list[Any]]` parameter. Returns 0 on success.

    ftwin report <run-id> --out html
        `<run-id>` is a positional argument naming an existing directory
        under `runs/` (created by a prior `run` or `balance` invocation in
        THIS process's current working directory — see "How report finds a
        run" below). `--out` is required; only `"html"` is a legal value in
        v1. Returns non-zero if `runs/<run-id>/` does not exist (or is
        missing the `kpis.json` proof-of-completion artifact). On success,
        ensures `runs/<run-id>/report.html` exists and returns 0.

--------------------------------------------------------------------------------
RUN ARTIFACT LAYOUT (structure.md's already-committed folder layout; this file
only pins down WHICH command populates it and when)

    runs/<run-id>/
        events.parquet   -- every dispatched replication's event rows,
                             concatenated, with `replication` as an ordinary
                             int column (mirrors the exact pattern
                             `instrumentation/sweep.py`'s `_write_partition`
                             already uses for one sweep point).
        kpis.json        -- written by `report.write_kpi_json`. Doubles as
                             this file's "the run completed" proof-of-life
                             marker: `report`'s existence check keys off it.
        report.html      -- written by `report.render_html`, either eagerly
                             by `run` (which already holds every object
                             `render_html` needs: `KpiSet`, `Assumption`
                             list, `RunStamp`) or (re)confirmed by `report`.
        run_meta.json     -- `RunStamp.create(...).to_dict()`, JSON-dumped.

`<run-id>` naming scheme is NOT pinned down by this file (any fresh, unique,
filesystem-safe string is legal — e.g. structure.md's own
`<utc-timestamp>-<short-hash>` convention). Tests that need a real run-id
discover it by diffing `runs/`'s directory listing before and after a
successful `run` call, never by parsing printed output or assuming a format.

HOW REPORT FINDS A RUN: `<run-id>` is looked up as a literal subdirectory name
under `./runs/` (relative to CWD, exactly like `RunDriver.run`'s own
`Path("runs") / run_id` convention, `plan/driver.py`). `report` never scans
sweep output or any other location — a `balance` run's `<run-id>` is looked up
the identical way, since `balance` also creates one `runs/<run-id>/` directory
per the `run artifact layout` above (holding `sweep_point=*` partitions plus
whatever machine-readable KPI table `balance` writes, not asserted here).

--------------------------------------------------------------------------------
SWEEP SPEC FILE FORMAT: JSON, not YAML. `model/loader.py` is the ONLY module
in the whole package allowed to import PyYAML (structure.md: "`model/loader.py`
[is] the only importer of PyYAML" -- one importer keeps the `yaml.safe_load`-
only guarantee, D-001, enforceable by a single grep). `cli/main.py` reading a
sweep grid off disk via a second YAML parser would violate that boundary, so
the sweep file is a plain JSON object matching `SweepHarness.run()`'s already-
committed `sweep: dict[str, list[Any]]` shape verbatim, e.g.:

    {"locations[cut].time_model.rate": [8, 12]}

loaded via the stdlib `json` module (no boundary issue: nothing in
structure.md restricts `json`, only PyYAML and simpleeval have single-importer
rules).

--------------------------------------------------------------------------------
STRUCTURAL CHECK DESIGN (acceptance 3 -- "no logic beyond parsing and
delegation")

A robust, non-brittle proxy: `cli/main.py` never imports a LIBRARY that only a
lower layer should need to perform actual computation (Polars, SimPy, SciPy,
NumPy -- each already single-purposed to one or two owning layers per tech.md
"Library Choices" and structure.md's per-module "Forbidden Imports" table). If
`cli/main.py` were computing KPIs or running simulation steps itself instead
of delegating, it would need to import one of these directly; thin
parse-and-delegate code never does. Paired with a positive check that
`cli/main.py` DOES import from the layers it's supposed to delegate to
(`factory_twin.model`, `factory_twin.plan`, `factory_twin.instrumentation`,
`factory_twin.report`), which is real, checkable evidence of delegation rather
than a brittle line-count or AST-shape rule. This mirrors the exact
`ast.walk` + `_imported_module_names` boundary-guard pattern already committed
in `tests/unit/test_assumptions.py::TestReportModuleBoundary`.

--------------------------------------------------------------------------------
RED-PHASE NOTE: `cli/main.py` currently defines

    def main(argv: list[str] | None = None) -> int:
        raise NotImplementedError("T-047")

a plain function whose signature already matches every call below, so every
behavioral test in this file fails AT THAT LINE with `NotImplementedError` --
the right reason ("feature absent"), never an `ImportError` or a
signature-mismatch `TypeError`, because `main` imports and is callable today;
only calling it fails. The two structural tests (acceptance 3 and 4) are
boundary guards expected to PASS today and stay passing -- same precedent
`test_assumptions.py::TestReportModuleBoundary` already sets.

Real collaborators throughout: real YAML/CSV/JSON on disk, the real
(already-shipped) `load_model`/`load_plan`/`ReplicationRunner`/`SweepHarness`/
`KpiEngine`/`AssumptionsCollector`/`RunStamp`/`render_html`/`write_kpi_json`,
real SimPy, real Polars/Parquet round-trips. No mocks anywhere in this file --
`main` is exercised purely as a black box via its own committed `argv -> int`
contract, and every assertion is made by reading `main`'s own returned exit
code and the artifacts it left on disk back in this process.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from factory_twin.cli.main import main

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "factory_twin"

# ---------------------------------------------------------------------------
# Fixture vocabulary -- the same minimal cut -> pack shape
# `tests/integration/test_run_driver.py` and `test_sweep.py` already proved
# works end to end, kept identical so a RED failure here can never be blamed
# on an unfamiliar fixture.
# ---------------------------------------------------------------------------


def _model_yaml(pack_machine: str = "pack_m") -> str:
    return f"""
stocks: []

part_types:
  - name: raw_wire
    uom: ft
    attributes: {{}}
  - name: blank
    uom: piece
    attributes: {{}}
  - name: finished
    uom: piece
    attributes: {{}}

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
    machine: {pack_machine}
    labor_skill: pack_op

routing:
  - part: finished
    steps: [cut, pack]

processes: []
bom: []
"""


PLAN_CSV = """work_order_id,part,qty,start_date,due_date
wo-1,finished,2,0,100000
"""


def _write_valid_model(dir_path: Path, name: str = "model.yaml") -> Path:
    path = dir_path / name
    path.write_text(_model_yaml(), encoding="utf-8")
    return path


def _write_invalid_model(dir_path: Path, name: str = "bad_model.yaml") -> Path:
    """`pack`'s `machine` references `phantom_machine`, never declared under
    top-level `machines:` -- the exact "missing reference" failure mode
    `tests/unit/test_validate.py::test_location_machine_is_not_declared_in_top_level_machines`
    already establishes triggers exactly one `ValidationError`."""
    path = dir_path / name
    path.write_text(_model_yaml(pack_machine="phantom_machine"), encoding="utf-8")
    return path


def _write_plan(dir_path: Path, name: str = "plan.csv") -> Path:
    path = dir_path / name
    path.write_text(PLAN_CSV, encoding="utf-8")
    return path


def _write_sweep(dir_path: Path, name: str = "sweep.json") -> Path:
    path = dir_path / name
    path.write_text(
        json.dumps({"locations[cut].time_model.rate": [8, 12]}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run inside tmp_path so `runs/<run-id>/` output never touches the real
    repository tree, exactly as the RunDriver/SweepHarness integration tests'
    fixture of the same name already does."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_dir_names(cwd: Path) -> set[str]:
    runs_dir = cwd / "runs"
    if not runs_dir.exists():
        return set()
    return {entry.name for entry in runs_dir.iterdir() if entry.is_dir()}


def _new_run_id(before: set[str], after: set[str]) -> str:
    added = after - before
    assert len(added) == 1, f"expected exactly one new run directory, found {added}"
    return next(iter(added))


# ---------------------------------------------------------------------------
# Import sanity
# ---------------------------------------------------------------------------


def test_cli_main_imports_cleanly_and_is_callable() -> None:
    assert callable(main)


# ---------------------------------------------------------------------------
# Acceptance 2 -- `validate` runs with a model and NO plan.
# ---------------------------------------------------------------------------


class TestValidateSubcommand:
    def test_validate_valid_model_with_no_plan_flag_exits_zero(self, isolated_cwd: Path) -> None:
        model_path = _write_valid_model(isolated_cwd)

        exit_code = main(["validate", str(model_path)])

        assert exit_code == 0

    def test_validate_invalid_model_exits_nonzero_and_prints_the_errors(
        self, isolated_cwd: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        model_path = _write_invalid_model(isolated_cwd)

        exit_code = main(["validate", str(model_path)])

        assert exit_code != 0
        captured = capsys.readouterr()
        # The ValidationError's offending path and/or message must actually
        # reach the user -- not merely "exit code is nonzero" with silence.
        assert "phantom_machine" in captured.out + captured.err

    def test_validate_missing_model_file_exits_nonzero(self, isolated_cwd: Path) -> None:
        exit_code = main(["validate", str(isolated_cwd / "does_not_exist.yaml")])

        assert exit_code != 0


# ---------------------------------------------------------------------------
# Acceptance 1 -- `run` accepts its documented arguments; non-zero on failure,
# zero on the happy path; writes run artifacts under a run dir.
# ---------------------------------------------------------------------------


class TestRunSubcommand:
    def test_run_happy_path_exits_zero_and_writes_all_four_run_artifacts(
        self, isolated_cwd: Path
    ) -> None:
        model_path = _write_valid_model(isolated_cwd)
        plan_path = _write_plan(isolated_cwd)
        before = _run_dir_names(isolated_cwd)

        exit_code = main(["run", str(model_path), "--plan", str(plan_path), "--reps", "1"])

        assert exit_code == 0
        after = _run_dir_names(isolated_cwd)
        run_id = _new_run_id(before, after)
        run_dir = isolated_cwd / "runs" / run_id

        assert (run_dir / "events.parquet").is_file()
        assert (run_dir / "kpis.json").is_file()
        assert (run_dir / "report.html").is_file()
        assert (run_dir / "run_meta.json").is_file()

        # kpis.json must be real, parseable JSON carrying at least the
        # objective-agnostic minimum (KpiJsonSidecar's committed contract) --
        # not an empty or malformed placeholder.
        kpi_payload = json.loads((run_dir / "kpis.json").read_text(encoding="utf-8"))
        assert "run_hours" in kpi_payload
        assert "setup_hours" in kpi_payload

    def test_run_missing_plan_file_exits_nonzero(self, isolated_cwd: Path) -> None:
        model_path = _write_valid_model(isolated_cwd)

        exit_code = main(
            [
                "run",
                str(model_path),
                "--plan",
                str(isolated_cwd / "does_not_exist.csv"),
                "--reps",
                "1",
            ]
        )

        assert exit_code != 0

    def test_run_invalid_model_exits_nonzero(self, isolated_cwd: Path) -> None:
        model_path = _write_invalid_model(isolated_cwd)
        plan_path = _write_plan(isolated_cwd)

        exit_code = main(["run", str(model_path), "--plan", str(plan_path), "--reps", "1"])

        assert exit_code != 0

    def test_run_missing_required_plan_flag_exits_nonzero(self, isolated_cwd: Path) -> None:
        model_path = _write_valid_model(isolated_cwd)

        exit_code = main(["run", str(model_path), "--reps", "1"])

        assert exit_code != 0

    def test_run_missing_required_reps_flag_exits_nonzero(self, isolated_cwd: Path) -> None:
        model_path = _write_valid_model(isolated_cwd)
        plan_path = _write_plan(isolated_cwd)

        exit_code = main(["run", str(model_path), "--plan", str(plan_path)])

        assert exit_code != 0


# ---------------------------------------------------------------------------
# Acceptance 1 -- `balance` accepts its documented arguments; non-zero on
# failure, zero on the happy path.
# ---------------------------------------------------------------------------


class TestBalanceSubcommand:
    def test_balance_happy_path_exits_zero_and_writes_sweep_output(
        self, isolated_cwd: Path
    ) -> None:
        model_path = _write_valid_model(isolated_cwd)
        plan_path = _write_plan(isolated_cwd)
        sweep_path = _write_sweep(isolated_cwd)
        before = _run_dir_names(isolated_cwd)

        exit_code = main(
            [
                "balance",
                str(model_path),
                "--plan",
                str(plan_path),
                "--sweep",
                str(sweep_path),
                "--reps",
                "1",
            ]
        )

        assert exit_code == 0
        after = _run_dir_names(isolated_cwd)
        run_id = _new_run_id(before, after)
        run_dir = isolated_cwd / "runs" / run_id

        # SweepHarness's own committed contract: one `sweep_point=<id>`
        # partition directory per grid point, each holding at least one
        # `.parquet` file (this file's grid declares 2 candidate values ->
        # 2 points).
        partition_dirs = sorted(p for p in run_dir.glob("sweep_point=*") if p.is_dir())
        assert len(partition_dirs) == 2
        for partition_dir in partition_dirs:
            assert list(partition_dir.glob("*.parquet")), f"{partition_dir} has no parquet output"

    def test_balance_missing_sweep_file_exits_nonzero(self, isolated_cwd: Path) -> None:
        model_path = _write_valid_model(isolated_cwd)
        plan_path = _write_plan(isolated_cwd)

        exit_code = main(
            [
                "balance",
                str(model_path),
                "--plan",
                str(plan_path),
                "--sweep",
                str(isolated_cwd / "does_not_exist.json"),
                "--reps",
                "1",
            ]
        )

        assert exit_code != 0

    def test_balance_invalid_model_exits_nonzero(self, isolated_cwd: Path) -> None:
        model_path = _write_invalid_model(isolated_cwd)
        plan_path = _write_plan(isolated_cwd)
        sweep_path = _write_sweep(isolated_cwd)

        exit_code = main(
            [
                "balance",
                str(model_path),
                "--plan",
                str(plan_path),
                "--sweep",
                str(sweep_path),
                "--reps",
                "1",
            ]
        )

        assert exit_code != 0


# ---------------------------------------------------------------------------
# Acceptance 1 -- `report` accepts its documented arguments; non-zero on
# failure, zero on the happy path.
# ---------------------------------------------------------------------------


class TestReportSubcommand:
    def test_report_on_a_completed_run_exits_zero_and_writes_report_html(
        self, isolated_cwd: Path
    ) -> None:
        model_path = _write_valid_model(isolated_cwd)
        plan_path = _write_plan(isolated_cwd)
        before = _run_dir_names(isolated_cwd)
        run_exit = main(["run", str(model_path), "--plan", str(plan_path), "--reps", "1"])
        assert run_exit == 0
        run_id = _new_run_id(before, _run_dir_names(isolated_cwd))

        exit_code = main(["report", run_id, "--out", "html"])

        assert exit_code == 0
        assert (isolated_cwd / "runs" / run_id / "report.html").is_file()

    def test_report_unknown_run_id_exits_nonzero(self, isolated_cwd: Path) -> None:
        exit_code = main(["report", "no-such-run-id", "--out", "html"])

        assert exit_code != 0


# ---------------------------------------------------------------------------
# Adversarial -- cases beyond the listed acceptance criteria: malformed
# invocations that must fail CLEANLY (a non-zero return), never crash `main`
# with an uncaught exception or an escaped `SystemExit`.
# ---------------------------------------------------------------------------


class TestMalformedInvocationsFailCleanly:
    def test_unknown_subcommand_exits_nonzero(self) -> None:
        assert main(["frobnicate"]) != 0

    def test_no_subcommand_at_all_exits_nonzero(self) -> None:
        assert main([]) != 0

    def test_run_with_non_integer_reps_exits_nonzero(self, isolated_cwd: Path) -> None:
        model_path = _write_valid_model(isolated_cwd)
        plan_path = _write_plan(isolated_cwd)

        exit_code = main(
            [
                "run",
                str(model_path),
                "--plan",
                str(plan_path),
                "--reps",
                "not-a-number",
            ]
        )

        assert exit_code != 0

    def test_run_with_zero_reps_exits_nonzero_not_a_crash(self, isolated_cwd: Path) -> None:
        """Degenerate input (reps=0) must fail closed, not propagate an
        uncaught exception out of `main` (e.g. `multiprocessing.Pool
        (processes=0)` raising `ValueError` deep inside `ReplicationRunner`).
        `main`'s own committed contract is `argv -> int`, always."""
        model_path = _write_valid_model(isolated_cwd)
        plan_path = _write_plan(isolated_cwd)

        exit_code = main(["run", str(model_path), "--plan", str(plan_path), "--reps", "0"])

        assert exit_code != 0


# ---------------------------------------------------------------------------
# Acceptance 3 -- the CLI module contains NO logic beyond argument parsing
# and delegation. Structural guard; expected to PASS today (see module
# docstring "STRUCTURAL CHECK DESIGN").
# ---------------------------------------------------------------------------


def _imported_module_names(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestCliHoldsNoLogicBeyondParsingAndDelegation:
    """Structural guard, not a T-047 behavior test -- expected to PASS today
    and stay passing, proving the boundary this task's contract depends on
    (same pattern as `tests/unit/test_assumptions.py::TestReportModuleBoundary`)."""

    _COMPUTATION_LIBRARIES = {"polars", "simpy", "scipy", "numpy"}
    _DELEGATION_TARGET_PREFIXES = (
        "factory_twin.model",
        "factory_twin.plan",
        "factory_twin.instrumentation",
        "factory_twin.report",
        "factory_twin.run_stamp",
    )

    def test_cli_main_never_imports_a_computation_library_directly(self) -> None:
        cli_main = SRC_ROOT / "cli" / "main.py"
        imported = _imported_module_names(cli_main)

        offending = {name for name in imported if name.split(".")[0] in self._COMPUTATION_LIBRARIES}
        assert offending == set(), (
            f"cli/main.py imports computation librar{'y' if len(offending) == 1 else 'ies'} "
            f"directly (should delegate to a Layer 2/3/4 module instead): {offending}"
        )

    def test_cli_main_imports_from_at_least_one_delegation_target(self) -> None:
        cli_main = SRC_ROOT / "cli" / "main.py"
        imported = _imported_module_names(cli_main)

        delegates = {
            name
            for name in imported
            if any(name.startswith(prefix) for prefix in self._DELEGATION_TARGET_PREFIXES)
        }
        assert delegates, (
            "cli/main.py imports nothing from model/plan/instrumentation/report/"
            "run_stamp -- it has no Layer 2/3/4 entry point to delegate to"
        )


# ---------------------------------------------------------------------------
# Acceptance 4 -- nothing in the package imports cli. Structural guard;
# expected to PASS today (see module docstring "RED-PHASE NOTE").
# ---------------------------------------------------------------------------


class TestNothingImportsCli:
    def test_no_module_outside_cli_imports_factory_twin_cli(self) -> None:
        offending: list[str] = []
        for py_file in SRC_ROOT.rglob("*.py"):
            if py_file.is_relative_to(SRC_ROOT / "cli"):
                continue
            for name in _imported_module_names(py_file):
                if name == "factory_twin.cli" or name.startswith("factory_twin.cli."):
                    offending.append(f"{py_file}: {name}")
        assert offending == []
