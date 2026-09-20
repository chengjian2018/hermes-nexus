"""calculator 工具安全求值测试——AST 白名单 + 资源上限（无 LLM）。

覆盖：
1. 正常算术/函数/常数求值与结果格式化
2. 幂运算炸弹（9**9**9**9）被指数上限拦截，不挂线程
3. 节点级白名单：属性访问/下标/推导式等一律拒绝（不再依赖 eval 字符过滤）
4. 错误路径返回 tool_error 而非抛异常
"""

import json
import time

import pytest

from tools.calculator_tool import _safe_eval, _handle_calculator


def test_basic_arithmetic():
    assert _safe_eval("3 + 4 * 2") == 11
    assert _safe_eval("(3 + 4) * 2") == 14
    assert _safe_eval("7 // 2") == 3
    assert _safe_eval("7 % 3") == 1
    assert _safe_eval("2 ** 10") == 1024
    assert _safe_eval("-5 + 2") == -3


def test_functions_and_constants():
    assert _safe_eval("sqrt(16)") == 4.0
    assert _safe_eval("abs(-5) + round(3.7)") == 9
    assert _safe_eval("min(1, 2, 3) + max(1, 2)") == 3
    assert _safe_eval("log2(8)") == 3.0
    assert _safe_eval("round(pi, 2)") == 3.14


def test_power_bomb_rejected_fast():
    """9**9**9**9：字符全在白名单内，必须在指数上限处快速拒绝。"""
    start = time.monotonic()
    with pytest.raises(ValueError, match="指数过大|结果过大"):
        _safe_eval("9**9**9**9")
    assert time.monotonic() - start < 2.0

    with pytest.raises(ValueError, match="指数过大"):
        _safe_eval("2 ** 99999999")


def test_huge_int_result_capped():
    """合法指数但结果位宽超限（9**9999 ≈ 3.2 万 bit）：结果上限拦截。"""
    with pytest.raises(ValueError, match="结果过大"):
        _safe_eval("9**9999")


def test_node_whitelist_blocks_escapes():
    """节点级拒绝：属性链/下标/推导式/字符串常量/未知名字。"""
    for expr in (
        "(0).__class__",
        "(0).__class__.__base__.__subclasses__()",
        "().__doc__",
        "[x for x in (1, 2)]",
        "'abc'",
        "unknown_name",
        "1 if True else 2",
        "lambda: 1",
    ):
        with pytest.raises(ValueError):
            _safe_eval(expr)


def test_division_and_domain_errors():
    with pytest.raises(ZeroDivisionError):
        _safe_eval("1 / 0")
    with pytest.raises(ValueError):
        _safe_eval("sqrt(-1)")
    with pytest.raises(ValueError):
        _safe_eval("log(0)")


def test_handler_result_and_error_shapes():
    ok = json.loads(_handle_calculator({"expression": "3 * 4 + 2"}))
    assert ok["result"] == 14

    err = json.loads(_handle_calculator({"expression": "9**9**9**9"}))
    assert "指数过大" in err["error"] or "结果过大" in err["error"]

    missing = json.loads(_handle_calculator({}))
    assert "error" in missing
