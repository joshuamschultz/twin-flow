"""`python -m twinflow.service.serve` — run the twinflow API with uvicorn.

A thin runner so the front end (and `twinflow serve`) has one command to start the
local service. Requires the `api` extra (`pip install -e ".[api]"`).
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Start the API. Returns a process exit code (0 on clean shutdown)."""
    import uvicorn

    from twinflow.service.app import create_app
    from twinflow.service.settings import ServiceSettings
    from twinflow.service.signing import ServiceSigner

    parser = argparse.ArgumentParser(prog="twinflow-serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--models-root", default="examples")
    parser.add_argument("--workspace-root", default=".twinflow-workspace")
    args = parser.parse_args(argv)

    settings = ServiceSettings.from_env(host=args.host)
    # Bind-time rule (COMP-127): off loopback a signing key is required; refuse to start.
    ServiceSigner.require(
        settings.signing_key_file,
        workspace_root=Path(args.workspace_root),
        local_only=settings.local_only,
    )
    _configure_audit_log()
    uvicorn.run(
        create_app(args.models_root, workspace_root=args.workspace_root, settings=settings),
        host=args.host,
        port=args.port,
    )
    return 0


def _configure_audit_log() -> None:
    """Emit ``twinflow.audit`` records as one JSON line each on stderr (COMP-122)."""
    import logging

    from twinflow.service.audit import AuditJsonFormatter

    audit_log = logging.getLogger("twinflow.audit")
    handler = logging.StreamHandler()
    handler.setFormatter(AuditJsonFormatter())
    audit_log.addHandler(handler)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False


if __name__ == "__main__":
    raise SystemExit(main())
