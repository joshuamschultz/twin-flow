"""Load one policy file by path (minimal Phase 5 loader; Phase 3 replaces it, COMP-155).

The file is imported with `importlib.util.spec_from_file_location` under the module name
`twinflow_user_policy_<stem>_<hash8>` (D-146), where `hash8` is the first 8 hex digits of
the sha256 of the resolved path, so two folders holding the same file name never collide
in `sys.modules`. The module is not added to `sys.modules`. Every failure becomes a
`PolicyFileError` that names the file, says what is wrong and how to fix it (REQ-213).
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import traceback
from pathlib import Path
from types import ModuleType

MODULE_PREFIX = "twinflow_user_policy_"


class PolicyFileError(ValueError):
    """A policy file cannot be used. `fix` says, in plain words, how to repair it."""

    def __init__(self, path: Path, problem: str, fix: str) -> None:
        super().__init__(f"{path.name}: {problem} {fix}")
        self.path = path
        self.problem = problem
        self.fix = fix


def module_name(path: Path) -> str:
    """`twinflow_user_policy_<stem>_<hash8>` for one file path (D-146)."""
    resolved = Path(path).resolve()
    stem = re.sub(r"\W", "_", resolved.stem)
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{MODULE_PREFIX}{stem}_{digest}"


def _line_in(path: Path, exc: BaseException) -> str:
    """` (line N)` of the deepest traceback frame inside the user's file, if any."""
    if isinstance(exc, SyntaxError) and exc.lineno is not None:
        return f" (line {exc.lineno})"
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename) == path]
    return f" (line {frames[-1].lineno})" if frames else ""


def load_module(path: Path | str) -> ModuleType:
    """Import one policy file as a fresh module; raises `PolicyFileError` on any failure."""
    file = Path(path).resolve()
    if not file.is_file():
        raise PolicyFileError(file, "file not found.", "Check the path to the policy file.")
    if file.suffix != ".py":
        raise PolicyFileError(file, "a policy file must end in .py.", "Rename it to <name>.py.")
    spec = importlib.util.spec_from_file_location(module_name(file), file)
    if spec is None or spec.loader is None:
        raise PolicyFileError(file, "Python cannot load this file.", "Check it is a .py file.")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # any error in user code becomes a plain-word problem
        raise PolicyFileError(
            file,
            f"loading failed with {type(exc).__name__}: {exc}{_line_in(file, exc)}.",
            "Fix that line, then run the policy again.",
        ) from exc
    return module
