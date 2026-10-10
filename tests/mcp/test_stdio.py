"""MCP over stdio against an in-process workspace; the REST proxy is gone (T-212, COMP-119).

Requirement: REQ-162. `python -m twinflow.mcp.server --workspace-root DIR [--models-root DIR]
[--caller-id NAME]` serves the same tool catalogue over stdio against a `Workspace` it opens
itself. The owner lock refuses a second process on the same root. `--url`,
the shared token variable and the `TwinflowClient` proxy are deleted.

Contract pinned here:

* tools/list over stdio equals `CATALOGUE` names, using the official SDK client
  (`mcp.Client(StdioServerParameters(...))`).
* No HTTP client is constructed: the server also starts and serves with `twinflow.sdk`
  made unimportable (`sys.modules["twinflow.sdk"] = None` before the module runs).
* A second process on a root the first one holds exits non-zero and its stderr names the
  owner lock ("already owned"), with no traceback.
* `--url` is rejected by argparse: exit 2, "unrecognized arguments: --url".
* Audit rows written by stdio calls carry `caller_id` = the `--caller-id` value, or
  `"local-stdio"` without the flag.

Also required of the implementer (not asserted here): remove the strict `xfail` marker on
`tests/test_boundaries.py::test_mcp_makes_no_outbound_calls_and_never_uses_the_sdk`; it
XPASSes (and so fails, being strict) once `server.py` stops importing `twinflow.sdk`.
"""

# ruff: noqa: E402 -- the agent extra is optional: skip before importing anything needing `mcp`.

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

import asyncio
import sqlite3
import subprocess
import sys
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import TypeVar

import mcp
from mcp import StdioServerParameters
from twinflow.mcp.tools import CATALOGUE

OLD_TOKEN_ENV = "TWINFLOW_" + "API_TOKEN"  # the deleted shared-token variable, never literal

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
SERVER_SOURCE = ROOT / "src" / "twinflow" / "mcp" / "server.py"
SESSION_TIMEOUT_SECONDS = 60.0
SUBPROCESS_TIMEOUT_SECONDS = 60
T = TypeVar("T")

# Runs `twinflow.mcp.server` as __main__ with the REST SDK made unimportable (the spy).
WITHOUT_SDK = (
    "import runpy, sys\n"
    "sys.modules['twinflow.sdk'] = None\n"
    "runpy.run_module('twinflow.mcp.server', run_name='__main__', alter_sys=True)\n"
)


def _server_args(workspace: Path, *extra: str) -> list[str]:
    return ["--workspace-root", str(workspace), "--models-root", str(EXAMPLES), *extra]


def _module_params(workspace: Path, *extra: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "twinflow.mcp.server", *_server_args(workspace, *extra)],
        cwd=str(ROOT),
    )


def _spied_params(workspace: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-c", WITHOUT_SDK, *_server_args(workspace)], cwd=str(ROOT)
    )


async def _list_tool_names(client: mcp.Client) -> list[str]:
    names: list[str] = []
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        names.extend(tool.name for tool in page.tools)
        cursor = page.next_cursor
        if not cursor:
            return names


def _run(body: Callable[[], Awaitable[T]]) -> T:
    """Run one client session body, bounded so a server that never answers fails the test."""

    async def bounded() -> T:
        async with asyncio.timeout(SESSION_TIMEOUT_SECONDS):
            return await body()

    return asyncio.run(bounded())


def _second_process(workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv: this interpreter, literal args
        [sys.executable, "-m", "twinflow.mcp.server", *_server_args(workspace)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        cwd=ROOT,
        check=False,
    )


def _audit_callers(workspace: Path, tool: str) -> list[str]:
    uri = f"file:{workspace / 'workspace.sqlite3'}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute(
            "SELECT caller_id FROM audit_events WHERE operation = 'tool_call' AND tool = ?",
            (tool,),
        ).fetchall()
    return [str(row[0]) for row in rows]


# --- the catalogue over stdio -------------------------------------------------------------------


def test_stdio_tools_list_equals_the_catalogue(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    async def body() -> list[str]:
        async with mcp.Client(_module_params(workspace), cache=None) as client:
            return await _list_tool_names(client)

    names = _run(body)

    assert sorted(names) == sorted(spec.name for spec in CATALOGUE)


def test_stdio_serves_tools_with_the_rest_sdk_unimportable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    async def body() -> tuple[list[str], bool]:
        async with mcp.Client(_spied_params(workspace), cache=None) as client:
            names = await _list_tool_names(client)
            result = await client.call_tool("describe_capabilities", {})
            return names, bool(result.is_error)

    names, is_error = _run(body)

    assert sorted(names) == sorted(spec.name for spec in CATALOGUE)
    assert is_error is False


def test_second_stdio_process_on_the_same_root_exits_with_the_owner_lock_message(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"

    async def body() -> subprocess.CompletedProcess[str]:
        async with mcp.Client(_module_params(workspace), cache=None) as client:
            await _list_tool_names(client)  # the first server has opened the workspace
            return await asyncio.to_thread(_second_process, workspace)

    second = _run(body)

    assert second.returncode != 0
    assert "already owned" in second.stderr, second.stderr[-2000:]
    assert "Traceback" not in second.stderr, second.stderr[-2000:]


# --- audit caller ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "expected"),
    [((), "local-stdio"), (("--caller-id", "ops-desk"), "ops-desk")],
    ids=["default", "flag"],
)
def test_stdio_calls_are_audited_with_the_stdio_caller_id(
    tmp_path: Path, extra: tuple[str, ...], expected: str
) -> None:
    workspace = tmp_path / "workspace"

    async def body() -> bool:
        async with mcp.Client(_module_params(workspace, *extra), cache=None) as client:
            result = await client.call_tool("describe_capabilities", {})
            return bool(result.is_error)

    is_error = _run(body)

    assert is_error is False
    assert _audit_callers(workspace, "describe_capabilities") == [expected]


# --- the REST proxy is gone -------------------------------------------------------------------


def test_url_flag_is_rejected_by_argparse(tmp_path: Path) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed argv: this interpreter, literal args
        [
            sys.executable,
            "-m",
            "twinflow.mcp.server",
            *_server_args(tmp_path / "workspace"),
            "--url",
            "http://127.0.0.1:8000",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        cwd=ROOT,
        check=False,
    )

    assert completed.returncode == 2, (completed.returncode, completed.stderr[-2000:])
    assert "unrecognized arguments: --url" in completed.stderr, completed.stderr[-2000:]


@pytest.mark.parametrize("needle", ["TwinflowClient", "--url", OLD_TOKEN_ENV])
def test_mcp_server_source_names_no_rest_proxy(needle: str) -> None:
    assert needle not in SERVER_SOURCE.read_text(encoding="utf-8")
