"""COMP-006 Stock — a material level pulled from by name; blocks when empty."""

from __future__ import annotations


class Stock:
    """pull/put over a material level; stamps material_ready_time; blocks, never negative."""

    def __init__(self) -> None:
        raise NotImplementedError("T-011")
