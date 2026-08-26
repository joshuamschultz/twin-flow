"""COMP-025 AssumptionsCollector — every default the engine actually substituted, named.

Collects default-applied events raised during compile and run (setup zero, one
undifferentiated labor pool, any machine eligible, arrival order as sequence) so the
report can name them. Rendered at the top of the report.

Boundary note (resolve in T-043): collection happens in model/ (compile) and plan/ (run),
which may not import report/. The Assumption record and the collector object must live
somewhere both layers can import, or defaults must be recorded as plain data the report
wraps. Placement is an Integration-phase decision.
"""

from __future__ import annotations


class Assumption:
    """{field, default_used, why_absent}."""


class AssumptionsCollector:
    def __init__(self) -> None:
        raise NotImplementedError("T-043")
