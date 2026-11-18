"""Pieces the project commands share: error handling and opening a project (TF-006).

Exit codes: 0 ok, 1 a problem the user can fix (the message names the path and a fix),
2 an internal failure (REQ-213, D-145).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from twinflow.extensions.project import Project, ProjectError

STAGING_SKIP = shutil.ignore_patterns("runs", "__pycache__", ".git", ".venv")


class UserError(Exception):
    """A problem the user can fix. The message says where and how."""


def guarded(handler: Callable[[argparse.Namespace], int], args: argparse.Namespace) -> int:
    """Run a handler: user problems are exit 1, anything else is "internal error", exit 2."""
    try:
        return int(handler(args))
    except (ProjectError, UserError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # an internal failure: say so and use exit code 2
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def open_project(path: str) -> Project:
    """Open a project folder, with a plain message when it is missing."""
    if not Path(path).exists():
        raise UserError(
            f"{path} does not exist. Fix: give the project folder (it holds one *.twin.yaml file)."
        )
    return Project.open(path)


@contextmanager
def staged_project(
    path: str, *, policies: Path | None = None, extra: dict[str, Path] | None = None
) -> Iterator[Project]:
    """Open the project from a temporary copy, with its `policies/` folder swapped for
    `policies` and the `extra` files (name -> source) added to it. The real folder is never
    touched. Used by `policies race --policies` and by `improve`."""
    source = open_project(path).folder
    with tempfile.TemporaryDirectory(prefix="twinflow-stage-") as scratch:
        stage = Path(scratch) / source.name
        shutil.copytree(source, stage, ignore=STAGING_SKIP)
        if policies is not None:
            shutil.rmtree(stage / "policies", ignore_errors=True)
            shutil.copytree(policies, stage / "policies", ignore=STAGING_SKIP)
        for name, file in (extra or {}).items():
            (stage / "policies").mkdir(exist_ok=True)
            shutil.copyfile(file, stage / "policies" / name)
        yield Project.open(stage)


def whole_number(text: str, flag: str, *, minimum: int = 0) -> int:
    """A whole number at least `minimum`, or a user error naming the flag."""
    fix = f"Fix: give a whole number of {minimum} or more, like {flag} {max(minimum, 5)}."
    try:
        value = int(text)
    except ValueError:
        raise UserError(f"{flag} must be a whole number, not {text!r}. {fix}") from None
    if value < minimum:
        raise UserError(f"{flag} must be at least {minimum}, not {value}. {fix}")
    return value
