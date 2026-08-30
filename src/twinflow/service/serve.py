"""`python -m twinflow.service.serve` — run the twinflow API with uvicorn.

A thin runner so the front end (and `twinflow serve`) has one command to start the
local service. Requires the `api` extra (`pip install -e ".[api]"`).
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    """Start the API. Returns a process exit code (0 on clean shutdown)."""
    import uvicorn

    from twinflow.service.app import create_app

    parser = argparse.ArgumentParser(prog="twinflow-serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--models-root", default="examples")
    args = parser.parse_args(argv)

    uvicorn.run(create_app(args.models_root), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
