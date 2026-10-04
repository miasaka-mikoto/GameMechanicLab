"""Safe arithmetic formula parsing for damage and balance experiments.

The engine never evaluates formula text as unrestricted Python.  Expressions are
parsed with :mod:`ast`, checked against a small allow-list, then evaluated with an
empty builtins dictionary and explicit functions only.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Set


class FormulaError(ValueError):
    """Raised when a formula is malformed or uses a forbidden construct."""


_FUNCTIONS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
    "clamp": lambda x, low, high: max(low, min(high, x)),
}

_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load,
    ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
    ast.Call, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.BoolOp, ast.And, ast.Or, ast.IfExp,
)


def _validate(node: ast.AST, variables: Set[str]) -> None:
    parts = list(ast.walk(node))
    if len(parts) > 256:
        raise FormulaError("formula is too large")

    def depth(part: ast.AST) -> int:
        children = list(ast.iter_child_nodes(part))
        return 1 + max((depth(child) for child in children), default=0)

    if depth(node) > 32:
        raise FormulaError("formula is too deep")
    for part in parts:
        if not isinstance(part, _ALLOWED_NODES):
            raise FormulaError(f"unsupported syntax: {type(part).__name__}")
        if isinstance(part, ast.Name):
            if part.id not in variables and part.id not in _FUNCTIONS and part.id not in {"True", "False"}:
                raise FormulaError(f"unknown variable: {part.id}")
        if isinstance(part, ast.Call):
            if not isinstance(part.func, ast.Name) or part.func.id not in _FUNCTIONS:
                raise FormulaError("only approved math functions may be called")
            if part.keywords:
                raise FormulaError("keyword arguments are not allowed")
            if len(part.args) > 5:
                raise FormulaError("too many function arguments")
        if isinstance(part, ast.Constant) and not isinstance(part.value, (int, float, bool)):
            raise FormulaError("only numeric constants are allowed")
        if isinstance(part, ast.BinOp) and isinstance(part.op, ast.Pow):
            # Prevent giant exponentiation from hanging a batch sweep.
            if not isinstance(part.right, ast.Constant) or not isinstance(part.right.value, (int, float)):
                raise FormulaError("the exponent must be a small numeric constant")
            if abs(part.right.value) > 20:
                raise FormulaError("exponent is too large")


@dataclass(frozen=True)
class Formula:
    expression: str
    variables: Set[str]

    def __post_init__(self) -> None:
        normalized = _normalise_expression(self.expression)
        object.__setattr__(self, "expression", normalized)
        if not isinstance(normalized, str) or not normalized.strip():
            raise FormulaError("formula must be a non-empty expression")
        try:
            tree = ast.parse(normalized, mode="eval")
        except SyntaxError as exc:
            raise FormulaError(f"invalid formula: {exc.msg}") from exc
        _validate(tree, set(self.variables))
        object.__setattr__(self, "_tree", tree)

    def evaluate(self, values: Mapping[str, Any]) -> float:
        env: Dict[str, Any] = {name: 0.0 for name in self.variables}
        env.update({k: v for k, v in values.items() if k in self.variables})
        # ``True``/``False`` are harmless literals in an arithmetic expression,
        # but builtins are intentionally disabled, so bind them explicitly.
        env.update({"True": True, "False": False})
        env.update(_FUNCTIONS)
        try:
            value = eval(compile(self._tree, "<safe-formula>", "eval"), {"__builtins__": {}}, env)
        except ZeroDivisionError:
            raise FormulaError("division by zero")
        except (TypeError, ValueError, OverflowError) as exc:
            raise FormulaError(f"formula evaluation failed: {exc}") from exc
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise FormulaError("formula must evaluate to a number") from exc
        if not math.isfinite(number):
            raise FormulaError("formula produced a non-finite number")
        return number


class FormulaEngine:
    """Compile/cache safe formulas and evaluate them against named variables."""

    DEFAULT_VARIABLES = {
        "attack", "defense", "multiplier", "base_damage", "critical_multiplier",
        "critical", "is_critical", "target_hp", "target_max_hp", "source_hp",
        "source_max_hp", "combo", "stacks", "level", "distance", "resistance",
        "damage_taken", "damage_below_half_hp", "hit_count", "time",
    }

    def __init__(self, allowed_variables: Set[str] | None = None) -> None:
        self.allowed_variables = set(allowed_variables or self.DEFAULT_VARIABLES)
        self._cache: Dict[str, Formula] = {}

    def compile(self, expression: str) -> Formula:
        key = _normalise_expression(expression)
        if key not in self._cache:
            self._cache[key] = Formula(key, self.allowed_variables)
        return self._cache[key]

    def evaluate(self, expression: str, values: Mapping[str, Any]) -> float:
        return self.compile(expression).evaluate(values)

    def validate(self, expression: str) -> tuple[bool, str]:
        try:
            self.compile(expression)
            return True, ""
        except FormulaError as exc:
            return False, str(exc)


class SafeFormula:
    """Compatibility facade for the documented one-formula API.

    ``SafeFormula("attack - defense").evaluate(values)`` is equivalent to
    compiling the expression through :class:`FormulaEngine`, while retaining
    the restricted variable/function allow-list.
    """

    def __init__(self, expression: str, allowed_variables: Set[str] | None = None) -> None:
        self.expression = str(expression)
        self._formula = FormulaEngine(allowed_variables).compile(self.expression)

    def evaluate(self, values: Mapping[str, Any]) -> float:
        return self._formula.evaluate(values)


def evaluate_formula(expression: str, values: Mapping[str, Any]) -> float:
    """Convenience function used by small integrations."""
    return FormulaEngine().evaluate(expression, values)


_ASSIGNMENT = re.compile(r"^\s*(?:damage|result)\s*=\s*(.+?)\s*$", re.DOTALL)


def _normalise_expression(expression: str) -> str:
    """Accept the editor-friendly ``damage = <expression>`` spelling.

    Only the two documented output names are accepted; arbitrary assignments
    remain invalid because the evaluator is expression-only.
    """
    text = str(expression).strip()
    match = _ASSIGNMENT.match(text)
    return match.group(1).strip() if match else text


__all__ = ["FormulaError", "Formula", "FormulaEngine", "SafeFormula", "evaluate_formula"]
