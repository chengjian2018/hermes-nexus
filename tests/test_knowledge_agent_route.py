"""knowledge_agent pattern 单测：模块结构 / transfer 边 / 工具解析。"""

import pytest

from chat.loop import _resolve_tools, build_transfer_tools
from dialogue.knowledge_agent_route import (
    human_handoff,
    kb_agent,
    knowledge_agent_pattern,
)
from dialogue.register import registry as pattern_registry


@pytest.fixture(scope="module")
def pattern():
    # 模块 import 即注册；再显式 get 校验
    p = pattern_registry.get("knowledge_agent")
    assert p is not None
    return p


def test_pattern_structure(pattern):
    assert pattern.entry_module_code == "kb_agent"
    assert set(pattern.module_map) == {"kb_agent", "human_handoff"}
    assert human_handoff.module_code == "human_handoff"
    assert kb_agent.sub_modules[0].target == "human_handoff"


def test_kb_agent_resolves_four_knowledge_tools(pattern):
    schemas = _resolve_tools(kb_agent, pattern)
    names = {s["function"]["name"] for s in schemas}
    assert names == {
        "search_product_knowledge",
        "search_customer_service_knowledge",
        "list_products",
        "send_goods_link",
    }


def test_transfer_tool_generated(pattern):
    """sub_modules 声明边 → transfer_to_human_handoff 自动生成。"""
    tools = build_transfer_tools(kb_agent, pattern.module_map)
    names = {t["function"]["name"] for t in tools}
    assert "transfer_to_human_handoff" in names
    # 交接模块是 terminal，自己没有转移边
    assert build_transfer_tools(human_handoff, pattern.module_map) == []
