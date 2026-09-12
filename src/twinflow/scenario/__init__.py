"""Portable, versioned scenario capsules.

The capsule is deliberately an adapter around the existing model and plan
contracts.  It does not introduce a second simulation engine.
"""

from twinflow.scenario.capsule import (
    CAPSULE_SCHEMA_VERSION,
    CapsuleValidationError,
    CompiledScenario,
    ScenarioCapsule,
    ValidationIssue,
    import_legacy,
    load,
    loads,
    schema,
)

__all__ = [
    "CAPSULE_SCHEMA_VERSION",
    "CapsuleValidationError",
    "CompiledScenario",
    "ScenarioCapsule",
    "ValidationIssue",
    "import_legacy",
    "load",
    "loads",
    "schema",
]
