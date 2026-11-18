"""T-249 / T-257: a `DECISION = "dispatch"` policy file runs as a `Policy` (COMP-154,
COMP-155, D-150).

`extensions.dispatch.des_runtime(file)` loads one local `.py` file with the generic
extension loader (in-process Python only, D-139) and adapts its `decide(state)` into a
`PolicyRuntime`. A broken decision falls back to the runtime's fallback rule and the reason
is kept in the decision trace.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from twinflow.engines.des import DesStateObserver
from twinflow.extensions.dispatch import PolicyFileError, des_runtime
from twinflow.extensions.kinds import KINDS
from twinflow.extensions.loader import Extension, load_file, module_name
from twinflow.model import load_model
from twinflow.plan.driver import RunDriver, RunResult
from twinflow.plan.loader import load_plan
from twinflow.policy import DISPATCH_NAMES, PolicyRuntime
from twinflow.state.recorder import read_moves

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "cnc-shop"

GOOD = '''
"""Use earliest due date when work piles up, else first in first out."""

NAME = "busy_edd"
DECISION = "dispatch"
SEEN = []


def decide(state):
    SEEN.append(state)
    return "edd" if len(state.queue) > 1 else "fifo"
'''


def _file(folder: Path, body: str, name: str = "rule.py") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _run(tmp_path: Path, runtime: PolicyRuntime, *observers: DesStateObserver) -> RunResult:
    model = load_model(str(EXAMPLE / "model.yaml"))
    plan = load_plan(str(EXAMPLE / "plan.csv"), model.registry)
    return RunDriver(model).run(
        plan,
        seed=0,
        replication_index=0,
        artifact_dir=tmp_path / "runs",
        decision_runtime=runtime,
        observers=observers,
    )


def test_dispatch_file_drives_real_decisions(tmp_path: Path) -> None:
    runtime = des_runtime(_file(tmp_path / "policies", GOOD))
    _run(tmp_path, runtime)
    trace = runtime.trace
    assert trace and all(record.fallback_reason is None for record in trace)
    assert {record.action.policy for record in trace} <= {"edd", "fifo"}
    assert runtime.policy_artifact.startswith("policy_file:rule.py:sha256:")


def test_state_has_plain_names(tmp_path: Path) -> None:
    path = _file(tmp_path / "policies", GOOD)
    runtime = des_runtime(path)
    _run(tmp_path, runtime)
    fresh = load_file(path, KINDS["policy"])  # a fresh load: its own SEEN list
    assert isinstance(fresh, Extension) and fresh.target.__globals__["SEEN"] == []
    seen = runtime._policy.extension.target.__globals__["SEEN"]  # type: ignore[attr-defined]
    state = seen[0]
    assert state.location == runtime.trace[0].location_id
    assert state.actions == DISPATCH_NAMES
    assert state.time == runtime.trace[0].simulation_time
    assert state.current == "fifo"
    assert isinstance(state.queue, tuple) and state.queue
    assert set(state.queue[0]) == {
        "order_id",
        "thing",
        "qty",
        "due_date",
        "priority",
        "arrival_time",
    }
    for state, record in zip(seen, runtime.trace, strict=True):
        assert record.action.policy == ("edd" if len(state.queue) > 1 else "fifo")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("DECISION = 'dispatch'\ndef decide(state):\n    raise KeyError('due')\n", "KeyError"),
        (
            "DECISION = 'dispatch'\ndef decide(state):\n    return 'lifo'\n",
            "'lifo', which is not one of: fifo, edd, spt, critical_ratio",
        ),
    ],
)
def test_a_bad_decision_falls_back_and_is_recorded(tmp_path: Path, body: str, reason: str) -> None:
    runtime = des_runtime(_file(tmp_path / "policies", body), fallback="spt")
    result = _run(tmp_path, runtime)
    assert result.termination_reason == "natural_exhaustion"
    assert runtime.trace
    for record in runtime.trace:
        assert record.action.policy == "spt"
        assert record.fallback_reason is not None
        assert "rule.py" in record.fallback_reason and reason in record.fallback_reason


def test_decision_moves_follow_the_policy_file(tmp_path: Path) -> None:
    runtime = des_runtime(_file(tmp_path / "policies", GOOD))
    result = _run(tmp_path, runtime, DesStateObserver(every_hours=None, decisions=True))
    assert result.artifact_dir is not None
    moves = read_moves(result.artifact_dir)
    assert [(m.actor, m.action) for m in moves] == [
        (r.location_id, r.action.policy) for r in runtime.trace
    ]


@pytest.mark.parametrize(
    ("body", "words"),
    [
        ("def decide(state):\n    return 'fifo'\n", "DECISION"),
        ("DECISION = 'make_next'\ndef decide(state):\n    return 'fifo'\n", "make_next"),
        ("DECISION = 'dispatch'\n", "decide"),
        ("DECISION = 'dispatch'\ndecide = 3\n", "decide"),
        ("DECISION = 'dispatch'\ndef decide(state)\n    return 'fifo'\n", "line 2"),
        ("import no_such_module_here\n", "no_such_module_here"),
    ],
)
def test_a_broken_file_is_a_plain_error_naming_the_file(
    tmp_path: Path, body: str, words: str
) -> None:
    with pytest.raises(PolicyFileError) as caught:
        des_runtime(_file(tmp_path / "policies", body))
    message = str(caught.value)
    assert "rule.py" in message and words in message
    assert caught.value.fix


def test_missing_file_is_a_plain_error(tmp_path: Path) -> None:
    with pytest.raises(PolicyFileError, match="not found"):
        des_runtime(tmp_path / "nope.py")


def test_same_stem_in_two_folders_does_not_collide(tmp_path: Path) -> None:
    one = _file(
        tmp_path / "a", "DECISION = 'dispatch'\nWHO = 'a'\ndef decide(s):\n    return 'fifo'\n"
    )
    two = _file(
        tmp_path / "b", "DECISION = 'dispatch'\nWHO = 'b'\ndef decide(s):\n    return 'edd'\n"
    )
    assert module_name(one, "policy") != module_name(two, "policy")
    assert module_name(one, "policy").startswith("twinflow_user_policy_rule_")
    assert len(module_name(one, "policy").rsplit("_", 1)[1]) == 8
    loaded = [load_file(p, KINDS["policy"]) for p in (one, two)]
    assert [ext.target.__globals__["WHO"] for ext in loaded] == ["a", "b"]  # type: ignore[union-attr]


def test_the_policy_files_package_is_gone() -> None:
    assert importlib.util.find_spec("twinflow.policy_files") is None


def test_service_side_packages_never_load_extensions() -> None:
    code = (
        "import sys\n"
        "import twinflow.application.scenarios, twinflow.service, twinflow.mcp, twinflow.domain\n"
        "import twinflow.engines.des\n"
        "print(any(n.startswith('twinflow.extensions') for n in sys.modules))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a literal
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "False"
