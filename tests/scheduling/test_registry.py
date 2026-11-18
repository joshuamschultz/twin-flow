"""Solver registry tests (T-118, COMP-002, REQ-088, D-069).

The registry discovers every leaf module of `scheduling/algorithms/` with
`pkgutil.iter_modules`; each contributes `SOLVERS: tuple[SolverSpec, ...]`. `baseline`
registers from core. Discovery behavior with planted modules runs in a subprocess over a
copy of the source tree, so the real tree and this interpreter's module cache stay clean.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from twinflow.scheduling import registry
from twinflow.scheduling.baseline import CAPABILITIES as BASELINE_CAPABILITIES
from twinflow.scheduling.kernel.dispatch import Dispatched, DispatchEngine
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import SolveRequest
from twinflow.scheduling.registry import (
    RuleSpec,
    SolverInfo,
    SolverSpec,
    UnknownRuleError,
    UnknownSolverError,
)
from twinflow.scheduling.seam import Feature
from twinflow.scheduling.solver import solve

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "twinflow"
FIXTURES = Path(__file__).parent / "fixtures"
ORTOOLS_MODULES = ("ortools", "ortools.sat", "ortools.sat.python", "ortools.sat.python.cp_model")
# The CP-SAT v3 capability set: every feature but pause (COMP-016, D-075, T-136, T-137).
ORTOOLS_CAPABILITIES = frozenset(
    {
        Feature.ALTERNATIVES,
        Feature.SETUPS,
        Feature.DETACHED_SETUP,
        Feature.CALENDARS,
        Feature.OPERATORS,
        Feature.BATCHING,
        Feature.RESTART,
        Feature.LAGS,
        Feature.MAX_LAGS,
        Feature.OVERLAP,
        Feature.PINS,
        Feature.IN_PROGRESS,
        Feature.MACHINE_STATE,
    }
)


def _info(name: str) -> SolverInfo:
    matches = [info for info in registry.available() if info.name == name]
    assert len(matches) == 1, [info.name for info in registry.available()]
    return matches[0]


def _fixture(name: str) -> SolveRequest:
    return SolveRequest.model_validate_json((FIXTURES / name).read_text())


# --- available ------------------------------------------------------------------------------


def test_available_lists_baseline_from_core_with_its_capabilities() -> None:
    info = _info("baseline")

    assert info.family == "baseline"
    assert info.capabilities == BASELINE_CAPABILITIES
    assert info.module == "twinflow.scheduling.baseline"
    assert info.deterministic is True


def test_available_lists_ortools_from_the_algorithms_package() -> None:
    info = _info("ortools")

    assert info.family == "exact"
    assert info.capabilities == ORTOOLS_CAPABILITIES
    assert info.module == "twinflow.scheduling.algorithms.cpsat"
    assert isinstance(info.deterministic, bool)


def test_available_is_sorted_by_name_with_unique_names() -> None:
    names = [info.name for info in registry.available()]

    assert names == sorted(names)
    assert len(names) == len(set(names))
    assert {"baseline", "ortools"} <= set(names)


def test_available_returns_a_tuple_of_frozen_infos() -> None:
    infos = registry.available()

    assert isinstance(infos, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        infos[0].name = "renamed"  # type: ignore[misc]  # proving the dataclass is frozen


def test_solver_info_fields() -> None:
    names = {field.name for field in dataclasses.fields(SolverInfo)}

    assert names == {"name", "family", "capabilities", "module", "deterministic"}


def test_ortools_capabilities_are_declared_without_building_the_solver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module in ORTOOLS_MODULES:
        monkeypatch.setitem(sys.modules, module, None)

    assert _info("ortools").capabilities == ORTOOLS_CAPABILITIES


# --- SolverSpec -----------------------------------------------------------------------------


def test_solver_spec_has_name_family_factory_deterministic_and_capabilities() -> None:
    names = {field.name for field in dataclasses.fields(SolverSpec)}

    assert names == {"name", "family", "factory", "deterministic", "capabilities"}


def test_solver_spec_is_frozen() -> None:
    spec = SolverSpec(
        name="probe",
        family="test",
        factory=lambda: None,  # type: ignore[arg-type,return-value]  # never called
        deterministic=True,
        capabilities=frozenset(),
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "other"  # type: ignore[misc]  # proving the dataclass is frozen


def test_cpsat_module_exports_solvers_tuple_of_specs() -> None:
    import importlib

    cpsat = importlib.import_module("twinflow.scheduling.algorithms.cpsat")

    assert isinstance(cpsat.SOLVERS, tuple)
    assert [spec.name for spec in cpsat.SOLVERS] == ["ortools"]
    assert all(isinstance(spec, SolverSpec) for spec in cpsat.SOLVERS)


# --- is_registered / get ---------------------------------------------------------------------


def test_is_registered_true_for_registered_and_false_for_unknown() -> None:
    assert registry.is_registered("baseline") is True
    assert registry.is_registered("ortools") is True
    assert registry.is_registered("nope") is False
    assert registry.is_registered("") is False


def test_get_unknown_name_raises_unknown_solver_error_naming_registered_names() -> None:
    with pytest.raises(UnknownSolverError) as caught:
        registry.get("nope")

    message = str(caught.value)
    assert "nope" in message
    for info in registry.available():
        assert info.name in message


def test_unknown_solver_error_is_a_lookup_or_value_error() -> None:
    assert issubclass(UnknownSolverError, KeyError | ValueError)


def test_get_baseline_returns_the_baseline_solver() -> None:
    solver = registry.get("baseline")

    assert solver.name == "baseline"
    assert solver.capabilities == BASELINE_CAPABILITIES


def test_get_returns_a_fresh_instance_per_call() -> None:
    assert registry.get("baseline") is not registry.get("baseline")


def test_get_ortools_without_ortools_raises_optional_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module in ORTOOLS_MODULES:
        monkeypatch.setitem(sys.modules, module, None)

    with pytest.raises(Exception) as caught:
        registry.get("ortools")

    assert type(caught.value).__name__ == "OptionalDependencyError"
    assert not isinstance(caught.value, UnknownSolverError)
    assert registry.is_registered("ortools") is True


def test_get_ortools_passes_params_to_the_factory() -> None:
    pytest.importorskip("ortools")

    assert registry.get("ortools", workers=1, seed=3).name == "ortools"
    with pytest.raises(ValueError, match="workers"):
        registry.get("ortools", workers=0)


# --- solve with registry solvers ---------------------------------------------------------------


def test_solve_without_solver_still_uses_the_baseline() -> None:
    result = solve(_fixture("pinedo_7_1_1.json"))

    assert result.schedule.solver == "baseline"
    assert result.verified is True


def test_solve_with_registry_ortools_verifies_pinedo_7_1_1() -> None:
    pytest.importorskip("ortools")

    result = solve(
        _fixture("pinedo_7_1_1.json"), solver=registry.get("ortools"), budget_seconds=10.0
    )

    assert result.verified is True
    assert result.schedule.solver == "ortools"


# --- priority rules by name (T-149 deviation: SDD COMP-002 has no rule registry) ----------------
#
# Construction and beam search need a dispatch rule object, not a solver: beam's rollout
# continues a partial `DispatchState`, which a whole-solver call cannot start from. Leaves
# export `RULES: tuple[RuleSpec, ...]` beside `SOLVERS`; `registry.rule(name, **params)`
# builds a fresh rule (the engine prepares it per run).

DISPATCH_RULE_NAMES = ("cr", "edd", "fifo", "lfj", "lpt", "ms", "spt", "sqno", "sst", "wspt")


def test_rule_spec_has_name_and_factory_and_is_frozen() -> None:
    assert {field.name for field in dataclasses.fields(RuleSpec)} == {"name", "factory"}
    spec = RuleSpec(
        name="probe",
        factory=lambda: None,  # type: ignore[arg-type,return-value]  # never called
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "other"  # type: ignore[misc]  # proving the dataclass is frozen


@pytest.mark.parametrize("name", [*DISPATCH_RULE_NAMES, "atc", "atcs"])
def test_rule_builds_a_fresh_priority_rule_per_call(name: str) -> None:
    first, second = registry.rule(name), registry.rule(name)

    assert first is not second
    assert callable(first.index)


def test_every_dispatch_family_solver_has_a_rule_of_the_same_name() -> None:
    dispatch = sorted(info.name for info in registry.available() if info.family == "dispatch")

    assert dispatch == sorted(DISPATCH_RULE_NAMES)
    for name in dispatch:
        assert registry.rule(name) is not None


def test_rule_unknown_name_raises_unknown_rule_error_naming_registered_rules() -> None:
    with pytest.raises(UnknownRuleError) as caught:
        registry.rule("baseline")

    message = str(caught.value)
    assert "baseline" in message
    for name in (*DISPATCH_RULE_NAMES, "atc", "atcs"):
        assert name in message
    assert issubclass(UnknownRuleError, ValueError)


def test_rule_with_an_invalid_param_raises_value_error_not_unknown_rule() -> None:
    for name, params in (("edd", {"K1": 1.0}), ("atcs", {"K1": -1.0}), ("atcs", {"nope": 1})):
        with pytest.raises(ValueError) as caught:
            registry.rule(name, **params)
        assert not isinstance(caught.value, UnknownRuleError), name


def _first_on_m1(rule_name: str, **params: object) -> str:
    request = _fixture("pinedo_14_3_4.json")
    index = ProblemIndex.build(request.problem)
    result = DispatchEngine(index, registry.rule(rule_name, **params)).run()
    assert isinstance(result, Dispatched), result
    return result.sequences["M1"][0]


def test_atc_rule_takes_its_k1_param() -> None:
    # Ex 14.3.4 with ATC K = 5 starts with job 4 (T-131); K1 reaches the rule.
    assert _first_on_m1("atc", K1=5.0) == "4-1"


def test_atcs_rule_is_prepared_per_run_on_each_problem() -> None:
    rule = registry.rule("atcs")
    for fixture in ("pinedo_14_3_4.json", "pinedo_7_1_1.json"):
        index = ProblemIndex.build(_fixture(fixture).problem)
        assert isinstance(DispatchEngine(index, rule).run(), Dispatched), fixture


# --- discovery over a copied tree ---------------------------------------------------------------

_LIST_SCRIPT = textwrap.dedent(
    """
    import json, sys
    import twinflow
    assert twinflow.__file__.startswith(sys.argv[1]), twinflow.__file__
    from twinflow.scheduling import registry
    infos = registry.available()
    out = {"infos": [[i.name, i.family, i.module, sorted(i.capabilities)] for i in infos]}
    out["registered"] = registry.is_registered("zz_probe")
    try:
        registry.get("zz_probe")
    except Exception as error:
        out["get_error"] = type(error).__name__
    print(json.dumps(out))
    """
)

_OPTIONAL_IN_FACTORY = textwrap.dedent(
    '''
    """Probe leaf: its optional dependency is imported only when the factory runs."""
    import importlib

    from twinflow.scheduling.registry import SolverSpec
    from twinflow.scheduling.seam import Feature


    def _build():
        importlib.import_module("twinflow_probe_missing_optional_dependency")


    SOLVERS = (
        SolverSpec(
            name="zz_probe",
            family="probe",
            factory=_build,
            deterministic=True,
            capabilities=frozenset({Feature.PINS}),
        ),
    )
    '''
)

_RULE_LEAF = textwrap.dedent(
    '''
    """Probe leaf: one priority rule and no solver."""
    from twinflow.scheduling.registry import RuleSpec


    class _Zero:
        def index(self, state, op_id, machine_id, t, last_op_id):
            return 0.0


    SOLVERS = ()
    RULES = (RuleSpec(name="zz_rule", factory=_Zero),)
    '''
)

_RULE_SCRIPT = textwrap.dedent(
    """
    import json
    from twinflow.scheduling import registry
    print(json.dumps({"rule": type(registry.rule("zz_rule")).__name__}))
    """
)

_BROKEN_AT_IMPORT = 'raise RuntimeError("probe-leaf-is-broken")\n'


def _tree_with(tmp_path: Path, leaf_name: str, leaf_source: str) -> Path:
    """A copy of `src/twinflow` with one extra module planted in `algorithms/`."""
    root = tmp_path / "src"
    shutil.copytree(SRC, root / "twinflow", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "twinflow" / "scheduling" / "algorithms" / f"{leaf_name}.py").write_text(leaf_source)
    return root


def _run_listing(tmp_path: Path, root: Path) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "list_registry.py"
    script.write_text(_LIST_SCRIPT)
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(  # noqa: S603  # argv is sys.executable plus paths this test made
        [sys.executable, str(script), str(root)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )


def test_new_leaf_module_registers_by_discovery_alone(tmp_path: Path) -> None:
    root = _tree_with(tmp_path, "zz_probe", _OPTIONAL_IN_FACTORY)

    done = _run_listing(tmp_path, root)

    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    probe = [info for info in out["infos"] if info[0] == "zz_probe"]
    assert probe == [["zz_probe", "probe", "twinflow.scheduling.algorithms.zz_probe", ["pins"]]]
    assert out["registered"] is True


def test_leaf_missing_optional_dependency_in_factory_still_registers(tmp_path: Path) -> None:
    root = _tree_with(tmp_path, "zz_probe", _OPTIONAL_IN_FACTORY)

    done = _run_listing(tmp_path, root)

    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert "zz_probe" in [info[0] for info in out["infos"]]
    assert "get_error" in out
    assert out["get_error"] != "UnknownSolverError"


def test_leaf_with_a_non_import_error_at_top_level_propagates(tmp_path: Path) -> None:
    root = _tree_with(tmp_path, "zz_probe", _BROKEN_AT_IMPORT)

    done = _run_listing(tmp_path, root)

    assert done.returncode != 0
    assert "RuntimeError" in done.stderr
    assert "probe-leaf-is-broken" in done.stderr


def test_leaf_rules_register_by_discovery_alone(tmp_path: Path) -> None:
    root = _tree_with(tmp_path, "zz_rule_probe", _RULE_LEAF)
    script = tmp_path / "rule_probe.py"
    script.write_text(_RULE_SCRIPT)
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}

    done = subprocess.run(  # noqa: S603  # argv is sys.executable plus a path this test made
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"rule": "_Zero"}
