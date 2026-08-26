"""COMP-015 ExpressionSandbox — unit tests (T-024, red phase).

SECURITY-CRITICAL: `model/expressions.py` is the ONLY simpleeval importer (D-002) and
is the trust boundary between config-authored expressions and the running engine.

These tests regression-guard CVE-2026-32640 (simpleeval < 1.0.5 sandbox escape,
CVSS 8.7 — a dunder-attribute walk from a whitelisted object to `os.system`) and lock
in the project-imposed depth, length and MAX_POWER guards named in tech.md
(D-002, D-006, D-036).

Declared attribute namespaces mirror COMP-005 `PartTypeRegistry` (the declared
attribute authority per structure.md Layer 2): referencing anything the registry did
not declare is a whitelist violation, not merely a missing-variable error.

API under test (contract, not yet implemented — see model/expressions.py stub):
    sandbox = ExpressionSandbox(max_depth=..., max_length=...)
    fn = sandbox.compile(expr_str, config_path, allowed_names)
    fn(namespace) -> value
    # raises ExpressionError (config_path named in the message) on any violation.
"""

from __future__ import annotations

import os
import time

import pytest

from factory_twin.model.expressions import ExpressionError, ExpressionSandbox
from factory_twin.primitives.part import PartTypeRegistry

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CONFIG_PATH = "locations.ht_oven.transform.output[0].attributes.length"


def _sandbox(max_depth: int = 10, max_length: int = 200) -> ExpressionSandbox:
    return ExpressionSandbox(max_depth=max_depth, max_length=max_length)


def _blank_registry() -> PartTypeRegistry:
    return PartTypeRegistry(
        {
            "blank": {
                "attributes": {"length": float, "diameter": float},
                "uom": "piece",
            },
        }
    )


def _declared_names(registry: PartTypeRegistry, part_id: str) -> set[str]:
    """The declared attribute namespace for part_id, per PartTypeRegistry (COMP-005)."""
    return set(registry.attributes(part_id))


# ---------------------------------------------------------------------------
# Acceptance 1 — CVE-2026-32640 sandbox-escape regression
# ---------------------------------------------------------------------------

DUNDER_ESCAPE_PAYLOADS = [
    # classic __class__ -> __bases__ -> __subclasses__ MRO walk off a whitelisted name
    "length.__class__.__bases__[0].__subclasses__()",
    # __globals__ walk off a whitelisted attribute chain toward __builtins__
    "length.__class__.__init__.__globals__['__builtins__']['eval']('1')",
    # object() literal -> __class__ -> __base__ -> subclasses -> os.system
    "().__class__.__base__.__subclasses__()[0].__init__.__globals__['os'].system('id')",
]


@pytest.mark.parametrize("payload", DUNDER_ESCAPE_PAYLOADS)
def test_compile_dunder_escape_payload_refused(payload: str) -> None:
    sandbox = _sandbox()
    registry = _blank_registry()
    allowed = _declared_names(registry, "blank")
    config_path = "locations.ht_oven.transform.output[0].attributes.escape_attempt"

    with pytest.raises(ExpressionError) as exc_info:
        fn = sandbox.compile(payload, config_path, allowed)
        fn({"length": 10.0, "diameter": 2.0})

    assert config_path in str(exc_info.value)


def test_compile_dunder_escape_payload_never_calls_os_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payload must be REFUSED, never evaluated — os.system must never fire."""
    calls: list[str] = []
    monkeypatch.setattr(os, "system", lambda cmd: calls.append(cmd))

    sandbox = _sandbox()
    payload = "().__class__.__base__.__subclasses__()[0].__init__.__globals__['os'].system('id')"

    with pytest.raises(ExpressionError):
        fn = sandbox.compile(payload, CONFIG_PATH, set())
        fn({})

    assert calls == [], "os.system was invoked -- the sandbox escape was NOT refused"


def test_compile_import_dunder_name_refused() -> None:
    """A direct dunder-builtin name lookup is refused, not just attribute chains."""
    sandbox = _sandbox()

    with pytest.raises(ExpressionError):
        fn = sandbox.compile("__import__('os').system('id')", CONFIG_PATH, set())
        fn({})


# ---------------------------------------------------------------------------
# Acceptance 2 — depth and length limits
# ---------------------------------------------------------------------------


def test_compile_expression_exceeding_max_depth_refused() -> None:
    sandbox = ExpressionSandbox(max_depth=3, max_length=500)
    config_path = "locations.ht_oven.time.depth_expr"
    # Ten nested parenthesized additions -- far beyond a max_depth of 3 under any
    # reasonable AST-depth accounting.
    deep_expr = "1+(1+(1+(1+(1+(1+(1+(1+(1+(1+1)))))))))"

    with pytest.raises(ExpressionError) as exc_info:
        sandbox.compile(deep_expr, config_path, set())

    assert config_path in str(exc_info.value)


def test_compile_expression_exceeding_max_length_refused() -> None:
    sandbox = ExpressionSandbox(max_depth=50, max_length=40)
    config_path = "locations.ht_oven.time.length_expr"
    # Structurally shallow (a single addition) but padded with whitespace to blow
    # past a max_length of 40 characters -- isolates the length limit from depth.
    long_expr = "1" + (" " * 200) + "+ 1"
    assert len(long_expr) > 40

    with pytest.raises(ExpressionError) as exc_info:
        sandbox.compile(long_expr, config_path, set())

    assert config_path in str(exc_info.value)


def test_compile_expression_within_depth_and_length_limits_succeeds() -> None:
    """Boundary sanity check: a small, shallow expression under generous limits
    is NOT refused -- proves the limits reject violations, not everything."""
    sandbox = ExpressionSandbox(max_depth=10, max_length=200)

    fn = sandbox.compile("1 + 1", CONFIG_PATH, set())

    assert fn({}) == 2


# ---------------------------------------------------------------------------
# Acceptance 3 — MAX_POWER guard (CVE-2026-32640 DoS companion, D-036)
# ---------------------------------------------------------------------------


def test_compile_max_power_guard_refuses_9_pow_9_pow_6_quickly() -> None:
    """9**9**6 must be REFUSED by the power guard, not computed.

    tech.md (D-036): "Keep simpleeval's MAX_POWER guard active -- an unguarded
    9**9**6 takes over 30 seconds." The test asserts BOTH that it raises AND that
    it does so quickly -- if the guard is inactive, this test either times out via
    the elapsed-time assertion or fails because no ExpressionError was raised.
    """
    sandbox = ExpressionSandbox(max_depth=10, max_length=100)

    start = time.perf_counter()
    with pytest.raises(ExpressionError):
        fn = sandbox.compile("9**9**6", CONFIG_PATH, set())
        fn({})
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0, f"MAX_POWER guard must refuse quickly; took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Acceptance 4 — errors name the offending config path
# ---------------------------------------------------------------------------
# (covered inline above for the dunder-escape, depth and length cases; the
# undeclared-attribute case is covered below alongside acceptance 5)


# ---------------------------------------------------------------------------
# Acceptance 5 — declared attribute namespace: valid compiles, undeclared refused
# ---------------------------------------------------------------------------


def test_compile_valid_expression_evaluates_declared_attributes() -> None:
    sandbox = _sandbox()
    registry = _blank_registry()
    allowed = _declared_names(registry, "blank")  # {"length", "diameter"}

    fn = sandbox.compile("length * 2 + diameter", CONFIG_PATH, allowed)

    assert fn({"length": 10.0, "diameter": 3.0}) == 23.0


def test_compile_undeclared_attribute_refused_even_when_present_in_namespace() -> None:
    """Whitelisting is by DECLARATION, not by what happens to be in the runtime
    namespace dict -- "weight" is supplied at call time but was never declared."""
    sandbox = _sandbox()
    registry = _blank_registry()
    allowed = _declared_names(registry, "blank")  # {"length", "diameter"} -- no "weight"
    config_path = "locations.ht_oven.scrap.undeclared_expr"

    with pytest.raises(ExpressionError) as exc_info:
        fn = sandbox.compile("length * weight", config_path, allowed)
        fn({"length": 10.0, "diameter": 3.0, "weight": 5.0})

    assert config_path in str(exc_info.value)


def test_compile_empty_expression_refused() -> None:
    sandbox = _sandbox()

    with pytest.raises(ExpressionError):
        sandbox.compile("", CONFIG_PATH, set())


# ---------------------------------------------------------------------------
# Adversarial: sandbox state isolation across independent compiles
# ---------------------------------------------------------------------------


def test_sandbox_reusable_after_a_refused_compile() -> None:
    """A refused (malicious or invalid) expression must not poison later, unrelated
    compiles on the same ExpressionSandbox instance."""
    sandbox = _sandbox()
    registry = _blank_registry()
    allowed = _declared_names(registry, "blank")

    fn_before = sandbox.compile("length + 1", "path.before", allowed)

    with pytest.raises(ExpressionError):
        sandbox.compile("length.__class__", "path.malicious", allowed)

    fn_after = sandbox.compile("diameter * 2", "path.after", allowed)

    assert fn_before({"length": 5.0, "diameter": 1.0}) == 6.0
    assert fn_after({"length": 5.0, "diameter": 4.0}) == 8.0
