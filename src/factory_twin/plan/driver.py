"""COMP-019 RunDriver — executes one terminating replication.

Seeds initial WIP, releases work orders on their start dates subject to any WIP ceiling,
returns when the plan completes. Callable in-process with no subprocess and no re-parse.
"""

from __future__ import annotations


class RunResult:
    """{event_log_path, horizon, run_meta}."""


class RunDriver:
    def __init__(self) -> None:
        raise NotImplementedError("T-033")
