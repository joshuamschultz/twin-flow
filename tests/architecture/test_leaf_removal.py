"""Leaf-removal architecture test (T-118, COMP-002, REQ-088, D-069).

Every module of `scheduling/algorithms/` is a leaf: deleting it must leave the package
importable, the registry listing the rest, and every remaining solver answering fixture
7.1.1 with a verified plan or a named issue. Each case copies `src/twinflow` to a temp
dir, deletes one leaf and runs a fresh interpreter whose `PYTHONPATH` is the copy. The
parameter list is collected at test time, so a new leaf is covered without editing this
file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "twinflow"
ALGORITHMS = SRC / "scheduling" / "algorithms"
FIXTURE = ROOT / "tests" / "scheduling" / "fixtures" / "pinedo_7_1_1.json"
PACKAGE = "twinflow.scheduling.algorithms"


def _leaf_modules() -> list[str]:
    if not ALGORITHMS.is_dir():
        return []
    return sorted(p.stem for p in ALGORITHMS.glob("*.py") if p.name != "__init__.py")


_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path

    root, fixture, removed = sys.argv[1], sys.argv[2], sys.argv[3]
    import twinflow
    assert twinflow.__file__.startswith(root), twinflow.__file__
    from twinflow.scheduling import registry
    from twinflow.scheduling.model import SolveRequest
    from twinflow.scheduling.solver import solve

    request = SolveRequest.model_validate_json(Path(fixture).read_text())
    rows = []
    for info in registry.available():
        row = {"name": info.name, "module": info.module}
        try:
            solver = registry.get(info.name)
        except Exception as error:
            row["get_error"] = type(error).__name__
            rows.append(row)
            continue
        result = solve(request, solver=solver, budget_seconds=10.0)
        codes = [i.code for i in (*result.issues, *result.schedule.issues)]
        row.update(verified=result.verified, codes=codes, status=result.schedule.status)
        rows.append(row)
    print(json.dumps({"removed": f"twinflow.scheduling.algorithms.{removed}", "rows": rows}))
    """
)


def _run_without(tmp_path: Path, leaf: str) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "src"
    shutil.copytree(SRC, root / "twinflow", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "twinflow" / "scheduling" / "algorithms" / f"{leaf}.py").unlink()
    script = tmp_path / "run_remaining.py"
    script.write_text(_SCRIPT)
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(  # noqa: S603  # argv is sys.executable plus paths this test made
        [sys.executable, str(script), str(root), str(FIXTURE), leaf],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=300,
        check=False,
    )


def test_algorithms_package_exists_with_leaf_modules() -> None:
    assert (ALGORITHMS / "__init__.py").is_file()
    assert "cpsat" in _leaf_modules()


@pytest.mark.slow
@pytest.mark.parametrize("leaf", _leaf_modules())
def test_removing_one_leaf_leaves_the_rest_working(tmp_path: Path, leaf: str) -> None:
    done = _run_without(tmp_path, leaf)

    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    rows = out["rows"]
    assert [row["module"] for row in rows if row["module"] == out["removed"]] == []
    assert "baseline" in [row["name"] for row in rows]
    for row in rows:
        if "get_error" in row:
            assert row["get_error"] == "OptionalDependencyError", row
            continue
        assert row["verified"] or row["codes"], row
        assert "solver_error" not in row["codes"], row  # a hidden hard dependency on the leaf
    baseline = next(row for row in rows if row["name"] == "baseline")
    assert baseline["verified"] is True, baseline
