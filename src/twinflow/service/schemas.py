"""Pydantic request bodies for the service API.

Response bodies are plain dicts built by `service.serialize` / `service.floor`, so only
the inbound side is modelled here — the boundary that must validate untrusted input.
`model` always names a discovered example (never an arbitrary path), so the API cannot
be steered into reading a file outside the models root.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    """Run one configuration for `reps` replications and return KPIs + intervals."""

    model: str
    reps: int = Field(default=20, ge=1, le=200)


class SweepRequest(BaseModel):
    """Run a declared lever grid; every point reported side by side."""

    model: str
    sweep: dict[str, list[Any]]
    reps: int = Field(default=20, ge=1, le=200)


class LeverDomainSpec(BaseModel):
    """One lever's search domain: an int range `{min,max,step}` or a `{choices}` set."""

    min: int | None = None
    max: int | None = None
    step: int = 1
    choices: list[Any] | None = None


class OptimizeRequest(BaseModel):
    """Search a lever space under a named objective with a named optimizer."""

    model: str
    space: dict[str, LeverDomainSpec]
    objective: str = "on_time_pct"
    optimizer: str = "hill_climb"
    budget: int = Field(default=12, ge=1, le=500)
    reps: int = Field(default=20, ge=1, le=200)
    seed: int = 0
