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
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml

from twinflow.domain import registry as domain_registry
from twinflow.model import CompiledModel, load_model, validate_model
from twinflow.model.loader import load_raw_model
from twinflow.plan.driver import RunDriver, RunResult
from twinflow.plan.loader import WorkOrder, load_plan

CAPSULE_SCHEMA_VERSION = "0.1"
MAX_CAPSULE_BYTES = 5 * 1024 * 1024
MAX_NESTING = 32
MAX_ENTITIES = 100_000
KNOWN_CAPABILITIES = frozenset({"manufacturing.basic", "office.basic"})
MAX_REPLICATIONS = 10_000
MAX_SEED = 2**63 - 1
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
        if not isinstance(data, Mapping):
            raise CapsuleValidationError([ValidationIssue("$", "capsule must be an object")])
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
        if not isinstance(data["assumptions"], (dict, list)):
            raise CapsuleValidationError(
                [ValidationIssue("$.assumptions", "must be an object or list")]
            )
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
        for field in ("id", "as_of", "model_revision"):
            if field in snapshot and not isinstance(snapshot[field], str):
                raise CapsuleValidationError(
                    [ValidationIssue(f"$.snapshot.{field}", "must be a string")]
                )
        if "as_of" in snapshot:
            try:
                parsed_as_of = datetime.fromisoformat(snapshot["as_of"])
            except ValueError as exc:
                raise CapsuleValidationError(
                    [ValidationIssue("$.snapshot.as_of", "must be an ISO-8601 timestamp")]
                ) from exc
            if parsed_as_of.tzinfo is None:
                raise CapsuleValidationError(
                    [ValidationIssue("$.snapshot.as_of", "must include a timezone")]
                )
        try:
            _canonical(data)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CapsuleValidationError(
                [ValidationIssue("$", f"capsule contains non-JSON value: {exc}")]
            ) from exc
        reps = data["experiment"].get("replications")
        if reps is not None and (
            isinstance(reps, bool) or not isinstance(reps, int) or not 1 <= reps <= MAX_REPLICATIONS
        ):
            raise CapsuleValidationError(
                [
                    ValidationIssue(
                        "$.experiment.replications",
                        f"must be an integer from 1 to {MAX_REPLICATIONS}",
                    )
                ]
            )
        seed = data["experiment"].get("seed")
        if seed is not None and (
            isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED
        ):
            raise CapsuleValidationError(
                [ValidationIssue("$.experiment.seed", f"must be an integer from 0 to {MAX_SEED}")]
            )
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
        parsed = _parse_yaml(raw)
        if not isinstance(parsed, dict):
            raise ValueError("capsule must be a YAML mapping")
        return cls.from_dict(parsed)

    @classmethod
    def loads(cls, content: str, *, max_bytes: int = MAX_CAPSULE_BYTES) -> ScenarioCapsule:
        """Parse capsule text using the same bounded boundary as :meth:`load`."""
        raw = content.encode("utf-8")
        if len(raw) > max_bytes:
            raise ValueError(f"capsule exceeds byte limit ({max_bytes})")
        parsed = _parse_yaml(raw)
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
        snapshot_fields: tuple[str, ...] = ("id", "as_of", "model_revision")
        if self.model.get("domain") == "office":
            snapshot_fields += ("cases",)
        else:
            snapshot_fields += ("production_plan",)
        for field in snapshot_fields:
            if field not in self.snapshot:
                issues.append(
                    ValidationIssue(f"$.snapshot.{field}", "missing required snapshot field")
                )
        reps = self.experiment.get("replications")
        if reps is not None and (
            isinstance(reps, bool) or not isinstance(reps, int) or not 1 <= reps <= MAX_REPLICATIONS
        ):
            issues.append(
                ValidationIssue(
                    "$.experiment.replications", f"must be an integer from 1 to {MAX_REPLICATIONS}"
                )
            )
        seed = self.experiment.get("seed")
        if seed is not None and (
            isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED
        ):
            issues.append(
                ValidationIssue("$.experiment.seed", f"must be an integer from 0 to {MAX_SEED}")
            )
        with tempfile.TemporaryDirectory(prefix="twinflow-validate-") as folder:
            if self.model.get("domain") == "office":
                for domain_issue in domain_registry.validate("office", self.model, self.snapshot):
                    issues.append(
                        ValidationIssue(
                            domain_issue.path,
                            domain_issue.message,
                            domain_issue.severity,
                            domain_issue.suggestion,
                        )
                    )
                return issues
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

    def evaluate(
        self,
        *,
        seed: int = 0,
        artifact_dir: str | None = None,
        limits: Mapping[str, int | float] | None = None,
    ) -> dict[str, object] | RunResult:
        """Evaluate a registered domain; manufacturing retains its RunResult API."""
        domain = self.model.get("domain")
        if isinstance(domain, str) and domain != "manufacturing":
            return domain_registry.evaluate(
                domain,
                self.model,
                self.snapshot,
                seed=seed,
                artifact_dir=artifact_dir,
                limits=limits,
            )
        return self.run(seed=seed)

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
                index = int(part)
                if index < 0 or index >= len(target):
                    raise ValueError(f"unknown edit path: {path}")
                target = target[index]
            elif isinstance(target, dict) and part in target:
                target = target[part]
            else:
                raise ValueError(f"unknown edit path: {path}")
        last = parts[-1]
        if isinstance(target, list):
            index = int(last)
            if index < 0 or index >= len(target):
                raise ValueError(f"unknown edit path: {path}")
            target[index] = copy.deepcopy(value)
        elif isinstance(target, dict) and last in target:
            target[last] = copy.deepcopy(value)
        else:
            raise ValueError(f"unknown edit path: {path}")
        edited = ScenarioCapsule.from_dict(data)
        issues = edited.validate()
        if issues:
            raise CapsuleValidationError(issues)
        return edited

    def branch(self, *, scenario_id: str | None = None) -> ScenarioCapsule:
        data = self.to_dict()
        data["provenance"]["parent_digest"] = self.digest
        if scenario_id is not None:
            data["experiment"]["id"] = scenario_id
        return ScenarioCapsule.from_dict(data)


def _reject_yaml_aliases(raw: bytes) -> None:
    """Reject anchors and aliases before safe_load can expand them."""
    tokens = yaml.scan(raw)
    for token in tokens:
        if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
            raise ValueError("YAML anchors and aliases are not allowed in capsules")


class _UniqueSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys."""


def _unique_mapping(
    loader: _UniqueSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _parse_yaml(raw: bytes) -> object:
    """Parse YAML after token/node checks, with stable boundary exceptions."""
    try:
        _reject_yaml_aliases(raw)
        node = yaml.compose(raw)
        if node is not None:
            _check_yaml_node(node)
        return yaml.load(raw, Loader=_UniqueSafeLoader)  # noqa: S506 -- SafeLoader subclass
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"invalid capsule YAML: {exc}") from exc


def _check_yaml_node(node: yaml.Node, depth: int = 0) -> int:
    if depth > MAX_NESTING:
        raise ValueError(f"capsule nesting exceeds {MAX_NESTING} levels")
    if isinstance(node, yaml.MappingNode):
        keys: set[str] = set()
        total = 1
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode) and key.value in keys:
                raise ValueError(f"duplicate YAML key: {key.value!r}")
            if isinstance(key, yaml.ScalarNode):
                keys.add(key.value)
            total += _check_yaml_node(key, depth + 1) + _check_yaml_node(value, depth + 1)
        return total
    if isinstance(node, yaml.SequenceNode):
        return 1 + sum(_check_yaml_node(value, depth + 1) for value in node.value)
    return 1


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
    """Adapt an existing model.yaml plus CSV or XLSX plan into one capsule."""
    compiled_model = load_model(str(model_path))
    model_data = load_raw_model(str(model_path)).data
    work_orders = load_plan(str(plan_path), compiled_model.registry)
    rows = [asdict(order) for order in work_orders]
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
    return cast(
        dict[str, Any],
        json.loads(Path(__file__).with_name("schema.json").read_text(encoding="utf-8")),
    )
