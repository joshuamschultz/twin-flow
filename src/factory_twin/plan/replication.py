"""COMP-020 ReplicationRunner — N replications in parallel, one per process.

Confidence intervals across replications use a SciPy t-quantile. A paired analysis is a
CI on the difference, never two overlapping CIs.
"""

from __future__ import annotations


class ReplicationRunner:
    def __init__(self) -> None:
        raise NotImplementedError("T-039")
