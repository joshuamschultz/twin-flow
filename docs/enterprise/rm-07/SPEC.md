# RM-07 standalone office domain

The office profile models cases, tasks, finite role resources, documents, and
explicit approvals. Tasks form an acyclic prerequisite graph, so independent
tasks execute in parallel and join tasks wait for every prerequisite. A task
can require case data, an exact document revision, and an approval by a named
role. Missing facts, stale documents, and missing approvals remain explicit
incomplete outcomes; the evaluator never invents them.

`DomainAdapter` is the transport-neutral seam shared by future capsule and
application layers. The built-in `office_adapter` exposes `validate`,
`describe`, and bounded `evaluate`. The evaluator uses SimPy, emits waiting
reasons and task traces, and optionally retains a JSON artifact.

Acceptance covers parallel fork/join, constrained role capacity, calendars,
bounded rework, stale document and missing approval/data gates, deterministic
case outcomes, and a model with no manufacturing entities.
