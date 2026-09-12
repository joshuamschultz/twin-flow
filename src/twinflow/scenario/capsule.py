"""Safe parsing, validation, migration, and compilation for ``.twin.yaml``.

The external representation is ordinary YAML so it is reviewable in source
control.  YAML is only parsed with ``safe_load``; expressions remain subject to
the model validator's existing sandbox.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from twinflow.model import CompiledModel, load_model, validate_model
from twinflow.plan.driver import RunDriver, RunResult
from twinflow.plan.loader import WorkOrder, load_plan

CAPSULE_SCHEMA_VERSION = "0.1"
MAX_CAPSULE_BYTES = 5 * 1024 * 1024
MAX_NESTING = 32
MAX_ENTITIES = 100_000
KNOWN_CAPABILITIES = frozenset({"manufacturing.basic"})
_REQUIRED = {
    "schema_version",
    "model",
    "snapshot",
    "experiment",
    "assumptions",
    "provenance",
    "required_capabilities",
}


@dataclass(frozen=True)
class ValidationIssue:
    """A reviewable validation result with a repair hint."""

    path: str
    message: str
    severity: str = "error"
    suggestion: str | None = None


class CapsuleValidationError(ValueError):
    """Raised when a capsule cannot be safely or semantically compiled."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        super().__init__(
            "scenario capsule validation failed: " + "; ".join(i.message for i in issues)
        )


@dataclass(frozen=True)
class CompiledScenario:
    """Existing execution contracts resolved from a capsule."""

    capsule: ScenarioCapsule
    model: CompiledModel
    plan: list[WorkOrder]


def _check_tree(value: object, depth: int = 0) -> int:
    if depth > MAX_NESTING:
        raise ValueError(f"capsule nesting exceeds {MAX_NESTING} levels")
    if isinstance(value, dict):
        return 1 + sum(
            _check_tree(k, depth + 1) + _check_tree(v, depth + 1) for k, v in value.items()
        )
    if isinstance(value, list):
        return 1 + sum(_check_tree(v, depth + 1) for v in value)
    return 1


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


@dataclass(frozen=True)
class ScenarioCapsule:
    """Portable value object with copy-on-write edits and branches.

    Input mappings are deep-copied at the boundary; callers should use
    :meth:`edit` and :meth:`branch` when constructing a new semantic value.
    """

    schema_version: str
    model: dict[str, Any]
    snapshot: dict[str, Any]
    experiment: dict[str, Any]
    assumptions: dict[str, Any] | list[Any]
    provenance: dict[str, Any]
    required_capabilities: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ScenarioCapsule:
        missing = sorted(_REQUIRED - set(data))
        if missing:
            raise CapsuleValidationError(
                [
                    ValidationIssue(
                        "$",
                        f"missing required field(s): {', '.join(missing)}",
                        suggestion="Add the required capsule sections",
                    )
                ]
            )
        unknown = sorted(set(data) - _REQUIRED)
        if unknown:
            raise CapsuleValidationError(
                [
                    ValidationIssue(
                        f"$.{unknown[0]}",
                        "unknown capsule field",
                        suggestion="Remove the unsupported field",
                    )
                ]
            )
        if data["schema_version"] != CAPSULE_SCHEMA_VERSION:
            raise CapsuleValidationError(
                [
                    ValidationIssue(
                        "$.schema_version", f"unsupported schema version {data['schema_version']!r}"
                    )
                ]
            )
        for key in ("model", "snapshot", "experiment", "provenance"):
            if not isinstance(data[key], dict):
                raise CapsuleValidationError([ValidationIssue(f"$.{key}", "must be an object")])
        caps = data["required_capabilities"]
        if not isinstance(caps, list) or not all(isinstance(item, str) and item for item in caps):
            raise CapsuleValidationError(
                [ValidationIssue("$.required_capabilities", "must be a list of non-empty strings")]
            )
        model = copy.deepcopy(data["model"])
        snapshot = copy.deepcopy(data["snapshot"])
        entities = _check_tree(
            {
                "model": model,
                "snapshot": snapshot,
                "experiment": data["experiment"],
                "assumptions": data["assumptions"],
                "provenance": data["provenance"],
            }
        )
        if entities > MAX_ENTITIES:
            raise ValueError(f"capsule contains more than {MAX_ENTITIES} values")
        return cls(
            str(data["schema_version"]),
            model,
            snapshot,
            copy.deepcopy(data["experiment"]),
            copy.deepcopy(data["assumptions"]),
            copy.deepcopy(data["provenance"]),
            tuple(caps),
        )

    @classmethod
    def load(cls, path: str | Path, *, max_bytes: int = MAX_CAPSULE_BYTES) -> ScenarioCapsule:
        raw = Path(path).read_bytes()
        if len(raw) > max_bytes:
            raise ValueError(f"capsule exceeds byte limit ({max_bytes})")
        _reject_yaml_aliases(raw)
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            raise ValueError("capsule must be a YAML mapping")
        return cls.from_dict(parsed)

    @classmethod
    def loads(cls, content: str, *, max_bytes: int = MAX_CAPSULE_BYTES) -> ScenarioCapsule:
        """Parse capsule text using the same bounded boundary as :meth:`load`."""
        raw = content.encode("utf-8")
        if len(raw) > max_bytes:
            raise ValueError(f"capsule exceeds byte limit ({max_bytes})")
        _reject_yaml_aliases(raw)
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            raise ValueError("capsule must be a YAML mapping")
        return cls.from_dict(parsed)

    @property
    def digest(self) -> str:
        """SHA-256 of canonical semantic content (formatting does not matter)."""
        return hashlib.sha256(_canonical(self.to_dict()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model": copy.deepcopy(self.model),
            "snapshot": copy.deepcopy(self.snapshot),
            "experiment": copy.deepcopy(self.experiment),
            "assumptions": copy.deepcopy(self.assumptions),
            "provenance": copy.deepcopy(self.provenance),
            "required_capabilities": list(self.required_capabilities),
        }

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(
            yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True), encoding="utf-8"
        )

    def validate(self, available_capabilities: set[str] | None = None) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        capabilities = (
            KNOWN_CAPABILITIES if available_capabilities is None else available_capabilities
        )
        for capability in self.required_capabilities:
            if capability not in capabilities:
                issues.append(
                    ValidationIssue(
                        "$.required_capabilities",
                        f"unsupported required capability: {capability}",
                        suggestion="Install the capability or remove it from the capsule",
                    )
                )
        for field in ("id", "as_of", "model_revision", "production_plan"):
            if field not in self.snapshot:
                issues.append(
                    ValidationIssue(f"$.snapshot.{field}", "missing required snapshot field")
                )
        reps = self.experiment.get("replications")
        if reps is not None and (isinstance(reps, bool) or not isinstance(reps, int) or reps < 1):
            issues.append(
                ValidationIssue("$.experiment.replications", "must be a positive integer")
            )
        with tempfile.TemporaryDirectory(prefix="twinflow-validate-") as folder:
            model_file = Path(folder) / "model.yaml"
            plan_file = Path(folder) / "plan.csv"
            model_file.write_text(yaml.safe_dump(self.model, sort_keys=False), encoding="utf-8")
            rows = self.snapshot.get("production_plan", [])
            if not isinstance(rows, list):
                issues.append(
                    ValidationIssue("$.snapshot.production_plan", "must be a list of row objects")
                )
                rows = []
            else:
                try:
                    _write_plan(rows, plan_file)
                except CapsuleValidationError as exc:
                    issues.extend(exc.issues)
            for issue in validate_model(str(model_file)):
                issues.append(
                    ValidationIssue(
                        f"$.model.{issue.path}",
                        issue.message,
                        suggestion="Repair the model field named in this issue",
                    )
                )
            if not any(issue.path.startswith("$.model.") for issue in issues):
                try:
                    compiled = load_model(str(model_file))
                    load_plan(str(plan_file), compiled.registry)
                except (OSError, ValueError) as exc:
                    issues.append(
                        ValidationIssue(
                            "$.snapshot.production_plan",
                            str(exc),
                            suggestion="Repair the production plan row",
                        )
                    )
        return issues

    def compile(self) -> CompiledScenario:
        issues = self.validate()
        if issues:
            raise CapsuleValidationError(issues)
        with tempfile.TemporaryDirectory(prefix="twinflow-capsule-") as folder:
            model_path = Path(folder) / "model.yaml"
            plan_path = Path(folder) / "plan.csv"
            model_path.write_text(yaml.safe_dump(self.model, sort_keys=False), encoding="utf-8")
            rows = self.snapshot.get("production_plan", [])
            if not isinstance(rows, list):
                raise CapsuleValidationError(
                    [ValidationIssue("$.snapshot.production_plan", "must be a list of row objects")]
                )
            _write_plan(rows, plan_path)
            compiled = load_model(str(model_path))
            plan = load_plan(str(plan_path), compiled.registry)
        return CompiledScenario(self, compiled, plan)

    def run(self, *, seed: int = 0, replication_index: int = 0) -> RunResult:
        compiled = self.compile()
        return RunDriver(compiled.model).run(
            compiled.plan, seed=seed, replication_index=replication_index
        )

    def edit(self, path: str, value: Any) -> ScenarioCapsule:
        """Return a new capsule after a bounded semantic JSON-pointer edit."""
        if not path.startswith("/") or ".." in path:
            raise ValueError("edit path must be an absolute JSON pointer")
        data = self.to_dict()
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path.strip("/").split("/")]
        if not parts or parts[0] not in data:
            raise ValueError(f"unknown edit path: {path}")
        target: Any = data
        for part in parts[:-1]:
            if isinstance(target, list):
                target = target[int(part)]
            elif isinstance(target, dict) and part in target:
                target = target[part]
            else:
                raise ValueError(f"unknown edit path: {path}")
        last = parts[-1]
        if isinstance(target, list):
            target[int(last)] = copy.deepcopy(value)
        elif isinstance(target, dict) and last in target:
            target[last] = copy.deepcopy(value)
        else:
            raise ValueError(f"unknown edit path: {path}")
        return ScenarioCapsule.from_dict(data)

    def branch(self, *, scenario_id: str | None = None) -> ScenarioCapsule:
        data = self.to_dict()
        data["provenance"]["parent_digest"] = self.digest
        if scenario_id is not None:
            data["experiment"]["id"] = scenario_id
        return ScenarioCapsule.from_dict(data)


def _reject_yaml_aliases(raw: bytes) -> None:
    """Reject anchors and aliases before safe_load can expand them."""
    try:
        tokens = yaml.scan(raw)
    except yaml.YAMLError:
        return
    for token in tokens:
        if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
            raise ValueError("YAML anchors and aliases are not allowed in capsules")


def _write_plan(rows: list[Any], path: Path) -> None:
    fields = ["work_order_id", "part", "qty", "start_date", "due_date"]
    optional = ["initial_wip_location", "initial_wip_qty", "initial_wip_remaining_time", "priority"]
    if rows:
        keys = {key for row in rows if isinstance(row, dict) for key in row}
        fields += [key for key in optional if key in keys]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            if not isinstance(row, dict):
                raise CapsuleValidationError(
                    [ValidationIssue("$.snapshot.production_plan", "each row must be an object")]
                )
            writer.writerow({key: row.get(key, "") for key in fields})


def import_legacy(
    model_path: str | Path,
    plan_path: str | Path,
    *,
    snapshot_id: str = "legacy-import",
    as_of: str | None = None,
) -> ScenarioCapsule:
    """Adapt an existing model.yaml + plan.csv pair into one capsule."""
    model_data = yaml.safe_load(Path(model_path).read_bytes())
    if not isinstance(model_data, dict):
        raise ValueError("legacy model must be a YAML mapping")
    with Path(plan_path).open(newline="", encoding="utf-8") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    for row in rows:
        for field in ("qty", "initial_wip_qty", "priority"):
            if row.get(field, "") != "":
                row[field] = int(row[field])
        if row.get("initial_wip_remaining_time", "") != "":
            row["initial_wip_remaining_time"] = float(row["initial_wip_remaining_time"])
    timestamp = as_of or datetime.now().astimezone().isoformat()
    return ScenarioCapsule(
        CAPSULE_SCHEMA_VERSION,
        model_data,
        {
            "id": snapshot_id,
            "as_of": timestamp,
            "model_revision": "legacy",
            "production_plan": rows,
        },
        {"id": snapshot_id, "replications": 1, "seed": 0},
        {"source": "legacy model + plan"},
        {
            "model": str(model_path),
            "plan": str(plan_path),
            "migration": "legacy-pair-to-capsule-v0.1",
        },
        ("manufacturing.basic",),
    )


def load(path: str | Path) -> ScenarioCapsule:
    return ScenarioCapsule.load(path)


def loads(content: str) -> ScenarioCapsule:
    """Parse a capsule from application supplied text."""
    return ScenarioCapsule.loads(content)


def schema() -> dict[str, Any]:
    """Return the published JSON-schema-shaped contract for agent inspection."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_REQUIRED),
        "properties": {
            "schema_version": {"const": CAPSULE_SCHEMA_VERSION},
            "model": {"type": "object"},
            "snapshot": {
                "type": "object",
                "required": ["id", "as_of", "model_revision", "production_plan"],
                "properties": {
                    "id": {"type": "string"},
                    "as_of": {"type": "string"},
                    "model_revision": {"type": "string"},
                    "production_plan": {"type": "array", "items": {"type": "object"}},
                },
            },
            "experiment": {
                "type": "object",
                "properties": {
                    "replications": {"type": "integer", "minimum": 1},
                    "seed": {"type": "integer"},
                },
            },
            "assumptions": {"type": ["object", "array"]},
            "provenance": {"type": "object"},
            "required_capabilities": {"type": "array", "items": {"type": "string"}},
        },
    }
