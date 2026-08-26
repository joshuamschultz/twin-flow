"""Layer 2 — config in, routing graph + BOM out. The only YAML/expression trust boundary."""

from __future__ import annotations


def load_model(path: str) -> object:
    """Public API: parse + compile + validate a model.yaml into a CompiledModel."""
    raise NotImplementedError("T-023/T-027")


def validate_model(compiled: object) -> list[object]:
    """Public API: run every validation check; empty list means the model may run."""
    raise NotImplementedError("T-029")
