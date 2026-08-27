"""COMP-029 Cli — thin argument parsing over validate/run/balance/report.

Holds no logic; every command delegates to a Layer 2/3/4 entry point
(`twinflow.model`, `twinflow.plan`, `twinflow.instrumentation`,
`twinflow.report`, `twinflow.run_stamp`). `main` never raises
`SystemExit` — every parse failure and every delegated failure is converted
to a non-zero return value instead (D-041's "callable in-process" convention,
applied to the CLI boundary itself).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from twinflow.instrumentation import compute_kpis
from twinflow.instrumentation.kpis import KpiSet
from twinflow.instrumentation.sweep import SweepHarness, orders_frame
from twinflow.model import CompiledModel, load_model, validate_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan
from twinflow.plan.replication import ReplicationRunner
from twinflow.report import render_html, write_kpi_json
from twinflow.report.assumptions import AssumptionsCollector
from twinflow.run_stamp import RunStamp

_BASE_SEED = 0
_KPI_SCHEMA_VERSION = 1


class _ArgumentParsingError(Exception):
    """Raised by `_NonExitingArgumentParser.error` instead of `SystemExit`."""


class _NonExitingArgumentParser(argparse.ArgumentParser):
    """An `ArgumentParser` that never calls `sys.exit()` on a parse failure.

    `add_subparsers()` propagates `parser_class=type(self)` to every
    sub-parser it creates, so every subcommand's own required-argument and
    type-conversion errors raise `_ArgumentParsingError` the same way.
    """

    def error(self, message: str) -> NoReturn:
        raise _ArgumentParsingError(message)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (`twinflow`). Returns a process exit code."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _ArgumentParsingError:
        return 2
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        return int(args.handler(args))
    except Exception as exc:  # the CLI boundary: never let a delegated failure escape
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = _NonExitingArgumentParser(prog="twinflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("model")
    validate_parser.set_defaults(handler=_handle_validate)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("model")
    run_parser.add_argument("--plan", required=True)
    run_parser.add_argument("--reps", required=True, type=int)
    run_parser.set_defaults(handler=_handle_run)

    balance_parser = subparsers.add_parser("balance")
    balance_parser.add_argument("model")
    balance_parser.add_argument("--plan", required=True)
    balance_parser.add_argument("--sweep", required=True)
    balance_parser.add_argument("--reps", required=True, type=int)
    balance_parser.set_defaults(handler=_handle_balance)

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("run_id")
    report_parser.add_argument("--out", required=True, choices=["html"])
    report_parser.set_defaults(handler=_handle_report)

    return parser


# ---------------------------------------------------------------------------
# Subcommand handlers — parse-then-delegate only.
# ---------------------------------------------------------------------------


def _handle_validate(args: argparse.Namespace) -> int:
    """`twinflow validate <model>` — no plan argument exists (COMP-017)."""
    errors = validate_model(args.model)
    for error in errors:
        print(f"{error.path}: {error.message}")
    return 0 if not errors else 1


def _handle_run(args: argparse.Namespace) -> int:
    """`twinflow run <model> --plan <plan> --reps N` — writes all four run
    artifacts under a fresh `runs/<run-id>/`."""
    if args.reps < 1:
        print("error: --reps must be a positive integer", file=sys.stderr)
        return 1

    model_path = str(Path(args.model).resolve())
    plan_path = str(Path(args.plan).resolve())

    compiled = load_model(model_path)
    work_orders = load_plan(plan_path, compiled.registry)

    if args.reps == 1:
        # A single replication runs in-process (no worker pool); RunDriver's
        # own auto-generated `runs/<run-id>/` directory IS the run directory,
        # so no second directory is ever created for it.
        result = RunDriver(compiled).run(work_orders, seed=_BASE_SEED, replication_index=0)
        run_dir = result.event_log_path.parent
        orders = orders_frame(work_orders, compiled, result.event_log_path)
        kpis = compute_kpis(result.event_log_path, orders, result.horizon)
    else:
        # `ReplicationRunner` dispatches one worker process per replication,
        # each of which independently writes its own `runs/<run-id>/` via
        # `RunDriver` — real droppings the CLI does not own. Isolating the
        # dispatch inside a scratch cwd keeps those out of the real `runs/`
        # tree; the CLI's own `run_dir` is an absolute path built up front,
        # so it lands in the right place regardless of the scratch chdir.
        run_dir = Path.cwd() / "runs" / _new_run_id()
        run_dir.mkdir(parents=True, exist_ok=True)
        with _scratch_cwd():
            results = ReplicationRunner(model_path).run(work_orders, args.reps, _BASE_SEED)
            shutil.copyfile(results[0].event_log_path, run_dir / "events.parquet")
            orders = orders_frame(work_orders, compiled, results[0].event_log_path)
            kpis = compute_kpis(
                [result.event_log_path for result in results],
                orders,
                max(result.horizon for result in results),
            )

    _write_run_artifacts(run_dir, compiled, kpis, model_path, plan_path)
    run_id = run_dir.name
    print(f"run {run_id}")
    print(f"  dir:    {run_dir}")
    print(f"  report: {run_dir / 'report.html'}")
    print(f"  kpis:   {run_dir / 'kpis.json'}")
    print(f"  view:   twinflow report {run_id} --out html")
    return 0


def _handle_balance(args: argparse.Namespace) -> int:
    """`twinflow balance <model> --plan <plan> --sweep <sweep> --reps N` — the
    sweep spec is plain JSON (never YAML; `model/loader.py` stays the only
    PyYAML importer, D-001), passed straight through to `SweepHarness.run()`.
    """
    if args.reps < 1:
        print("error: --reps must be a positive integer", file=sys.stderr)
        return 1

    model_path = str(Path(args.model).resolve())
    plan_path = str(Path(args.plan).resolve())
    sweep_path = Path(args.sweep).resolve()

    compiled = load_model(model_path)
    work_orders = load_plan(plan_path, compiled.registry)
    with open(sweep_path, encoding="utf-8") as handle:
        sweep = json.load(handle)

    run_dir = Path.cwd() / "runs" / _new_run_id()
    with _scratch_cwd():
        SweepHarness(model_path).run(
            plan=work_orders,
            sweep=sweep,
            reps=args.reps,
            base_seed=_BASE_SEED,
            out_dir=run_dir,
        )
    return 0


def _handle_report(args: argparse.Namespace) -> int:
    """`twinflow report <run-id> --out html` — (re)confirms `report.html` for an
    already-completed run; `kpis.json` is the "run completed" proof-of-life
    marker (COMP-027)."""
    run_dir = Path("runs") / args.run_id
    if not (run_dir / "kpis.json").is_file():
        print(f"error: no completed run found at {run_dir}", file=sys.stderr)
        return 1
    if not (run_dir / "report.html").is_file():
        print(f"error: run {args.run_id!r} completed but report.html is missing", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _write_run_artifacts(
    run_dir: Path,
    compiled: CompiledModel,
    kpis: KpiSet,
    model_path: str,
    plan_path: str,
) -> None:
    """Write `kpis.json`, `report.html` and `run_meta.json` for one run."""
    assumptions = AssumptionsCollector().collect(compiled)
    stamp = RunStamp.create(model_path, plan_path, base_seed=_BASE_SEED)
    write_kpi_json(kpis, _KPI_SCHEMA_VERSION, run_dir / "kpis.json")
    render_html(kpis, assumptions, stamp, run_dir / "report.html", model=compiled)
    (run_dir / "run_meta.json").write_text(json.dumps(stamp.to_dict(), indent=2), encoding="utf-8")


def _new_run_id() -> str:
    """A fresh, unique id following structure.md's own run-folder convention:
    `<utc-timestamp>-<short-hash>`."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


@contextlib.contextmanager
def _scratch_cwd() -> Iterator[None]:
    """Run the wrapped delegated call from a throwaway working directory.

    `plan.driver.RunDriver.run` writes its own `runs/<run-id>/` via a
    relative `Path("runs")`, resolved against whatever process it executes
    in. A worker pool (`ReplicationRunner`/`SweepHarness`) spawns one such
    write per replication — real side effects this CLI does not own and
    must not let leak into its own `runs/` tree. Every path this CLI passes
    into the delegated call (model, plan, sweep, its own `out_dir`) is
    already absolute, so relocating the CWD changes only where an internal
    *relative* write lands, never what gets written.
    """
    previous_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="twinflow-cli-scratch-") as scratch_dir:
        os.chdir(scratch_dir)
        try:
            yield
        finally:
            os.chdir(previous_cwd)
