"""COMP-028 RunStamp — everything needed to reproduce a run exactly.

Engine version, resolved commit SHA, model file hash, plan file hash, base seed, Python
interpreter version, resolved dependency hash. Contains no secrets and no client data.

Lives at the package root (not report/) so plan/driver can stamp a run at start without
importing a higher layer. Pure: only hashlib/importlib.metadata/sys.
"""

from __future__ import annotations


class RunStamp:
    def __init__(self) -> None:
        raise NotImplementedError("T-043")
