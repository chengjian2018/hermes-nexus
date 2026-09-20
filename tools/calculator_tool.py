"""Calculator tool — safe arithmetic expression evaluation.

Automatically registered via ``registry.register()`` at module import; no manual setup needed.

Tool name: ``calculator``
Toolset: ``utility``
Permission: all patterns, all modules (``{"*": True}``).
"""

import ast
import json
import math
import operator
import re
from typing import Any, Dict

from tools.register import registry, tool_error, tool_result

# ---------------------------------------------------------------------------
# Safe arithmetic evaluation — AST whitelist, no eval()
# ---------------------------------------------------------------------------

_SAFE_OPS: Dict[str, Any] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "//": operator.floordiv,
    "%": operator.mod,
    "**": operator.pow,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "pi": math.pi,
    "e": math.e,
}

_ALLOWED_FUNCS = {"abs", "round", "min", "max", "sqrt", "sin", "cos",
                  "tan", "log", "log2", "log10"}
_ALLOWED_CONSTS = {"pi", "e"}
_ALLOWED_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub,
                   ast.Mult: operator.mul, ast.Div: operator.truediv,
                   ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
                   ast.Pow: operator.pow}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# Cheap pre-filter (keeps gibberish out of the parser); the AST whitelist
# below is the actual security boundary
_ALLOWED_CHARS_RE = re.compile(r"^[a-zA-Z0-9\s\+\-\*/%=\(\)\._,]+$")

# Resource caps: without them `9**9**9**9` (chars all whitelisted) computes
# an astronomically large int and hangs the dispatch thread
_MAX_POW_EXPONENT = 10_000        # |exponent| cap at a ** node
_MAX_INT_BITS = 10_000            # magnitude cap on any intermediate int


class _CalcError(ValueError):
    """Expression rejected by the evaluator (syntax, whitelist, or caps)."""


def _check_value(value: Any) -> Any:
    if isinstance(value, int) and value.bit_length() > _MAX_INT_BITS:
        raise _CalcError("结果过大，拒绝计算")
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        raise _CalcError("结果溢出")
    return value


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise _CalcError(f"不支持的常量: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTS:
            return _SAFE_OPS[node.id]
        raise _CalcError(f"未知标识符: {node.id!r}")
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise _CalcError(f"不支持的运算符: {type(node.op).__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op is operator.pow:
            # Pre-check the exponent: nested ** bombs expand the inner
            # operand into the millions before any result cap could fire
            try:
                if abs(right) > _MAX_POW_EXPONENT:
                    raise _CalcError("幂运算指数过大，拒绝计算")
            except TypeError:
                raise _CalcError("幂运算指数非法") from None
        return _check_value(op(left, right))
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARY.get(type(node.op))
        if op is None:
            raise _CalcError(f"不支持的一元运算符: {type(node.op).__name__}")
        return _check_value(op(_eval_node(node.operand)))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise _CalcError("不支持的函数调用")
        if node.keywords:
            raise _CalcError("不支持关键字参数")
        args = [_eval_node(a) for a in node.args]
        return _check_value(_SAFE_OPS[node.func.id](*args))
    raise _CalcError(f"不支持的语法: {type(node).__name__}")


def _safe_eval(expression: str) -> float:
    """Safely evaluate an arithmetic expression.

    Parses with ``ast.parse`` and evaluates a whitelisted node set only
    (numeric constants, named math functions/constants, arithmetic ops) —
    attribute access, subscripts, comprehensions and everything else is
    rejected at the node level, and resource caps block ``**`` bombs.
    """
    expr = expression.strip()
    if not expr:
        raise _CalcError("表达式为空")

    # Character whitelist validation
    if not _ALLOWED_CHARS_RE.match(expr):
        raise _CalcError(f"Expression contains disallowed characters: {expr!r}")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise _CalcError(f"表达式语法无效: {e}") from e
    return _eval_node(tree)


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def _handle_calculator(args: Dict[str, Any]) -> str:
    """Handle calculator tool calls.

    Args:
        args: Dictionary containing the ``expression`` key, e.g. ``"3 * 4 + 2"``.

    Returns:
        JSON string with ``result`` or ``error`` field.
    """
    expression = args.get("expression", "")
    if not expression or not isinstance(expression, str):
        return tool_error("请提供有效的算术表达式", expression=expression)

    try:
        result = _safe_eval(expression)
        # Omit decimal point for integer values
        if isinstance(result, float) and result == int(result) and abs(result) < 1e15:
            result = int(result)
        return tool_result({"expression": expression, "result": result})
    except ZeroDivisionError:
        return tool_error("除零错误", expression=expression)
    except (ValueError, SyntaxError, TypeError) as e:
        return tool_error(f"表达式无效: {e}", expression=expression)
    except Exception as e:
        return tool_error(f"计算错误: {e}", expression=expression)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CALCULATOR_SCHEMA = {
    "name": "calculator",
    "description": (
        "执行算术运算。支持加减乘除 (+, -, *, /)、整除 (//)、取余 (%)、"
        "幂运算 (**)、以及常用数学函数: sqrt, sin, cos, tan, log, log2, "
        "log10, abs, round, min, max。常数: pi, e。"
        "示例表达式: '3 + 4 * 2', 'sqrt(16)', 'abs(-5) + round(3.7)'"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": (
                    "要求值的算术表达式。支持 +, -, *, /, //, %, **, "
                    "以及 sqrt, sin, cos, tan, abs, round, min, max, "
                    "log, log2, log10 等函数。"
                ),
            }
        },
        "required": ["expression"],
    },
}


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

registry.register(
    name="calculator",
    toolset="utility",
    schema=CALCULATOR_SCHEMA,
    handler=_handle_calculator,
    description="安全算术表达式求值，支持加减乘除与常用数学函数",
    emoji="🔢",
    allowed_patterns={"*": True},
)