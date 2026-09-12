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
import json
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from twinflow.modules.space import Domain, IntRange

from twinflow.instrumentation import compute_kpis
from twinflow.instrumentation.aggregate import AggregatedKpis, Interval, aggregate_kpis
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
_INTERVALS_SCHEMA_VERSION = 1


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

    optimize_parser = subparsers.add_parser("optimize")
    optimize_parser.add_argument("model")
    optimize_parser.add_argument("--plan", required=True)
    optimize_parser.add_argument(
        "--lever",
        action="append",
        required=True,
        metavar="PATH:MIN:MAX[:STEP]",
        help="a tunable lever and its integer search range, e.g. labor.pools[0].headcount:2:6",
    )
    optimize_parser.add_argument("--objective", default="on_time_pct")
    optimize_parser.add_argument("--optimizer", default="hill_climb")
    optimize_parser.add_argument("--budget", type=int, default=12)
    optimize_parser.add_argument("--reps", type=int, default=12)
    optimize_parser.add_argument("--seed", type=int, default=0)
    optimize_parser.set_defaults(handler=_handle_optimize)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--models-root", default="examples")
    serve_parser.set_defaults(handler=_handle_serve)

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
        per_rep = [compute_kpis(result.event_log_path, orders, result.horizon)]
    else:
        # `ReplicationRunner` dispatches one worker process per replication,
        # each of which independently writes its own `runs/<run-id>/` via
        # `RunDriver` — real droppings the CLI does not own. Isolating the
        # dispatch inside a scratch cwd keeps those out of the real `runs/`
        # tree; the CLI's own `run_dir` is an absolute path built up front,
        # so it lands in the right place regardless of the scratch chdir.
        run_dir = Path.cwd() / "runs" / _new_run_id()
        run_dir.mkdir(parents=True, exist_ok=True)
        results = ReplicationRunner(model_path).run(
            work_orders,
            args.reps,
            _BASE_SEED,
            artifact_dir=run_dir / "replications",
        )
        shutil.copyfile(results[0].event_log_path, run_dir / "events.parquet")
        # Per-replication KPIs (one sample of a random floor each), not the
        # pooled log: pooling would multiply every count by the rep count and
        # hide the very variation we are here to measure.
        per_rep = [
            compute_kpis(
                r.event_log_path,
                orders_frame(work_orders, compiled, r.event_log_path),
                r.horizon,
            )
            for r in results
        ]

    # The first replication is the representative single run for the charts and
    # tables; the whole set becomes the confidence intervals.
    kpis = per_rep[0]
    aggregated = aggregate_kpis(per_rep)

    _write_run_artifacts(run_dir, compiled, kpis, aggregated, model_path, plan_path)
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


def _handle_optimize(args: argparse.Namespace) -> int:
    """`twinflow optimize <model> --plan <plan> --lever PATH:MIN:MAX[:STEP] ...`.

    Searches the declared integer lever space for the scenario a named objective
    prefers, via a named optimizer, and prints the winner with its confidence
    band. The twin scores; it proposes no commit — the decision stays with you.
    """
    from twinflow.modules import LeverSpace
    from twinflow.modules import optimize as run_optimize
    from twinflow.modules.objectives import OBJECTIVES
    from twinflow.modules.optimizers import OPTIMIZERS

    if args.reps < 1 or args.budget < 1:
        print("error: --reps and --budget must be positive integers", file=sys.stderr)
        return 1
    if args.objective not in OBJECTIVES:
        print(
            f"error: unknown objective {args.objective!r}; try {OBJECTIVES.names()}",
            file=sys.stderr,
        )
        return 1
    if args.optimizer not in OPTIMIZERS:
        print(
            f"error: unknown optimizer {args.optimizer!r}; try {OPTIMIZERS.names()}",
            file=sys.stderr,
        )
        return 1

    model_path = str(Path(args.model).resolve())
    plan_path = str(Path(args.plan).resolve())
    compiled = load_model(model_path)
    work_orders = load_plan(plan_path, compiled.registry)

    domains: dict[str, Domain] = {}
    for spec in args.lever:
        path, domain = _parse_lever(spec)
        domains[path] = domain
    space = LeverSpace(domains)

    result = run_optimize(
        model_path,
        work_orders,
        space,
        OBJECTIVES.create(args.objective),
        OPTIMIZERS.create(args.optimizer),
        budget=args.budget,
        reps=args.reps,
        seed=args.seed,
    )

    band = result.best.intervals.on_time_pct
    print(
        f"objective {result.objective_name} ({result.direction}) "
        f"-> best score {result.best_score:.2f}"
    )
    print(f"  levers:   {result.best.scenario.levers}")
    print(f"  on-time%: {band.mean:.1f}  (band {band.lo:.1f}-{band.hi:.1f}, {band.n} reps)")
    print(f"  evals:    {result.evaluations_used} of budget {args.budget}")
    print("  (the twin scores the options; the decision stays yours)")
    return 0


def _parse_lever(spec: str) -> tuple[str, IntRange]:
    """`PATH:MIN:MAX[:STEP]` -> (path, IntRange). Paths never contain a colon
    (they use dots + `[selector]`), so colon-splitting is unambiguous."""
    from twinflow.modules import IntRange

    parts = spec.split(":")
    if len(parts) not in (3, 4):
        raise ValueError(f"malformed --lever {spec!r}; expected PATH:MIN:MAX[:STEP]")
    path = parts[0]
    low, high = int(parts[1]), int(parts[2])
    step = int(parts[3]) if len(parts) == 4 else 1
    return path, IntRange(low, high, step)


def _handle_serve(args: argparse.Namespace) -> int:
    """`twinflow serve` — start the local REST API (needs the `api` extra)."""
    from twinflow.service.serve import main as serve_main

    return serve_main(
        ["--host", args.host, "--port", str(args.port), "--models-root", args.models_root]
    )


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _write_run_artifacts(
    run_dir: Path,
    compiled: CompiledModel,
    kpis: KpiSet,
    aggregated: AggregatedKpis,
    model_path: str,
    plan_path: str,
) -> None:
    """Write `kpis.json`, `intervals.json`, `report.html` and `run_meta.json`."""
    assumptions = AssumptionsCollector().collect(compiled)
    stamp = RunStamp.create(model_path, plan_path, base_seed=_BASE_SEED)
    write_kpi_json(kpis, _KPI_SCHEMA_VERSION, run_dir / "kpis.json")
    (run_dir / "intervals.json").write_text(
        json.dumps(_intervals_to_dict(aggregated), indent=2), encoding="utf-8"
    )
    render_html(
        kpis, assumptions, stamp, run_dir / "report.html", model=compiled, aggregated=aggregated
    )
    (run_dir / "run_meta.json").write_text(json.dumps(stamp.to_dict(), indent=2), encoding="utf-8")


def _intervals_to_dict(aggregated: AggregatedKpis) -> dict[str, Any]:
    """The confidence intervals as machine-readable JSON (schema-versioned)."""

    def iv(interval: Interval) -> dict[str, float | int]:
        return {
            "mean": interval.mean,
            "lo": interval.lo,
            "hi": interval.hi,
            "p50": interval.p50,
            "n": interval.n,
        }

    return {
        "schema_version": _INTERVALS_SCHEMA_VERSION,
        "reps": aggregated.reps,
        "level": aggregated.level,
        "on_time_pct": iv(aggregated.on_time_pct),
        "lateness_by_order": {k: iv(v) for k, v in aggregated.lateness_by_order.items()},
        "completion_by_order": {k: iv(v) for k, v in aggregated.completion_by_order.items()},
        "utilization_by_cell": {k: iv(v) for k, v in aggregated.utilization_by_cell.items()},
    }


def _new_run_id() -> str:
    """A fresh, unique id following structure.md's own run-folder convention:
    `<utc-timestamp>-<short-hash>`."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"
