# RM-02 review

The implementation was reviewed against the RM-02 requirements. It delegates
semantic model checks and execution to the existing `twinflow.model` and
`twinflow.plan` contracts, uses `yaml.safe_load`, bounds capsule bytes and
nested values, and computes a digest from canonical JSON without a digest field.

Validation evidence: `pytest tests/unit/test_scenario.py`, `ruff check
src/twinflow/scenario tests/unit/test_scenario.py`, and strict mypy for the
scenario module were run in the project virtual environment. A capsule imported
from `examples/spring` compiled and validated successfully.

Known limits are deliberate: archive (`.twin`) support, dimensional
normalization, rich calendars, and bounded execution budgets belong to later
roadmap work. The capsule has extension points for these fields and reports
unsupported required capabilities instead of silently ignoring them.

## Review fixes

The final boundary review added duplicate-key and malformed-YAML rejection,
pre-expansion node depth checks, scalar/timestamp/finite-value checks, bounded
seed and replication values, negative-index rejection, semantic validation for
edits, copy-isolation coverage for branches, and XLSX legacy-plan support via
the existing `load_plan` contract. The published JSON schema is loaded from
the package resource so `schema()` and the inspectable file cannot diverge.

Fresh evidence includes six focused scenario tests plus import/validation of
all eight runnable example model/plan pairs, Ruff, and strict mypy.
