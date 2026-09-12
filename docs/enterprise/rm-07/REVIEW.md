# RM-07 review

The office adapter is isolated under `twinflow.domain.office`; it has no
imports from service, persistence, connectors, or an agent framework. The
registry is explicit and transport-neutral. Validation runs before SimPy and
names missing prerequisites, cycles, role capacity, required data, document
revision, and approval paths.

Focused tests cover fork/join timing, finite role capacity, stale documents,
missing approvals and data, rework bounds, artifacts, and registry discovery.
Ruff, strict mypy, and the focused pytest suite are required gates.

Limitations: calendar windows are finite absolute windows rather than named
timezone calendars; no material or external-response semantics are included;
random seeds are accepted for the stable adapter contract but current task
durations are deterministic; richer case revisions and role qualification
history belong to later domain work.
