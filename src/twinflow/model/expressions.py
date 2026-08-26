"""COMP-015 ExpressionSandbox — the ONLY importer of simpleeval (D-002), pinned >=1.0.5.

Compiles every config expression behind a whitelist with project-imposed depth and
length limits and the MAX_POWER guard active. Wraps errors naming the offending path.

Trust boundary (D-002, D-006, OWASP baseline in tech.md): an expression string is
config-authored, untrusted input. Before it is ever handed to simpleeval, this module
parses it with the stdlib `ast` module and rejects, by explicit allow-list, anything
that is not plain arithmetic/comparison over declared attribute names:

- `ast.Attribute` and `ast.Call` are never in the allow-list, which is what stops the
  CVE-2026-32640 dunder-attribute-chain escape (`__class__.__bases__[0]...`,
  `__globals__['__builtins__']`) before it can be evaluated at all — not merely
  refused by simpleeval's own runtime checks.
- Every `ast.Name` id must be present in the caller-declared `allowed_names` set and
  must not start with `_`; this is a whitelist by declaration, not by whatever keys
  happen to be in the runtime namespace dict.
- Chained/nested `**` (e.g. `9**9**6`) is refused at parse time, before any exponent
  is computed. simpleeval's own `MAX_POWER` guard (default 4_000_000) does not catch
  this case: 9**6 = 531441 is under the cap, so `9**9**6` reduces to `9**531441`,
  which simpleeval happily computes. Our AST-level chained-power check refuses it in
  microseconds instead.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Any

import simpleeval  # noqa: F401 — this module is intentionally the sole simpleeval importer.

# Allow-list of AST node types a config expression may contain. Deliberately excludes
# ast.Attribute, ast.Call, ast.Subscript, ast.Lambda, ast.Import and every statement
# form: none of those are needed for "arithmetic/comparison over declared attribute
# names" (structure.md D-005, D-049), and excluding them by default-deny is what
# stops CVE-2026-32640-shaped payloads regardless of simpleeval's own protections.
_ALLOWED_NODE_TYPES: frozenset[type[ast.AST]] = frozenset(
    {
        ast.Expr,
        ast.BinOp,
        ast.UnaryOp,
        ast.BoolOp,
        ast.Compare,
        ast.Name,
        ast.Constant,
        ast.Load,
        # binary / unary operators
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.USub,
        ast.UAdd,
        ast.Not,
        # boolean / comparison operators
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
    }
)


class ExpressionError(Exception):
    """Raised on parse, depth, length or whitelist violation; names the config path."""


def _node_depth(node: ast.AST) -> int:
    """Max nesting depth of node, counting every AST node (leaves are depth 1)."""
    children = list(ast.iter_child_nodes(node))
    if not children:
        return 1
    return 1 + max(_node_depth(child) for child in children)


def _contains_pow(node: ast.AST) -> bool:
    """True if node or anything under it is a ``**`` binary operation."""
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow) for n in ast.walk(node))


class ExpressionSandbox:
    """Compiles an expression string into a callable over a declared attribute namespace."""

    def __init__(self, max_depth: int, max_length: int) -> None:
        self._max_depth = max_depth
        self._max_length = max_length

    def compile(
        self, expr_str: str, config_path: str, allowed_names: set[str]
    ) -> Callable[[dict[str, Any]], Any]:
        """Validate expr_str and return a callable that evaluates it over a namespace.

        Every violation (length, empty, syntax, disallowed construct, depth,
        undeclared name, chained power) raises ExpressionError naming config_path.
        The returned callable performs no further AST validation; it only evaluates
        the already-vetted parse tree.
        """
        if len(expr_str) > self._max_length:
            raise ExpressionError(
                f"{config_path}: expression exceeds max_length={self._max_length} "
                f"(got {len(expr_str)} characters)"
            )
        if not expr_str.strip():
            raise ExpressionError(f"{config_path}: expression is empty")

        try:
            parsed = ast.parse(expr_str.strip())
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ExpressionError(f"{config_path}: could not parse expression: {exc}") from exc

        if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.Expr):
            raise ExpressionError(f"{config_path}: expression must be a single expression")

        body = parsed.body[0]

        try:
            depth = _node_depth(body)
        except RecursionError as exc:
            raise ExpressionError(f"{config_path}: expression nesting too deep") from exc
        if depth > self._max_depth:
            raise ExpressionError(
                f"{config_path}: expression exceeds max_depth={self._max_depth} (got {depth})"
            )

        for node in ast.walk(body):
            if type(node) not in _ALLOWED_NODE_TYPES:
                raise ExpressionError(
                    f"{config_path}: disallowed construct '{type(node).__name__}' in expression"
                )
            if isinstance(node, ast.Name):
                if node.id.startswith("_"):
                    raise ExpressionError(f"{config_path}: name '{node.id}' is not permitted")
                if node.id not in allowed_names:
                    raise ExpressionError(
                        f"{config_path}: name '{node.id}' is not in the declared "
                        "attribute namespace"
                    )
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
                if _contains_pow(node.left) or _contains_pow(node.right):
                    raise ExpressionError(
                        f"{config_path}: chained exponentiation ('**' nested inside "
                        "'**') is not permitted"
                    )

        def fn(namespace: dict[str, Any]) -> Any:
            names = {name: namespace[name] for name in allowed_names if name in namespace}
            evaluator = simpleeval.SimpleEval(names=names, functions={})
            evaluate = evaluator.eval
            try:
                return evaluate(expr_str, previously_parsed=body)
            except simpleeval.InvalidExpression as exc:
                raise ExpressionError(f"{config_path}: {exc}") from exc
            except RecursionError as exc:
                raise ExpressionError(f"{config_path}: expression too deep to evaluate") from exc

        return fn
