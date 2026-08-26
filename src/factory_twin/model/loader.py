"""COMP-014 ModelLoader — the ONLY importer of PyYAML in the system (D-001).

Reads one model.yaml with yaml.safe_load; produces an unvalidated parse tree.
Never resolves includes or external references.
"""

from __future__ import annotations

import yaml  # noqa: F401 — this module is intentionally the sole PyYAML importer.


class RawModel:
    """The unvalidated parse tree of one model.yaml."""


def load_raw_model(path: str) -> RawModel:
    raise NotImplementedError("T-023")
