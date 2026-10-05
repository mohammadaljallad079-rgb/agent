"""Safe expression evaluation for workflow conditions and templates.

Workflow definitions are *untrusted input*: they are authored by users and may
come from an API. They must never be able to execute arbitrary Python. This
module therefore does not call :func:`eval`. It parses an expression with the
:mod:`ast` module, rejects anything outside a small whitelist of node types and
builtins, and then walks the validated tree with a resolver that only reads
from a plain data context.

What is allowed
---------------
* literals: strings, numbers, booleans, ``None``, lists, tuples
* names resolved from the context (``input``, ``steps``, ``workflow``, ...)
* dotted access into dictionaries (``steps.fetch.output.customer_id``)
* indexing and slicing (``items[0]``, ``items[1:3]``)
* comparisons, boolean logic, arithmetic
* the conditional expression ``a if cond else b``
* a fixed whitelist of pure builtins (``len``, ``int``, ``lower``, ...)

What is rejected
----------------
calls to anything but the whitelist, attribute access on non-dicts (so
``().__class__`` and friends are unreachable), comprehensions, lambdas,
assignment/walrus, ``await``/``yield``, f-strings, starred expressions and
anything else not enumerated. There is no path from an expression to the
interpreter's namespace, the filesystem, the network or an import.

Defense in depth: expressions are also bounded in source length and node count,
and ``**`` is bounded so a definition cannot be used for a cheap DoS.
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core.errors import ValidationError

#: Maximum characters in an expression source.
MAX_EXPRESSION_LENGTH = 2000
#: Maximum number of AST nodes, to bound evaluation cost.
MAX_NODES = 200
#: Maximum magnitude of an integer exponent.
MAX_EXPONENT = 64


class ExpressionError(ValidationError):
    """An expression could not be parsed or validated."""


class ExpressionEvaluationError(ValidationError):
    """A validated expression failed at evaluation time (e.g. missing key)."""


# --- Allowed operators ------------------------------------------------------
_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


def _fn_len(value: Any) -> int:
    return len(value)


def _fn_default(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value


def _fn_contains(haystack: Any, needle: Any) -> bool:
    try:
        return needle in haystack
    except TypeError:
        return False


#: Pure, deterministic builtins an expression may call. No I/O, no imports.
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": _fn_len,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "lower": lambda value: str(value).lower(),
    "upper": lambda value: str(value).upper(),
    "strip": lambda value: str(value).strip(),
    "startswith": lambda value, prefix: str(value).startswith(str(prefix)),
    "endswith": lambda value, suffix: str(value).endswith(str(suffix)),
    "contains": _fn_contains,
    "default": _fn_default,
}

#: Node types that may appear. Anything else fails validation.
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.Tuple,
    ast.List,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.BinOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    # operator nodes
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
)

_MISSING = object()


def _validate(tree: ast.AST) -> None:
    for count, node in enumerate(ast.walk(tree), start=1):
        if count > MAX_NODES:
            raise ExpressionError(f"expression too complex (>{MAX_NODES} nodes)")
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(
                f"disallowed expression element: {type(node).__name__}",
                details={"node": type(node).__name__},
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ExpressionError("dunder attribute access is not allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
                raise ExpressionError("call to a function that is not allowed")
            if node.keywords:
                raise ExpressionError("keyword arguments are not allowed in expressions")
        if isinstance(node, ast.Constant) and isinstance(node.value, (bytes, complex)):
            raise ExpressionError("unsupported constant type")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ExpressionError("dunder names are not allowed")


@dataclass(frozen=True)
class Expression:
    """A validated, immutable expression that can be evaluated against a context."""

    source: str
    _tree: ast.Expression

    @classmethod
    def compile(cls, source: str) -> Expression:
        if not isinstance(source, str) or not source.strip():
            raise ExpressionError("expression must be a non-empty string")
        # ``{{ expr }}`` templates carry surrounding spaces; ``eval`` mode is
        # indentation-sensitive, so trim before parsing.
        source = source.strip()
        if len(source) > MAX_EXPRESSION_LENGTH:
            raise ExpressionError(f"expression too long (>{MAX_EXPRESSION_LENGTH} characters)")
        try:
            parsed = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(
                f"invalid expression syntax: {exc.msg}", details={"source": source}
            ) from exc
        _validate(parsed)
        return cls(source=source, _tree=parsed)

    def evaluate(self, context: Mapping[str, Any]) -> Any:
        return _Evaluator(context).visit(self._tree.body)


class _Evaluator:
    """Walks a validated AST. Only reads from the supplied mapping."""

    def __init__(self, context: Mapping[str, Any]) -> None:
        self._context = context

    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, f"_visit_{type(node).__name__}", None)
        if method is None:  # pragma: no cover - validation prevents this
            raise ExpressionEvaluationError(f"cannot evaluate {type(node).__name__}")
        return method(node)

    def _visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def _visit_Name(self, node: ast.Name) -> Any:
        if node.id not in self._context:
            raise ExpressionEvaluationError(f"unknown name: {node.id}")
        return self._context[node.id]

    def _visit_Attribute(self, node: ast.Attribute) -> Any:
        base = self.visit(node.value)
        # Attribute access is *only* dotted access into a mapping. This makes
        # ``something.__class__`` impossible because non-dicts have no attributes
        # reachable here.
        if not isinstance(base, Mapping):
            raise ExpressionEvaluationError(
                f"cannot read attribute '{node.attr}' of a {type(base).__name__}"
            )
        if node.attr not in base:
            raise ExpressionEvaluationError(f"missing key: {node.attr}")
        return base[node.attr]

    def _visit_Subscript(self, node: ast.Subscript) -> Any:
        base = self.visit(node.value)
        index = node.slice
        if isinstance(index, ast.Slice):
            lower = self.visit(index.lower) if index.lower is not None else None
            upper = self.visit(index.upper) if index.upper is not None else None
            step = self.visit(index.step) if index.step is not None else None
            try:
                return base[slice(lower, upper, step)]
            except TypeError as exc:
                raise ExpressionEvaluationError(f"cannot slice {type(base).__name__}") from exc
        key = self.visit(index)
        try:
            return base[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExpressionEvaluationError(f"cannot index with {key!r}") from exc

    def _visit_Tuple(self, node: ast.Tuple) -> Any:
        return tuple(self.visit(elt) for elt in node.elts)

    def _visit_List(self, node: ast.List) -> Any:
        return [self.visit(elt) for elt in node.elts]

    def _visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self.visit(value)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = self.visit(value)
            if result:
                return result
        return result

    def _visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        return +operand

    def _visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Pow):
            if not isinstance(right, int) or abs(right) > MAX_EXPONENT:
                raise ExpressionEvaluationError("exponent out of bounds")
            return left**right
        op = _BIN_OPS.get(type(node.op))
        if op is None:  # pragma: no cover - validation prevents this
            raise ExpressionEvaluationError("unsupported operator")
        try:
            return op(left, right)
        except (TypeError, ZeroDivisionError) as exc:
            raise ExpressionEvaluationError(f"arithmetic error: {exc}") from exc

    def _visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op_node, comparator in zip(node.ops, node.comparators, strict=True):
            right = self.visit(comparator)
            op = _CMP_OPS.get(type(op_node))
            if op is None:  # pragma: no cover - validation prevents this
                raise ExpressionEvaluationError("unsupported comparison")
            try:
                if not op(left, right):
                    return False
            except TypeError as exc:
                raise ExpressionEvaluationError(f"comparison error: {exc}") from exc
            left = right
        return True

    def _visit_IfExp(self, node: ast.IfExp) -> Any:
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def _visit_Call(self, node: ast.Call) -> Any:
        func_node = node.func
        # Validation guarantees this is a whitelisted Name; narrow for mypy.
        if not isinstance(func_node, ast.Name):  # pragma: no cover - validation prevents this
            raise ExpressionEvaluationError("call to a function that is not allowed")
        name = func_node.id
        func = _FUNCTIONS[name]
        args = [self.visit(arg) for arg in node.args]
        try:
            return func(*args)
        except ExpressionEvaluationError:
            raise
        except (TypeError, ValueError) as exc:
            raise ExpressionEvaluationError(f"{name} failed: {exc}") from exc


def evaluate_condition(source: str, context: Mapping[str, Any]) -> bool:
    """Compile and evaluate ``source`` as a boolean condition.

    A condition may be a bare expression (``steps.gate.output.enabled``) or a
    single ``{{ ... }}`` template. The braces are unwrapped first: passing
    ``{{ x }}`` straight to ``eval`` mode would parse as a set literal.
    """
    if isinstance(source, str):
        full = _FULL_TEMPLATE_RE.match(source)
        if full is not None:
            source = full.group("expr")
    return bool(Expression.compile(source).evaluate(context))


# --- Templating -------------------------------------------------------------
#: A string of the form ``{{ expression }}`` (optionally surrounded by spaces).
_FULL_TEMPLATE_RE = re.compile(r"^\s*\{\{(?P<expr>.+?)\}\}\s*$", re.DOTALL)
_INLINE_TEMPLATE_RE = re.compile(r"\{\{(?P<expr>.+?)\}\}", re.DOTALL)
#: Bounds on a template payload to prevent unbounded expansion.
MAX_TEMPLATE_ITEMS = 500
MAX_TEMPLATE_STRING = 20000


def _render_string(value: str, context: Mapping[str, Any]) -> Any:
    """Render a single string.

    A string that is *entirely* one ``{{ expr }}`` keeps the expression's native
    type (so ``{{ steps.fetch.output.customer }}`` yields a dict, not its repr).
    A string with surrounding text interpolates each placeholder's ``str``.
    """
    full = _FULL_TEMPLATE_RE.match(value)
    if full is not None:
        return Expression.compile(full.group("expr")).evaluate(context)

    def _substitute(match: Any) -> str:
        result = Expression.compile(match.group("expr")).evaluate(context)
        text = "" if result is None else str(result)
        return text[:MAX_TEMPLATE_STRING]

    if _INLINE_TEMPLATE_RE.search(value):
        return _INLINE_TEMPLATE_RE.sub(_substitute, value)
    return value


def render(value: Any, context: Mapping[str, Any], *, _depth: int = 0) -> Any:
    """Recursively render templates in a JSON-like value against ``context``.

    Only ``str`` leaves are treated as templates; the container structure is
    preserved. Item count is bounded to keep a hostile definition from forcing
    an expensive expansion.
    """
    if _depth > 20:
        raise ExpressionError("template nesting too deep")
    if isinstance(value, str):
        return _render_string(value, context)
    if isinstance(value, dict):
        if len(value) > MAX_TEMPLATE_ITEMS:
            raise ExpressionError("template object too large")
        return {str(k): render(v, context, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_TEMPLATE_ITEMS:
            raise ExpressionError("template list too large")
        return [render(item, context, _depth=_depth + 1) for item in value]
    return value
