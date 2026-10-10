"""MCP over stdio against an in-process workspace (COMP-119, D-116, REQ-162).

    python -m twinflow.mcp.server --workspace-root DIR [--models-root DIR] [--caller-id NAME]

Opens the `Workspace` itself (its owner lock refuses a second process on the same root) and
serves the same tool catalogue as `/mcp`, in process. The local OS user is the boundary, so
there is no token: every call runs as `--caller-id` (default `local-stdio`), is audited
under that id, and is rate limited like an HTTP caller. Without a signing key the service is
in local mode: builds use the built-in default planning profile, recorded as unsigned.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from twinflow.application.decision_tools import DecisionTools
from twinflow.application.planning import PlanningService
from twinflow.application.portfolio_port import load_portfolio_api
from twinflow.application.workspace import Workspace, WorkspaceOwned
from twinflow.mcp.guard import ToolGuard
from twinflow.mcp.spec import ToolServices
from twinflow.service.limits import CallerLimiter

STDIO_CALLER_ID = "local-stdio"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m twinflow.mcp.server",
        description="Serve the Twinflow MCP tools over stdio against a local workspace.",
    )
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--models-root", default=Path("examples"), type=Path)
    parser.add_argument("--caller-id", default=STDIO_CALLER_ID)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Serve until stdin closes; 1 with a one-line message when the workspace is owned."""
    args = _parser().parse_args(argv)
    try:
        from twinflow.mcp.tools import build_server
    except ModuleNotFoundError as exc:
        if exc.name is None or exc.name.split(".")[0] != "mcp":
            raise
        sys.stderr.write("twinflow-mcp: install twinflow[agent] to serve MCP tools\n")
        return 1
    try:
        workspace = Workspace(args.workspace_root, args.models_root)
    except WorkspaceOwned as exc:
        sys.stderr.write(f"twinflow-mcp: {exc}\n")
        return 1
    planning = PlanningService(workspace, portfolio=load_portfolio_api())
    try:
        services = ToolServices(
            workspace=workspace,
            decisions=DecisionTools(workspace),
            planning=planning,
            artifacts=None,
            models_root=args.models_root,
            local_mode=True,
        )
        guard = ToolGuard.local(
            workspace.repository, caller_id=args.caller_id, limiter=CallerLimiter()
        )
        build_server(services, guard).run()
    finally:
        planning.close()
        workspace.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
