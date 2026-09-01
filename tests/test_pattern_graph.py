"""Pattern 转移图构建与注册期 fail fast 测试。"""

import pytest

from src.dialogue.module import AgentModule, FSMModule, ModuleLink
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern


def _mk_pattern(modules, **kw):
    return Pattern(
        code="p_test", name="t", description="t",
        entry_module_code=modules[0].module_code, modules=modules, **kw,
    )


def test_dispatch_graph_from_links():
    a = AgentModule(module_code="a", sub_modules=["b", ModuleLink(target="c")])
    b = AgentModule(module_code="b")
    c = FSMModule(module_code="c")
    p = _mk_pattern([a, b, c])
    assert p.dispatch_graph == {"a": {"b", "c"}}


def test_route_jump_module_derived_into_graph():
    menu = BaseNode(node_code="menu_x", node_name="x", jump_module="b")
    root = BaseNode(node_code="root", node_name="r", sub_nodes=["menu_x"])
    route_mod = AgentModule(module_code="rt", module_nodes=[root, menu])
    route_mod.type = type(route_mod).type  # 保持默认；推导只看 jump_module 属性
    b = AgentModule(module_code="b")
    p = _mk_pattern([route_mod, b])
    assert "b" in p.dispatch_graph["rt"]


def test_dangling_link_raises():
    a = AgentModule(module_code="a", sub_modules=["ghost"])
    b = AgentModule(module_code="b")
    with pytest.raises(ValueError, match="悬空"):
        _mk_pattern([a, b])


def test_unauthorized_lend_raises():
    b = AgentModule(module_code="b", use_tools=["t1"])
    a = AgentModule(
        module_code="a",
        sub_modules=[ModuleLink(target="b", lend_tools=["t_not_in_b"])],
    )
    with pytest.raises(ValueError, match="借出"):
        _mk_pattern([a, b])


def test_self_loop_raises():
    a = AgentModule(module_code="a", sub_modules=["a"])
    with pytest.raises(ValueError, match="自环"):
        _mk_pattern([a])


def test_agent_to_fsm_link_allowed():
    """混合 pattern：AGENT → FSM 边合法（不拦）。"""
    a = AgentModule(module_code="a", sub_modules=["f"])
    f = FSMModule(module_code="f", module_nodes=[
        BaseNode(node_code="f1", node_name="n1", is_end=True)
    ])
    p = _mk_pattern([a, f])
    assert p.dispatch_graph["a"] == {"f"}


def test_max_hops_default_and_override():
    a = AgentModule(module_code="a")
    assert _mk_pattern([a]).max_hops == 2
    assert _mk_pattern([a], max_hops=1).max_hops == 1


def test_route_jump_module_self_loop_raises():
    """M-1：jump_module 指向自身模块 → 注册期自环 fail fast。"""
    menu = BaseNode(node_code="menu_self", node_name="m", jump_module="rt")
    root = BaseNode(node_code="root2", node_name="r", sub_nodes=["menu_self"])
    route_mod = AgentModule(module_code="rt", module_nodes=[root, menu])
    with pytest.raises(ValueError, match="自环"):
        _mk_pattern([route_mod])
