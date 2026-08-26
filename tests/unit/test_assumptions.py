"""COMP-025 AssumptionsCollector + COMP-028 RunStamp — unit tests (T-042, red phase).

AssumptionsCollector (`report/assumptions.py`, Layer 4) derives the Assumptions block
by inspecting an already-BUILT `CompiledModel` passed IN — never by model/ or plan/
reaching up into report/ (structure.md: "report/ depends on instrumentation/, never
the reverse"; report may depend on model, never model on report). Committed API:

    AssumptionsCollector().collect(compiled_model: CompiledModel) -> list[Assumption]

Each returned `Assumption` carries `.field`, `.default_used`, `.why_absent` (SDD
COMP-025's `{field, default_used, why_absent}` shape). This test file commits the
`field` identifiers the T-043 implementer conforms to, one per named default in
tech.md's Assumptions row (D-027, D-040):

    "setup"                    -- setup zero, no changeover matrix declared
    "labor_pool"                -- one undifferentiated labor pool
    "machine_eligibility"       -- any machine eligible, no eligibility data
    "pull_rule.<location_id>"   -- arrival order used as sequence, no pull rule
                                    declared for THAT location (per-location, since
                                    PullRule.default_applied is a per-Location fact)

RunStamp (`run_stamp.py`, package root) records what reproduces a run exactly.
Committed API:

    RunStamp.create(model_path: str, plan_path: str, base_seed: int,
                     commit_sha: str | None = None) -> RunStamp

Fields: `.engine_version`, `.commit_sha`, `.model_hash`, `.plan_hash`, `.base_seed`,
`.python_version`, `.dependency_hash`; plus `.to_dict()` for `run_meta.json`. Hashes
are sha256 hex digests of file CONTENT, never a copy of the content itself.

RED-phase note: `AssumptionsCollector.__init__` and `RunStamp.__init__` both
currently `raise NotImplementedError("T-043")` with no constructor arguments, and
`RunStamp.create` does not exist yet. Both modules import cleanly today (structural
guard tests below prove it and PASS now); only calling the missing behavior fails,
and only because the real behavior does not exist — never ImportError or syntax.

Pure Layer 4 tests for AssumptionsCollector: a real `CompiledModel` built by the
real, already-working `LocationCompiler` (COMP-016, same pattern as
`tests/unit/test_compile.py`) over a minimal `locations` + `routing` YAML fragment
-- `LocationCompiler.compile()` reads only those two RawModel keys, so no other
model.yaml section is needed. No mocks; the model layer is exercised for real.

Pure stdlib tests for RunStamp: real temp files on `tmp_path`, real `hashlib.sha256`
expectations computed independently in the test, real `sys.version_info`. No mocks.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

from twinflow.model import CompiledModel, LaborPoolConfig
from twinflow.model.compile import LocationCompiler
from twinflow.model.expressions import ExpressionSandbox
from twinflow.model.loader import RawModel
from twinflow.primitives.part import PartTypeRegistry
from twinflow.report.assumptions import Assumption, AssumptionsCollector
from twinflow.run_stamp import RunStamp

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "twinflow"

# ---------------------------------------------------------------------------
# Shared model.yaml fragments. LocationCompiler.compile() only ever reads
# RawModel.locations and RawModel.routing (see model/compile.py), so a minimal
# two-key fragment is a real, exercised model.yaml, not a shortcut around it.
# ---------------------------------------------------------------------------

ONE_LOCATION_NO_BATCH_SIZE = """
locations:
  - name: cutter
    consumes:
      - thing: wire
        qty: 1
        uom: ft
    emits:
      - thing: blank
        qty: 10
        uom: piece
    setup_key: grp_cut
    time_model:
      kind: rate_based
      rate: 5
    machine: cutter_m
    labor_skill: cut_op

routing: []
"""

TWO_LOCATIONS_ONE_WITH_BATCH_SIZE = """
locations:
  - name: press
    consumes:
      - thing: raw_blank
        qty: 1
        uom: piece
    emits:
      - thing: good
        qty: 1
        uom: piece
    batch_size: "200 piece"
    setup_key: grp_press
    time_model:
      kind: rate_based
      rate: 12
    machine: press_m
    labor_skill: press_op

  - name: cutter
    consumes:
      - thing: wire
        qty: 1
        uom: ft
    emits:
      - thing: blank
        qty: 10
        uom: piece
    setup_key: grp_cut
    time_model:
      kind: rate_based
      rate: 5
    machine: cutter_m
    labor_skill: cut_op

routing: []
"""


def _compiled_model(model_yaml: str, labor_pools: list[LaborPoolConfig]) -> CompiledModel:
    """Build a real `CompiledModel` via the real `LocationCompiler`, matching
    `tests/unit/test_compile.py`'s already-established pattern. `PartTypeRegistry`
    is empty because `LocationCompiler.compile()` never reads it at compile time
    (only `Transform.apply()` does, at run time -- not exercised here)."""
    raw_model = RawModel(yaml.safe_load(model_yaml))
    sandbox = ExpressionSandbox(max_depth=10, max_length=200)
    registry = PartTypeRegistry({})
    result = LocationCompiler(registry, sandbox).compile(raw_model)
    return CompiledModel(
        locations=result.locations,
        routing=result.routing,
        bom=result.bom,
        labor_pools=labor_pools,
        registry=registry,
    )


def _one_labor_pool() -> LaborPoolConfig:
    return LaborPoolConfig(name="general", headcount=5, skills=frozenset({"cut_op", "press_op"}))


def _by_field(assumptions: list[Assumption], field: str) -> Assumption | None:
    matches = [a for a in assumptions if a.field == field]
    assert len(matches) <= 1, f"expected at most one Assumption for field {field!r}, got {matches}"
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Acceptance 1 — every substituted default is captured for a model that
# declares none of them.
# ---------------------------------------------------------------------------


class TestCollectReportsEverySubstitutedDefault:
    def test_setup_zero_reported_when_no_changeover_matrix_declared(self) -> None:
        compiled_model = _compiled_model(ONE_LOCATION_NO_BATCH_SIZE, [_one_labor_pool()])

        assumptions = AssumptionsCollector().collect(compiled_model)

        setup = _by_field(assumptions, "setup")
        assert setup is not None
        assert "changeover matrix" in setup.why_absent.lower()
        assert "0" in setup.default_used or "zero" in setup.default_used.lower()

    def test_one_undifferentiated_labor_pool_reported(self) -> None:
        compiled_model = _compiled_model(ONE_LOCATION_NO_BATCH_SIZE, [_one_labor_pool()])

        assumptions = AssumptionsCollector().collect(compiled_model)

        labor = _by_field(assumptions, "labor_pool")
        assert labor is not None
        assert "one" in labor.default_used.lower()
        assert "pool" in labor.default_used.lower()

    def test_any_machine_eligible_reported(self) -> None:
        compiled_model = _compiled_model(ONE_LOCATION_NO_BATCH_SIZE, [_one_labor_pool()])

        assumptions = AssumptionsCollector().collect(compiled_model)

        eligibility = _by_field(assumptions, "machine_eligibility")
        assert eligibility is not None
        assert "any machine" in eligibility.default_used.lower()
        assert "eligib" in eligibility.why_absent.lower()

    def test_arrival_order_used_as_sequence_reported_for_undeclared_pull_rule(self) -> None:
        compiled_model = _compiled_model(ONE_LOCATION_NO_BATCH_SIZE, [_one_labor_pool()])

        assumptions = AssumptionsCollector().collect(compiled_model)

        pull = _by_field(assumptions, "pull_rule.cutter")
        assert pull is not None
        assert "arrival order" in pull.default_used.lower()
        assert "pull rule" in pull.why_absent.lower()

    def test_every_returned_item_carries_the_full_assumption_shape(self) -> None:
        """{field, default_used, why_absent} — every item, not just the ones the
        other tests happen to check by name."""
        compiled_model = _compiled_model(ONE_LOCATION_NO_BATCH_SIZE, [_one_labor_pool()])

        assumptions = AssumptionsCollector().collect(compiled_model)

        assert len(assumptions) >= 4
        for assumption in assumptions:
            assert isinstance(assumption.field, str) and assumption.field
            assert isinstance(assumption.default_used, str) and assumption.default_used
            assert isinstance(assumption.why_absent, str) and assumption.why_absent


# ---------------------------------------------------------------------------
# Acceptance 2 — a declared default must NOT be reported as substituted.
# ---------------------------------------------------------------------------


class TestCollectOmitsDeclaredDefaults:
    def test_declared_batch_size_suppresses_arrival_order_default_for_that_location(
        self,
    ) -> None:
        compiled_model = _compiled_model(TWO_LOCATIONS_ONE_WITH_BATCH_SIZE, [_one_labor_pool()])

        assumptions = AssumptionsCollector().collect(compiled_model)

        assert _by_field(assumptions, "pull_rule.press") is None

    def test_undeclared_sibling_location_still_reports_its_own_default(self) -> None:
        """Proves omission is per-location, not a blanket suppression once ANY
        location declares a batch_size — a lazy 'declared anywhere -> suppress
        everywhere' implementation must fail this."""
        compiled_model = _compiled_model(TWO_LOCATIONS_ONE_WITH_BATCH_SIZE, [_one_labor_pool()])

        assumptions = AssumptionsCollector().collect(compiled_model)

        assert _by_field(assumptions, "pull_rule.cutter") is not None

    def test_multiple_declared_labor_pools_suppress_the_undifferentiated_default(self) -> None:
        """A model with more than one distinct labor pool has DECLARED a
        differentiated workforce; the 'one undifferentiated pool' default must
        not be claimed."""
        pools = [
            LaborPoolConfig(name="cutters", headcount=3, skills=frozenset({"cut_op"})),
            LaborPoolConfig(name="press_ops", headcount=2, skills=frozenset({"press_op"})),
        ]
        compiled_model = _compiled_model(TWO_LOCATIONS_ONE_WITH_BATCH_SIZE, pools)

        assumptions = AssumptionsCollector().collect(compiled_model)

        assert _by_field(assumptions, "labor_pool") is None


# ---------------------------------------------------------------------------
# Adversarial — edge the acceptance criteria didn't spell out.
# ---------------------------------------------------------------------------


class TestCollectAdversarialEdgeCases:
    def test_no_locations_reports_no_pull_rule_assumptions_and_does_not_crash(self) -> None:
        compiled_model = CompiledModel(
            locations=[],
            routing={},
            bom={},
            labor_pools=[_one_labor_pool()],
            registry=PartTypeRegistry({}),
        )

        assumptions = AssumptionsCollector().collect(compiled_model)

        assert not any(a.field.startswith("pull_rule.") for a in assumptions)

    def test_collect_is_side_effect_free_and_repeatable(self) -> None:
        """Calling collect() twice on the same compiled model must not mutate it
        or double-report; a report can legitimately be regenerated."""
        compiled_model = _compiled_model(ONE_LOCATION_NO_BATCH_SIZE, [_one_labor_pool()])
        collector = AssumptionsCollector()

        first = collector.collect(compiled_model)
        second = collector.collect(compiled_model)

        assert [(a.field, a.default_used, a.why_absent) for a in first] == [
            (a.field, a.default_used, a.why_absent) for a in second
        ]


class TestReportModuleBoundary:
    """Structural guard, not a T-043 behavior test -- expected to PASS today
    and stay passing, proving the boundary this task's contract depends on."""

    def test_model_and_plan_source_never_imports_report(self) -> None:
        offending: list[str] = []
        for layer in ("model", "plan"):
            for py_file in (SRC_ROOT / layer).rglob("*.py"):
                for name in _imported_module_names(py_file):
                    if name == "twinflow.report" or name.startswith("twinflow.report."):
                        offending.append(f"{py_file}: {name}")
        assert offending == []


def _imported_module_names(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ===========================================================================
# COMP-028 RunStamp
# ===========================================================================


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _sha256_of(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


EXPECTED_PYTHON_VERSION = (
    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
)


# ---------------------------------------------------------------------------
# Acceptance 3 — the stamp records every reproducibility field; hashes are
# real content hashes (equal for identical content, different after a change).
# ---------------------------------------------------------------------------


class TestRunStampRecordsReproducibilityFields:
    def test_create_records_engine_version_seed_hashes_and_interpreter_version(
        self, tmp_path: Path
    ) -> None:
        model_content = "locations: []\nrouting: []\n"
        plan_content = "part,qty,due_date\nwidget,10,2026-09-01\n"
        model_path = _write(tmp_path, "model.yaml", model_content)
        plan_path = _write(tmp_path, "plan.csv", plan_content)

        stamp = RunStamp.create(
            str(model_path), str(plan_path), base_seed=42, commit_sha="a1b2c3d4"
        )

        import twinflow

        assert stamp.engine_version == twinflow.__version__
        assert stamp.commit_sha == "a1b2c3d4"
        assert stamp.model_hash == _sha256_of(model_content)
        assert stamp.plan_hash == _sha256_of(plan_content)
        assert stamp.model_hash != stamp.plan_hash
        assert stamp.base_seed == 42
        assert stamp.python_version == EXPECTED_PYTHON_VERSION
        assert isinstance(stamp.dependency_hash, str)
        assert len(stamp.dependency_hash) == 64  # sha256 hex digest length
        int(stamp.dependency_hash, 16)  # must be valid hex

    def test_commit_sha_defaults_to_none_when_not_provided(self, tmp_path: Path) -> None:
        model_path = _write(tmp_path, "model.yaml", "locations: []\nrouting: []\n")
        plan_path = _write(tmp_path, "plan.csv", "part,qty\n")

        stamp = RunStamp.create(str(model_path), str(plan_path), base_seed=1)

        assert stamp.commit_sha is None

    def test_identical_model_files_hash_equal(self, tmp_path: Path) -> None:
        content = "locations:\n  - name: cutter\nrouting: []\n"
        model_path_a = _write(tmp_path, "model_a.yaml", content)
        model_path_b = _write(tmp_path, "model_b.yaml", content)
        plan_path = _write(tmp_path, "plan.csv", "part,qty\n")

        stamp_a = RunStamp.create(str(model_path_a), str(plan_path), base_seed=7)
        stamp_b = RunStamp.create(str(model_path_b), str(plan_path), base_seed=7)

        assert stamp_a.model_hash == stamp_b.model_hash

    def test_changed_model_file_hashes_different(self, tmp_path: Path) -> None:
        plan_path = _write(tmp_path, "plan.csv", "part,qty\n")
        model_path = _write(tmp_path, "model.yaml", "locations: []\nrouting: []\n")
        stamp_before = RunStamp.create(str(model_path), str(plan_path), base_seed=1)

        model_path.write_text("locations:\n  - name: press\nrouting: []\n", encoding="utf-8")
        stamp_after = RunStamp.create(str(model_path), str(plan_path), base_seed=1)

        assert stamp_before.model_hash != stamp_after.model_hash

    def test_dependency_hash_is_deterministic_across_calls(self, tmp_path: Path) -> None:
        plan_path = _write(tmp_path, "plan.csv", "part,qty\n")
        model_path_a = _write(tmp_path, "model_a.yaml", "locations: []\nrouting: []\n")
        model_path_b = _write(tmp_path, "model_b.yaml", "locations: []\nrouting: []\n")

        stamp_a = RunStamp.create(str(model_path_a), str(plan_path), base_seed=1)
        stamp_b = RunStamp.create(str(model_path_b), str(plan_path), base_seed=2)

        assert stamp_a.dependency_hash == stamp_b.dependency_hash


# ---------------------------------------------------------------------------
# Acceptance 4 — no secret, no copy of client data, anywhere in the stamp.
# ---------------------------------------------------------------------------


class TestRunStampContainsNoSecretsOrClientData:
    def test_to_dict_never_contains_the_raw_model_file_content(self, tmp_path: Path) -> None:
        marker = "ACME_CORP_CONFIDENTIAL_ROUTING_7f3a9c2b1e"
        model_content = f"locations: []\nrouting: []\n# {marker}\n"
        model_path = _write(tmp_path, "model.yaml", model_content)
        plan_path = _write(tmp_path, "plan.csv", "part,qty\nwidget,10\n")

        stamp = RunStamp.create(str(model_path), str(plan_path), base_seed=1)

        serialized = json.dumps(stamp.to_dict())
        assert marker not in serialized

    def test_to_dict_never_contains_the_raw_plan_file_content(self, tmp_path: Path) -> None:
        marker = "CLIENT_ORDER_NUMBER_PO-99182-CONFIDENTIAL"
        plan_content = f"part,qty\n{marker},10\n"
        model_path = _write(tmp_path, "model.yaml", "locations: []\nrouting: []\n")
        plan_path = _write(tmp_path, "plan.csv", plan_content)

        stamp = RunStamp.create(str(model_path), str(plan_path), base_seed=1)

        serialized = json.dumps(stamp.to_dict())
        assert marker not in serialized

    def test_to_dict_is_json_serializable_with_exactly_the_reproducibility_fields(
        self, tmp_path: Path
    ) -> None:
        model_path = _write(tmp_path, "model.yaml", "locations: []\nrouting: []\n")
        plan_path = _write(tmp_path, "plan.csv", "part,qty\n")

        stamp = RunStamp.create(str(model_path), str(plan_path), base_seed=5, commit_sha="deadbeef")
        payload = stamp.to_dict()

        # Round-trips through JSON (run_meta.json's actual format), and every
        # value is itself a JSON scalar -- never a nested blob of file content.
        round_tripped = json.loads(json.dumps(payload))
        expected_keys = {
            "engine_version",
            "commit_sha",
            "model_hash",
            "plan_hash",
            "base_seed",
            "python_version",
            "dependency_hash",
        }
        assert expected_keys <= set(round_tripped)
        for key in expected_keys:
            assert isinstance(round_tripped[key], (str, int, type(None)))


# ---------------------------------------------------------------------------
# Adversarial — RunStamp
# ---------------------------------------------------------------------------


class TestRunStampAdversarialEdgeCases:
    def test_create_raises_when_model_path_does_not_exist(self, tmp_path: Path) -> None:
        plan_path = _write(tmp_path, "plan.csv", "part,qty\n")
        missing_model_path = tmp_path / "does_not_exist.yaml"

        with pytest.raises(OSError):
            RunStamp.create(str(missing_model_path), str(plan_path), base_seed=1)

    def test_create_raises_when_plan_path_does_not_exist(self, tmp_path: Path) -> None:
        model_path = _write(tmp_path, "model.yaml", "locations: []\nrouting: []\n")
        missing_plan_path = tmp_path / "does_not_exist.csv"

        with pytest.raises(OSError):
            RunStamp.create(str(model_path), str(missing_plan_path), base_seed=1)


class TestRunStampModuleBoundary:
    """Structural guard, not a T-043 behavior test -- expected to PASS today
    and stay passing: run_stamp.py stays stdlib-only (plus twinflow's own
    __version__), never importing report/instrumentation/model/plan, so a
    plan-layer driver can stamp a run without importing a higher layer."""

    def test_run_stamp_module_imports_only_stdlib_and_its_own_package(self) -> None:
        py_file = SRC_ROOT / "run_stamp.py"
        imports = _imported_module_names(py_file)
        forbidden_prefixes = (
            "twinflow.report",
            "twinflow.instrumentation",
            "twinflow.model",
            "twinflow.plan",
        )
        offending = [
            name
            for name in imports
            if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden_prefixes)
        ]
        assert offending == []
