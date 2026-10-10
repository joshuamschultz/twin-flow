"""Scheduling algorithms: one leaf module per method family (COMP-002, REQ-088, D-069).

The leaf rule: each module here exports `SOLVERS: tuple[SolverSpec, ...]` and is found by
`twinflow.scheduling.registry` with `pkgutil.iter_modules` over this package's path. No
module outside `algorithms/` imports a leaf, and this file imports none, so deleting one
leaf leaves the package importable and every other solver working. A leaf imports its
optional dependencies inside its factory or solver only; their absence raises
`seam.OptionalDependencyError` at build time, never at discovery. Methods that compose
others look them up by name through the registry at call time.
"""
