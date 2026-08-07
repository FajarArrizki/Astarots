"""Small, deterministic evaluator for declared invariant predicates."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from operator import add, eq, floordiv, ge, gt, le, lt, mul, ne, sub, truediv
from typing import Any


class PredicateError(ValueError):
    """Raised when a predicate is invalid or uses unsupported syntax."""


@dataclass(frozen=True)
class PredicateExpression:
    """Compiled expression restricted to pure value operations."""

    source: str
    _tree: ast.Expression

    @classmethod
    def parse(cls, source: str) -> PredicateExpression:
        if not source.strip():
            raise PredicateError("predicate expression cannot be empty")
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise PredicateError(f"invalid predicate syntax: {exc.msg}") from exc
        return cls(source=source, _tree=tree)

    def evaluate(self, values: Mapping[str, Any]) -> bool:
        result = _evaluate(self._tree.body, values)
        if not isinstance(result, bool):
            raise PredicateError("predicate must evaluate to a boolean")
        return result


def evaluate_predicate(source: str, values: Mapping[str, Any]) -> bool:
    return PredicateExpression.parse(source).evaluate(values)


def _evaluate(node: ast.AST, values: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (bool, int, float, str, type(None))
    ):
        return node.value
    if isinstance(node, ast.Name):
        try:
            return values[node.id]
        except KeyError as exc:
            raise PredicateError(f"unknown predicate variable: {node.id}") from exc
    if isinstance(node, ast.Subscript):
        container = _evaluate(node.value, values)
        key = _evaluate(node.slice, values)
        try:
            return container[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise PredicateError("invalid predicate subscript") from exc
    if isinstance(node, ast.Tuple):
        return tuple(_evaluate(item, values) for item in node.elts)
    if isinstance(node, ast.List):
        return [_evaluate(item, values) for item in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
        value = _evaluate(node.operand, values)
        if isinstance(node.op, ast.Not):
            return not value
        return -value if isinstance(node.op, ast.USub) else +value
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        results = (_evaluate(value, values) for value in node.values)
        if isinstance(node.op, ast.And):
            return all(results)
        return any(results)
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, values)
        right = _evaluate(node.right, values)
        operations = {
            ast.Add: add,
            ast.Sub: sub,
            ast.Mult: mul,
            ast.Div: truediv,
            ast.FloorDiv: floordiv,
        }
        operation = operations.get(type(node.op))
        if operation is None:
            raise PredicateError(f"unsupported arithmetic operator: {type(node.op).__name__}")
        try:
            return operation(left, right)
        except (ArithmeticError, TypeError) as exc:
            raise PredicateError("invalid arithmetic operation") from exc
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, values)
        comparisons = {
            ast.Eq: eq,
            ast.NotEq: ne,
            ast.Lt: lt,
            ast.LtE: le,
            ast.Gt: gt,
            ast.GtE: ge,
        }
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            operation = comparisons.get(type(operator))
            if operation is None:
                raise PredicateError(f"unsupported comparison: {type(operator).__name__}")
            right = _evaluate(comparator, values)
            try:
                if not operation(left, right):
                    return False
            except TypeError as exc:
                raise PredicateError("invalid comparison") from exc
            left = right
        return True
    raise PredicateError(f"unsupported predicate syntax: {type(node).__name__}")
