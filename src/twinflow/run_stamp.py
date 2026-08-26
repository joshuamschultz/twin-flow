"""COMP-028 RunStamp — everything needed to reproduce a run exactly.

Engine version, resolved commit SHA, model file hash, plan file hash, base seed, Python
interpreter version, resolved dependency hash. Contains no secrets and no client data —
only sha256 hex digests of file content, never a copy of the content itself.

Lives at the package root (not report/) so plan/driver can stamp a run at start without
importing a higher layer. Pure: only hashlib/sys/importlib.metadata plus twinflow's
own `__version__` (never report/, instrumentation/, model/, or plan/).
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import asdict, dataclass
from importlib import metadata as importlib_metadata

import twinflow


def _hash_file(path: str) -> str:
    """sha256 hex digest of the file's raw bytes. Raises OSError if `path` is missing."""
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _dependency_hash() -> str:
    """sha256 hex digest of the sorted `name==version` list of installed distributions.

    Deterministic within one environment — same installed set always hashes the same,
    regardless of which model/plan files or seed a given run used.
    """
    installed = sorted(
        f"{dist.name}=={dist.version}" for dist in importlib_metadata.distributions()
    )
    joined = "\n".join(installed)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunStamp:
    """Everything needed to reproduce a run exactly; serializes to `run_meta.json`."""

    engine_version: str
    commit_sha: str | None
    model_hash: str
    plan_hash: str
    base_seed: int
    python_version: str
    dependency_hash: str

    @classmethod
    def create(
        cls, model_path: str, plan_path: str, base_seed: int, commit_sha: str | None = None
    ) -> RunStamp:
        """Build a `RunStamp` from the model/plan files on disk at run start.

        Raises OSError if `model_path` or `plan_path` does not exist.
        """
        model_hash = _hash_file(model_path)
        plan_hash = _hash_file(plan_path)
        version_info = sys.version_info
        python_version = f"{version_info.major}.{version_info.minor}.{version_info.micro}"
        return cls(
            engine_version=twinflow.__version__,
            commit_sha=commit_sha,
            model_hash=model_hash,
            plan_hash=plan_hash,
            base_seed=base_seed,
            python_version=python_version,
            dependency_hash=_dependency_hash(),
        )

    def to_dict(self) -> dict[str, str | int | None]:
        """JSON-serializable dict with exactly the reproducibility fields."""
        return asdict(self)
