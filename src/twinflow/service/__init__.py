"""twinflow.service — an optional local REST API over the engine + module surface.

The core engine never imports this package, so a plain install stays dependency-light
(`pip install -e ".[api]"` adds FastAPI/uvicorn). The API is the seam the React front
end drives: discover models, read a floor's graph and its tunable levers, list the
registered modules, and launch run / sweep / optimize jobs it then polls to completion.
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app() -> object:
    """Build the FastAPI application. Imported lazily so `import twinflow.service`
    never hard-requires FastAPI until the app is actually constructed."""
    from twinflow.service.app import create_app as _create_app

    return _create_app()
