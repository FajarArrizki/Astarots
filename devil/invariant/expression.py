"""Typed, side-effect-free expression AST for invariant predicates."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from operator import add, eq, floordiv, ge, gt, le, lt, mod, mul, ne, sub
from typing import Any


class PredicateError(ValueError):
    """Raised when a predicate is invalid or uses unsupported syntax."""


class UnaryOperator(StrEnum):
    NOT = "not"
    NEGATE = "negate"


class BinaryOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"
    AND = "and"
    OR = "or"
    ADD = "add"
    SUB = "sub"
    MUL = "mul"
    DIV = "div"
    MOD = "mod"


@dataclass(frozen=True)
class Literal:
    value: Any


@dataclass(frozen=True)
class Reference:
    name: str


@dataclass(frozen=True)
class Index:
    value: Expression
    key: Expression


@dataclass(frozen=True)
class Unary:
    op: UnaryOperator
    operand: Expression


@dataclass(frozen=True)
class Binary:
    op: BinaryOperator
    left: Expression
    right: Expression


@dataclass(frozen=True)
class AdapterCall:
    function: str
    version: str
    arguments: tuple[Expression, ...]


ExpressionNode = Literal | Reference | Index | Unary | Binary | AdapterCall


@dataclass(frozen=True)
class Expression:
    """A typed normalized predicate expression."""

    value_type: str
    node: ExpressionNode
    source: str

    def evaluate(
        self,
        values: Mapping[str, Any],
        adapters: Mapping[str, Callable[..., Any]] | None = None,
    ) -> Any:
        result = _evaluate(self, values, adapters or {})
        _validate_integer(self.value_type, result)
        return result

    def canonical(self) -> str:
        return _canonical(self)


@dataclass(frozen=True)
class PredicateExpression:
    """Compatibility wrapper around a typed boolean expression."""

    expression: Expression

    @property
    def source(self) -> str:
        return self.expression.source

    @classmethod
    def parse(
        cls,
        source: str,
        value_types: Mapping[str, str] | None = None,
        adapter_functions: Mapping[str, str] | None = None,
    ) -> PredicateExpression:
        expression = parse_expression(source, value_types, adapter_functions)
        if expression.value_type != "bool":
            raise PredicateError("predicate must have boolean type")
        return cls(expression)

    def evaluate(self, values: Mapping[str, Any]) -> bool:
        result = self.expression.evaluate(values)
        if not isinstance(result, bool):
            raise PredicateError("predicate must evaluate to a boolean")
        return result


def parse_expression(
    source: str,
    value_types: Mapping[str, str] | None = None,
    adapter_functions: Mapping[str, str] | None = None,
) -> Expression:
    if not source.strip():
        raise PredicateError("predicate expression cannot be empty")
    python_source = _solidity_to_python(source)
    try:
        tree = ast.parse(python_source, mode="eval")
    except SyntaxError as exc:
        raise PredicateError(f"invalid predicate syntax: {exc.msg}") from exc
    return _from_ast(tree.body, source.strip(), value_types or {}, adapter_functions or {})


def evaluate_predicate(
    source: str | Expression,
    values: Mapping[str, Any],
    adapters: Mapping[str, Callable[..., Any]] | None = None,
) -> bool:
    expression = source if isinstance(source, Expression) else parse_expression(source)
    result = expression.evaluate(values, adapters)
    if not isinstance(result, bool):
        raise PredicateError("predicate must evaluate to a boolean")
    return result


def _solidity_to_python(source: str) -> str:
    converted = source.replace("&&", " and ").replace("||", " or ")
    converted = re.sub(r"!(?!=)", " not ", converted)
    converted = re.sub(r"\btrue\b", "True", converted, flags=re.IGNORECASE)
    return re.sub(r"\bfalse\b", "False", converted, flags=re.IGNORECASE).strip()


def _from_ast(
    node: ast.AST,
    source: str,
    value_types: Mapping[str, str],
    adapter_functions: Mapping[str, str],
) -> Expression:
    if isinstance(node, ast.Constant) and isinstance(node.value, (bool, int, str)):
        value_type = (
            "bool"
            if isinstance(node.value, bool)
            else "uint256"
            if isinstance(node.value, int)
            else "string"
        )
        return Expression(value_type, Literal(node.value), source)
    if isinstance(node, ast.Name):
        if node.id not in value_types:
            if node.id[:1].isupper():
                return Expression("enum", Literal(node.id), source)
            raise PredicateError(f"unknown predicate variable: {node.id}")
        return Expression(value_types[node.id], Reference(node.id), source)
    if isinstance(node, ast.Subscript):
        value = _from_ast(node.value, source, value_types, adapter_functions)
        key = _from_ast(node.slice, source, value_types, adapter_functions)
        element_type = (
            value.value_type.removesuffix("[]") if value.value_type.endswith("[]") else "uint256"
        )
        return Expression(element_type, Index(value, key), source)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub)):
        operand = _from_ast(node.operand, source, value_types, adapter_functions)
        if isinstance(node.op, ast.Not):
            if operand.value_type != "bool":
                raise PredicateError("logical not requires bool")
            return Expression("bool", Unary(UnaryOperator.NOT, operand), source)
        _require_numeric(operand)
        return Expression(operand.value_type, Unary(UnaryOperator.NEGATE, operand), source)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        operands = [_from_ast(item, source, value_types, adapter_functions) for item in node.values]
        if any(item.value_type != "bool" for item in operands):
            raise PredicateError("logical operators require bool operands")
        operator = BinaryOperator.AND if isinstance(node.op, ast.And) else BinaryOperator.OR
        result = operands[0]
        for operand in operands[1:]:
            result = Expression("bool", Binary(operator, result, operand), source)
        return result
    if isinstance(node, ast.BinOp):
        left = _from_ast(node.left, source, value_types, adapter_functions)
        right = _from_ast(node.right, source, value_types, adapter_functions)
        _require_compatible(left, right, numeric=True)
        operators = {
            ast.Add: BinaryOperator.ADD,
            ast.Sub: BinaryOperator.SUB,
            ast.Mult: BinaryOperator.MUL,
            ast.Div: BinaryOperator.DIV,
            ast.FloorDiv: BinaryOperator.DIV,
            ast.Mod: BinaryOperator.MOD,
        }
        operator = operators.get(type(node.op))
        if operator is None:
            raise PredicateError(f"unsupported arithmetic operator: {type(node.op).__name__}")
        return Expression(left.value_type, Binary(operator, left, right), source)
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise PredicateError("chained comparisons are not supported")
        left = _from_ast(node.left, source, value_types, adapter_functions)
        right = _from_ast(node.comparators[0], source, value_types, adapter_functions)
        _require_compatible(left, right)
        operators = {
            ast.Eq: BinaryOperator.EQ,
            ast.NotEq: BinaryOperator.NE,
            ast.Lt: BinaryOperator.LT,
            ast.LtE: BinaryOperator.LE,
            ast.Gt: BinaryOperator.GT,
            ast.GtE: BinaryOperator.GE,
        }
        operator = operators.get(type(node.ops[0]))
        if operator is None:
            raise PredicateError(f"unsupported comparison: {type(node.ops[0]).__name__}")
        return Expression("bool", Binary(operator, left, right), source)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        version = adapter_functions.get(node.func.id)
        if version is None:
            raise PredicateError(f"unsupported adapter function is not declared: {node.func.id}")
        arguments = tuple(
            _from_ast(argument, source, value_types, adapter_functions) for argument in node.args
        )
        return Expression("uint256", AdapterCall(node.func.id, version, arguments), source)
    raise PredicateError(f"unsupported predicate syntax: {type(node).__name__}")


def _require_numeric(expression: Expression) -> None:
    if not re.fullmatch(r"u?int(?:8|16|32|64|128|256)?", expression.value_type):
        raise PredicateError(f"numeric expression required, got {expression.value_type}")


def _require_compatible(left: Expression, right: Expression, *, numeric: bool = False) -> None:
    if numeric:
        _require_numeric(left)
        _require_numeric(right)
    if left.value_type != right.value_type:
        literal_pair = isinstance(left.node, Literal) or isinstance(right.node, Literal)
        if not literal_pair:
            raise PredicateError(
                f"implicit conversion is forbidden: {left.value_type} vs {right.value_type}"
            )


def _evaluate(
    expression: Expression,
    values: Mapping[str, Any],
    adapters: Mapping[str, Callable[..., Any]],
) -> Any:
    node = expression.node
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Reference):
        try:
            return values[node.name]
        except KeyError as exc:
            raise PredicateError(f"unknown predicate variable: {node.name}") from exc
    if isinstance(node, Index):
        container = _evaluate(node.value, values, adapters)
        key = _evaluate(node.key, values, adapters)
        try:
            return container[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise PredicateError("invalid predicate subscript") from exc
    if isinstance(node, Unary):
        value = _evaluate(node.operand, values, adapters)
        return not value if node.op is UnaryOperator.NOT else -value
    if isinstance(node, AdapterCall):
        function = adapters.get(node.function)
        if function is None:
            raise PredicateError(f"adapter function is unavailable: {node.function}@{node.version}")
        return function(*(_evaluate(item, values, adapters) for item in node.arguments))
    left = _evaluate(node.left, values, adapters)
    if node.op is BinaryOperator.AND and not left:
        return False
    if node.op is BinaryOperator.OR and left:
        return True
    right = _evaluate(node.right, values, adapters)
    operations = {
        BinaryOperator.EQ: eq,
        BinaryOperator.NE: ne,
        BinaryOperator.LT: lt,
        BinaryOperator.LE: le,
        BinaryOperator.GT: gt,
        BinaryOperator.GE: ge,
        BinaryOperator.AND: lambda a, b: bool(a and b),
        BinaryOperator.OR: lambda a, b: bool(a or b),
        BinaryOperator.ADD: add,
        BinaryOperator.SUB: sub,
        BinaryOperator.MUL: mul,
        BinaryOperator.DIV: floordiv,
        BinaryOperator.MOD: mod,
    }
    try:
        result = operations[node.op](left, right)
    except (ArithmeticError, TypeError) as exc:
        raise PredicateError("invalid expression operation") from exc
    _validate_integer(expression.value_type, result)
    return result


def _validate_integer(value_type: str, value: Any) -> None:
    match = re.fullmatch(r"(?P<unsigned>u?)int(?P<bits>8|16|32|64|128|256)?", value_type)
    if match is None or not isinstance(value, int) or isinstance(value, bool):
        return
    bits = int(match.group("bits") or "256")
    minimum = 0 if match.group("unsigned") else -(2 ** (bits - 1))
    maximum = 2**bits - 1 if match.group("unsigned") else 2 ** (bits - 1) - 1
    if not minimum <= value <= maximum:
        raise PredicateError(f"{value_type} arithmetic overflow")


def _canonical(expression: Expression) -> str:
    node = expression.node
    if isinstance(node, Literal):
        return repr(node.value)
    if isinstance(node, Reference):
        return node.name
    if isinstance(node, Index):
        return f"{_canonical(node.value)}[{_canonical(node.key)}]"
    if isinstance(node, Unary):
        return f"{node.op.value}({_canonical(node.operand)})"
    if isinstance(node, AdapterCall):
        args = ",".join(_canonical(item) for item in node.arguments)
        return f"{node.function}@{node.version}({args})"
    return f"{node.op.value}({_canonical(node.left)},{_canonical(node.right)})"
