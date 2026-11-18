"""`twinflow new <folder> --template NAME` and `twinflow new --list` (TF-006 T-252, T-265).

A template is a complete project folder shipped as package data under
`twinflow/templates/<name>/` (D-149). The capsule in a new folder is named after the folder.
Exit codes: 0 ok, 1 a problem the user can fix, 2 an internal failure (REQ-211, REQ-225).
"""

from __future__ import annotations

import argparse
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TypeVar

from twinflow.cli._common import UserError, guarded
from twinflow.state.issues import did_you_mean

ParserT = TypeVar("ParserT", bound=argparse.ArgumentParser)

CAPSULE_SUFFIX = ".twin.yaml"
TEMPLATES = {
    "make-to-stock": "One molding press, 12 products. Race rules for what to make next.",
    "job-shop": "A CNC shop with state capture on and a dispatch rule file.",
    "custom-engine": "A toy bakery written in Python. Copy it to model your own world.",
}


def add_new_subcommands(subparsers: argparse._SubParsersAction[ParserT]) -> None:
    """Attach `new` to an application's subparser collection."""
    parser = subparsers.add_parser("new")
    parser.add_argument("folder", nargs="?")
    parser.add_argument("--template")
    parser.add_argument("--list", action="store_true", dest="list_templates")
    parser.set_defaults(handler=lambda args: guarded(_handle_new, args))


def _root() -> Traversable:
    return resources.files("twinflow").joinpath("templates")


def _copy(source: Traversable, target: Path, capsule_name: str, top: bool = True) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        if entry.name == "__pycache__":
            continue
        if entry.is_dir():
            _copy(entry, target / entry.name, capsule_name, top=False)
            continue
        name = capsule_name if top and entry.name.endswith(CAPSULE_SUFFIX) else entry.name
        (target / name).write_bytes(entry.read_bytes())


def _list() -> int:
    width = max(len(name) for name in TEMPLATES)
    for name, about in TEMPLATES.items():
        print(f"{name.ljust(width)}  {about}")
    return 0


def _handle_new(args: argparse.Namespace) -> int:
    if args.list_templates:
        return _list()
    choices = ", ".join(TEMPLATES)
    if args.template is None:
        raise UserError(
            f"new needs --template. Fix: pick one of {choices} "
            "(run `twinflow new --list` to see what each one is)."
        )
    if args.template not in TEMPLATES:
        raise UserError(
            f"{args.template!r} is not a template. {did_you_mean(args.template, list(TEMPLATES))} "
            f"Fix: pick one of {choices}."
        )
    if args.folder is None:
        raise UserError(
            "new needs a folder to create. "
            f"Fix: run `twinflow new my-plant --template {args.template}`."
        )
    target = Path(args.folder)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise UserError(
            f"{target} already exists and is not empty. "
            "Fix: pick a new folder name, or empty this folder first. Nothing was changed."
        )
    _copy(_root().joinpath(args.template), target, f"{target.resolve().name}{CAPSULE_SUFFIX}")
    print(f"Created {target} from the {args.template} template.")
    print(f"Next: twinflow extensions check {target}")
    return 0
