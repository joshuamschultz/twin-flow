"""HMLV benchmark instances and the D-083 quality gates (T-127, COMP-027, REQ-125).

A package so tests anywhere under `tests/scheduling/` import it as `from bench.gates import
...`: pytest's `prepend` import mode puts `tests/scheduling/` (the first directory without an
`__init__.py`) on `sys.path`, which also keeps `oracle` and `generators` importable here.
"""
