"""Standalone CLI surface for RM-05 operational data workflows."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from twinflow.data import (
    ActualRecord,
    ForecastRecord,
    FreshnessRule,
    SnapshotBuilder,
    SQLiteEventStore,
    backtest_to_dict,
    load_events,
    score_forecasts,
    snapshot_to_dict,
)
from twinflow.data.errors import DataError, DataFormatError


ParserT = TypeVar("ParserT", bound=argparse.ArgumentParser)


def add_data_subcommands(subparsers: argparse._SubParsersAction[ParserT]) -> None:
    """Attach RM-05 commands to an application's subparser collection."""
    data_parser = subparsers.add_parser("data")
    nested = data_parser.add_subparsers(dest="data_command", required=True)
    _configure_commands(nested)


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone `python -m twinflow.cli.data` parser."""
    parser = argparse.ArgumentParser(prog="twinflow-data")
    commands = parser.add_subparsers(dest="command", required=True)
    _configure_commands(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a data command and return a process exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.handler(args))
    except (DataError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _configure_commands(
    commands: argparse._SubParsersAction[ParserT],
) -> None:
    import_parser = commands.add_parser("import")
    import_parser.add_argument("--db", required=True)
    import_parser.add_argument("input")
    import_parser.set_defaults(handler=_handle_import)

    events_parser = commands.add_parser("events")
    events_parser.add_argument("--db", required=True)
    events_parser.add_argument("--known-at")
    events_parser.add_argument("--occurred-through")
    events_parser.set_defaults(handler=_handle_events)

    snapshot_parser = commands.add_parser("snapshot")
    snapshot_parser.add_argument("--db", required=True)
    snapshot_parser.add_argument("--known-at", required=True)
    snapshot_parser.add_argument("--occurred-through")
    snapshot_parser.add_argument("--freshness-rules")
    snapshot_parser.add_argument("--save", action="store_true")
    snapshot_parser.set_defaults(handler=_handle_snapshot)

    validate_parser = commands.add_parser("validate-forecasts")
    validate_parser.add_argument("forecasts")
    validate_parser.add_argument("actuals")
    validate_parser.set_defaults(handler=_handle_validate_forecasts)


def _handle_import(args: argparse.Namespace) -> int:
    result = SQLiteEventStore(args.db).ingest(load_events(args.input))
    _print_json({"inserted": result.inserted, "duplicates": result.duplicates})
    return 0


def _handle_events(args: argparse.Namespace) -> int:
    events = SQLiteEventStore(args.db).events(
        known_at=_optional_timestamp(args.known_at),
        occurred_through=_optional_timestamp(args.occurred_through),
    )
    _print_json([json.loads(event.raw_record) for event in events])
    return 0


def _handle_snapshot(args: argparse.Namespace) -> int:
    rules = _load_freshness_rules(args.freshness_rules) if args.freshness_rules else ()
    snapshot = SnapshotBuilder(SQLiteEventStore(args.db), freshness_rules=rules).as_known_at(
        _timestamp(args.known_at),
        occurred_through=_optional_timestamp(args.occurred_through),
        save=bool(args.save),
    )
    _print_json(snapshot_to_dict(snapshot))
    return 0


def _handle_validate_forecasts(args: argparse.Namespace) -> int:
    forecast_rows = _load_json_array(args.forecasts)
    actual_rows = _load_json_array(args.actuals)
    forecasts = [
        ForecastRecord(
            item_id=_string(row, "item_id"),
            decision_at=_timestamp(_string(row, "decision_at")),
            point_date=_timestamp(_string(row, "point_date")),
            quantiles={
                float(key): _timestamp(str(value))
                for key, value in _mapping(row.get("quantiles", {}), "quantiles").items()
            },
            segment=str(row.get("segment", "all")),
            baseline_date=_timestamp(str(row["baseline_date"]))
            if row.get("baseline_date") is not None
            else None,
        )
        for row in forecast_rows
    ]
    actuals = [
        ActualRecord(
            item_id=_string(row, "item_id"),
            observed_date=_timestamp(str(row["observed_date"]))
            if row.get("observed_date") is not None
            else None,
            censored_at=_timestamp(str(row["censored_at"]))
            if row.get("censored_at") is not None
            else None,
        )
        for row in actual_rows
    ]
    _print_json(backtest_to_dict(score_forecasts(forecasts, actuals)))
    return 0


def _load_freshness_rules(path: str) -> tuple[FreshnessRule, ...]:
    return tuple(
        FreshnessRule(
            name=_string(row, "name"),
            maximum_age_seconds=float(row["maximum_age_seconds"]),
            source=str(row["source"]) if row.get("source") is not None else None,
            entity_type=str(row["entity_type"]) if row.get("entity_type") is not None else None,
        )
        for row in _load_json_array(path)
    )


def _load_json_array(path: str) -> list[dict[str, Any]]:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise DataFormatError(f"invalid JSON in {path}: {exc.msg}") from exc
    if not isinstance(document, list) or not all(isinstance(row, dict) for row in document):
        raise DataFormatError(f"{path} must contain a JSON array of objects")
    return document


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DataFormatError(f"{name} must be an object")
    return value


def _string(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise DataFormatError(f"{name} must be a non-empty string")
    return value


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataFormatError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataFormatError(f"timestamp must include a timezone: {value}")
    return parsed.astimezone(UTC)


def _optional_timestamp(value: str | None) -> datetime | None:
    return _timestamp(value) if value is not None else None


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
