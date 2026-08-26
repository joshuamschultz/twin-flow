"""COMP-029 Cli — thin argument parsing over validate/run/balance/report.

Holds no logic; every command delegates to a Layer 2/3/4 entry point.
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (`ftwin`). Returns a process exit code."""
    raise NotImplementedError("T-047")
