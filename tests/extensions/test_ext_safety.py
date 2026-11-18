"""T-238: only the CLI and in-process Python load extension files (REQ-205, D-139).

The boundary rules live in `tests/test_boundaries.py`. Here a fresh interpreter proves that
running a make-to-stock capsule through the domain registry (the path the REST service and
MCP use) never imports the extensions package or any user file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAPSULE = ROOT / "examples" / "molding-press" / "molding-press.twin.yaml"

SCRIPT = """
import json, sys
from twinflow.scenario import ScenarioCapsule
capsule = ScenarioCapsule.load(sys.argv[1])
assert capsule.validate() == []
result = capsule.evaluate(seed=3)
loaded = sorted(
    name for name in sys.modules
    if name.startswith(("twinflow.extensions", "twinflow.policy_files", "twinflow_user_",
                        "twinflow_builtin_"))
)
print(json.dumps({"status": result["status"], "loaded": loaded}))
"""


def test_the_step_adapter_never_imports_extensions() -> None:
    out = subprocess.run(  # noqa: S603 - fixed interpreter and script, no shell
        [sys.executable, "-c", SCRIPT, str(CAPSULE)],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report == {"status": "completed", "loaded": []}


def test_the_service_and_mcp_entry_points_never_import_extensions() -> None:
    script = (
        "import sys\n"
        "import twinflow.application, twinflow.domain, twinflow.scenario\n"
        "try:\n    import twinflow.service.app\nexcept ImportError:\n    pass\n"
        "try:\n    import twinflow.mcp\nexcept ImportError:\n    pass\n"
        "print(sorted(m for m in sys.modules if m.startswith('twinflow.extensions')))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and script, no shell
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert out.stdout.strip().splitlines()[-1] == "[]"
