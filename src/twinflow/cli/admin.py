"""Offline administration: workspace backup and restore, caller tokens, the signing key.

``token new`` prints the minted token once on stdout and stores only its hash (D-117).
``signing-key new`` prints only the new key's id; key material never reaches the terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from twinflow.application.backup import backup_workspace, restore_workspace
from twinflow.service.auth import add_token


def build_parser() -> argparse.ArgumentParser:
    """The ``twinflow admin`` argument parser."""
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
    _add_token_commands(commands)
    _add_signing_key_commands(commands)
    return parser


def _add_token_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    token = commands.add_parser("token").add_subparsers(dest="token_command", required=True)
    token_new = token.add_parser("new", help="mint a caller token; prints it once")
    token_new.add_argument("--caller", required=True)
    token_new.add_argument("--scopes", required=True, help="comma-separated: read,plan")
    token_new.add_argument("--days", type=int, default=None)
    token_new.add_argument("--tokens-file", default=None, help="default: $TWINFLOW_TOKENS_FILE")
    token_new.set_defaults(handler=_token_new)


def _add_signing_key_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    signing_key = commands.add_parser("signing-key").add_subparsers(
        dest="signing_key_command", required=True
    )
    signing_key_new = signing_key.add_parser("new", help="write a new Ed25519 service key")
    signing_key_new.add_argument("--out", required=True)
    signing_key_new.set_defaults(handler=_signing_key_new)


def main(argv: list[str] | None = None) -> int:
    """Run one admin command. Returns a process exit code."""
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


def _token_new(args: argparse.Namespace) -> int:
    tokens_file = args.tokens_file or os.environ.get("TWINFLOW_TOKENS_FILE", "").strip()
    if not tokens_file:
        print("error: pass --tokens-file or set TWINFLOW_TOKENS_FILE", file=sys.stderr)
        return 2
    scopes = [scope.strip() for scope in args.scopes.split(",") if scope.strip()]
    token = add_token(Path(tokens_file), caller_id=args.caller, scopes=scopes, days=args.days)
    print(token)
    return 0


def _signing_key_new(args: argparse.Namespace) -> int:
    # Imported here so the rest of the admin CLI works without the signing dependency.
    from twinflow.service.signing import generate_signing_key, key_id_of

    path = Path(args.out)
    generate_signing_key(path)
    print(key_id_of(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
