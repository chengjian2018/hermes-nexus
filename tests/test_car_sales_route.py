"""car_sales_route（ROUTE 模式）离线测试。

通过脚本化 FakeProvider 模拟 LLM 输出（不访问真实 API），
覆盖：
1. Pattern 结构与 AST 自动发现注册
2. 顶层路由 → 购车 FSM 子模块完整多轮流程
3. 顶层路由 → 售后 FSM 子模块流程
4. 闲聊/未知意图停留在路由模块（无 jump_module / next_node 兜底）
5. RouteNLU 解析失败 → 重试成功；重试仍失败 → 兜底不崩溃
"""

import logging

import pytest

from fake_provider import (
    FAKE_PROVIDER_CODE,
    FakeProvider,
    fake_llm_config,
    register_fake_provider,
)

logging.basicConfig(level=logging.WARNING)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="session", autouse=True)
def _fake_provider():
    """注册脚本化 provider，测试全程复用。"""
    register_fake_provider()


@pytest.fixture(scope="module")
def pattern():
    """发现内置 pattern 并返回 car_sales_route。"""
    from src.dialogue.register import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "src.dialogue.car_sales_route" in imported, (
        f"car_sales_route 未被自动发现，已发现: {imported}"
    )
    return registry.get("car_sales_route")


@pytest.fixture()
def sessions():
    """每次测试独立的会话容器。"""
    return {}


def launch(pattern, sessions, session_id="s1"):
    """模拟 main.py 的 launch 流程：注册会话并注入管线上下文。"""
    from src.chat.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = {}
    session.cxt.llm_config = fake_llm_config()
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    """调用 src.chat.chat 处理一轮对话。"""
    from src.chat.chat import chat as chat_fn

    return chat_fn(query=query, session_id=session_id, all_sessions=sessions)


# ============================================================================
# 结构测试
# ============================================================================

def test_pattern_auto_discovered_and_structure(pattern):
    """Pattern 可被 AST 自动发现，模块/节点映射与路由结构正确。"""
    from src.dialogue.module import ModuleType

    assert pattern.code == "car_sales_route"
    assert pattern.entry_module_code == "car_sales_root"
    assert set(pattern.module_map) == {
        "car_sales_root", "car_sales_buy", "car_sales_after",
    }

    root_module = pattern.module_map["car_sales_root"]
    buy_module = pattern.module_map["car_sales_buy"]
    after_module = pattern.module_map["car_sales_after"]
    assert root_module.type == ModuleType.ROUTE
    assert buy_module.type == ModuleType.FSM
    assert after_module.type == ModuleType.FSM

    # 路由模块节点顺序：root 必须位于 module_nodes[0]（首节点）
    assert [n.node_code for n in root_module.module_nodes] == [
        "route_root", "menu_sales", "menu_after", "menu_chitchat",
    ]
    assert pattern.node_map["route_root"].sub_nodes == [
        "menu_sales", "menu_after", "menu_chitchat",
    ]

    # 意图菜单 → 子模块分发映射
    assert pattern.node_map["menu_sales"].jump_module == "car_sales_buy"
    assert pattern.node_map["menu_after"].jump_module == "car_sales_after"
    assert not hasattr(pattern.node_map["menu_chitchat"], "jump_module")

    # FSM 子模块节点链
    assert pattern.node_map["buy_ask_brand"].sub_nodes == ["buy_ask_budget"]
    assert pattern.node_map["buy_ask_budget"].sub_nodes == ["buy_ask_city"]
    assert pattern.node_map["buy_ask_city"].sub_nodes == ["buy_confirm"]
    assert pattern.node_map["buy_confirm"].is_end is True
    assert pattern.node_map["buy_ask_brand"].node_slots["brand"]
    assert pattern.node_map["after_ask_issue"].sub_nodes == ["after_ask_vehicle"]


# ============================================================================
# 路由分发流程测试
# ============================================================================

def test_route_to_buy_fsm_full_flow(pattern, sessions):
    """购车意图：路由分发 → 购车 FSM 子模块完整多轮（品牌→预算→城市→确认）。"""
    session = launch(pattern, sessions)

    # 第 1 轮：顶层路由，NLU 命中 menu_sales，回复来自菜单节点并分发到子模块
    reply = chat(sessions, "s1", "我想买车，帮忙看看有什么车型")
    assert "购车咨询" in reply, f"回复应来自菜单节点，实际: {reply!r}"
    assert session.cxt.current_module_code == "car_sales_buy"
    assert session.cxt.current_node_code is None  # 下一轮从子模块首节点开始

    # 第 2 轮：FSM 首节点询问品牌
    reply = chat(sessions, "s1", "比亚迪")
    assert "询问品牌" in reply
    assert session.cxt.current_module_code == "car_sales_buy"
    assert session.cxt.current_node_code == "buy_ask_budget"
    assert session.cxt.filled_slots["brand"] == "比亚迪"

    # 第 3 轮：询问预算
    reply = chat(sessions, "s1", "预算20万左右")
    assert "询问预算" in reply
    assert session.cxt.current_node_code == "buy_ask_city"
    assert session.cxt.filled_slots["budget"] == "预算20万左右"

    # 第 4 轮：询问城市
    reply = chat(sessions, "s1", "北京")
    assert "询问城市" in reply
    assert session.cxt.current_node_code == "buy_confirm"
    assert session.cxt.filled_slots["city"] == "北京"

    # 第 5 轮：确认节点（is_end），无后续节点则停留
    reply = chat(sessions, "s1", "好的，就这些")
    assert "确认购车信息" in reply
    assert session.cxt.current_module_code == "car_sales_buy"
    assert session.cxt.current_node_code == "buy_confirm"

    # 槽位贯穿始终
    assert session.cxt.filled_slots == {
        "brand": "比亚迪",
        "budget": "预算20万左右",
        "city": "北京",
    }


def test_route_to_after_fsm_flow(pattern, sessions):
    """售后意图：路由分发 → 售后 FSM 子模块流程。"""
    session = launch(pattern, sessions)

    reply = chat(sessions, "s1", "我的车需要维修")
    assert "售后咨询" in reply
    assert session.cxt.current_module_code == "car_sales_after"
    assert session.cxt.current_node_code is None

    reply = chat(sessions, "s1", "发动机故障灯亮了")
    assert "询问问题类型" in reply
    assert session.cxt.current_node_code == "after_ask_vehicle"
    assert session.cxt.filled_slots["issue_type"] == "发动机故障灯亮了"

    reply = chat(sessions, "s1", "比亚迪汉 京A12345")
    assert "询问车辆信息" in reply
    assert session.cxt.current_node_code == "after_confirm"
    assert session.cxt.filled_slots["car_info"] == "比亚迪汉 京A12345"


def test_chitchat_stays_in_route_module(pattern, sessions):
    """闲聊意图：无 jump_module 的菜单节点，回复后重置回路由根节点。"""
    session = launch(pattern, sessions)

    reply = chat(sessions, "s1", "你好呀")
    assert "闲聊" in reply
    assert session.cxt.current_module_code == "car_sales_root"
    assert session.cxt.current_node_code == "route_root"

    # 下一轮仍可继续路由
    reply = chat(sessions, "s1", "我想买车")
    assert session.cxt.current_module_code == "car_sales_buy"


def test_unknown_intent_falls_back_to_root(pattern, sessions):
    """未知意图：NLU 返回空 next_node，回复来自根节点，状态保持在路由模块。"""
    session = launch(pattern, sessions)

    reply = chat(sessions, "s1", "今天天气怎么样")
    assert "路由根节点" in reply  # NLG 使用 root 节点
    assert session.cxt.current_module_code == "car_sales_root"
    assert session.cxt.current_node_code == "route_root"


def test_route_nlu_parse_failure_retries_and_recovers(pattern, sessions):
    """RouteNLU 首次解析失败 → 重试成功 → 正常分发。"""
    session = launch(pattern, sessions)
    before = FakeProvider.call_count

    reply = chat(sessions, "s1", "解析失败重试 买车")
    # NLU 失败 + 重试 + NLG = 3 次调用
    assert FakeProvider.call_count - before == 3
    assert session.cxt.current_module_code == "car_sales_buy"
    assert "购车咨询" in reply


def test_route_nlu_parse_failure_exhausted_falls_back(pattern, sessions):
    """RouteNLU 重试后仍解析失败 → 兜底空意图，状态保持在根节点不崩溃。"""
    session = launch(pattern, sessions)
    before = FakeProvider.call_count

    reply = chat(sessions, "s1", "永远解析失败")
    assert FakeProvider.call_count - before == 3  # NLU + 重试 + NLG
    assert session.cxt.current_module_code == "car_sales_root"
    assert session.cxt.current_node_code == "route_root"
    assert isinstance(reply, str) and reply  # 有兜底回复，不抛异常


# ============================================================================
# 防回归：FAKE_PROVIDER_CODE 可正常注册（供 API 测试复用）
# ============================================================================

def test_fake_provider_registered():
    from src.llm import registry as llm_registry

    assert llm_registry.is_registered(FAKE_PROVIDER_CODE)
