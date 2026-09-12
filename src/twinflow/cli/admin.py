"""Offline administration commands for dedicated workspace backup and restore."""

from __future__ import annotations

import argparse
import json
import sys

from twinflow.application.backup import backup_workspace, restore_workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="twinflow-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--workspace", required=True)
    backup.add_argument("--out", required=True)
    backup.set_defaults(handler=_backup)
    restore = commands.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("--workspace", required=True)
    restore.set_defaults(handler=_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.handler(args))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _backup(args: argparse.Namespace) -> int:
    print(json.dumps(backup_workspace(args.workspace, args.out), sort_keys=True))
    return 0


def _restore(args: argparse.Namespace) -> int:
    print(json.dumps(restore_workspace(args.archive, args.workspace), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
