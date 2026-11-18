"""The ERP issue record shared by the digest gate and the snapshot validator (COMP-111, COMP-112).

An `ErpIssue` names what is wrong (`code`, see the SDD issue-code list), how bad it is
(`severity`: hard blocks a build, soft never does), where it is (`path`, a `$`-rooted JSON
path into the snapshot) and which ERP row caused it (`lineage`, when one record is to blame).
Messages describe the rule that failed; they never repeat the rejected input value.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from twinflow.erp.schema import Lineage, Severity
from twinflow.scheduling.model import Id, Text

__all__ = ["ErpIssue", "Severity"]


class ErpIssue(BaseModel):
    """One problem found in an ERP snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: Id
    severity: Severity
    path: Text
    lineage: Lineage | None = None
    message: Text
