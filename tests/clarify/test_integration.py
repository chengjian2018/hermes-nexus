"""偏题轮全链路集成测试 —— FakeProvider + 内存知识库。

Pattern 为内联构建，节点 code/name 与 fake_provider 脚本约定保持一致
（路由根节点 / menu_sales / 询问品牌 …）。
"""

import pytest

from fake_provider import fake_llm_config, register_fake_provider

from src.chat.chat import chat as chat_fn
from src.chat.session import Session
from src.dialogue.module import FSMModule, RouteModule
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern
from src.dialogue.recaller import (
    KeywordRecallPath,
    MultiPathRecaller,
    ScoreThresholdFilter,
    WeightedScoreFusion,
)
from src.clarify import ClarifyRouteRule, ClarifyStage


@pytest.fixture(scope="module", autouse=True)
def _fake_provider():
    register_fake_provider()


KB_DOCS = [
    {"id": "fee_policy", "content": "除车价外仅收取上牌费与服务费，无其他收费",
     "metadata": {"keywords": ["收费", "服务费", "上牌费"]}},
]


@pytest.fixture()
def pattern():
    """内联 route + FSM pattern（ROUTE 路由 + 购车 FSM 子模块）。"""
    return Pattern(
        code="clarify_demo",
        name="澄清集成测试 pattern",
        description="路由 + 购车 FSM（内联测试 fixture）",
        entry_module_code="demo_root",
        modules=[
            RouteModule(
                module_code="demo_root",
                module_name="总路由",
                module_description="顶层路由",
                module_todo_description="意图分发",
                module_nodes=[
                    BaseNode(
                        node_code="route_root",
                        node_name="路由根节点",
                        node_description="总入口",
                        node_todo_description="意图分类",
                        sub_nodes=["menu_sales"],
                    ),
                    BaseNode(
                        node_code="menu_sales",
                        node_name="购车咨询",
                        node_description="购车入口",
                        node_todo_description="跳转到购车子模块",
                        sub_nodes=[],
                        jump_module="demo_buy",
                    ),
                ],
            ),
            FSMModule(
                module_code="demo_buy",
                module_name="购车流程",
                module_description="品牌 → 预算 → 城市 → 确认",
                module_todo_description="收集购车信息",
                module_nodes=[
                    BaseNode(
                        node_code="buy_ask_brand",
                        node_name="询问品牌",
                        node_description="收集品牌",
                        node_todo_description="抽取 brand 槽位",
                        node_slots={"brand": "品牌"},
                        sub_nodes=["buy_ask_budget"],
                    ),
                    BaseNode(
                        node_code="buy_ask_budget",
                        node_name="询问预算",
                        node_description="收集预算",
                        node_todo_description="抽取 budget 槽位",
                        node_slots={"budget": "预算"},
                        sub_nodes=["buy_ask_city"],
                    ),
                    BaseNode(
                        node_code="buy_ask_city",
                        node_name="询问城市",
                        node_description="收集城市",
                        node_todo_description="抽取 city 槽位",
                        node_slots={"city": "城市"},
                        sub_nodes=["buy_confirm"],
                    ),
                    BaseNode(
                        node_code="buy_confirm",
                        node_name="确认购车信息",
                        node_description="最终确认",
                        node_todo_description="结束流程",
                        sub_nodes=[],
                        is_end=True,
                    ),
                ],
            ),
        ],
    )


def test_off_topic_turn_routes_kb_and_keeps_node(pattern):
    """偏题轮：kb 应答 + 拉回；节点不动、槽位不污染；下一轮恢复正常。"""
    session = Session(session_id="it", pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = {}
    session.cxt.metadata["llm_override"] = fake_llm_config()

    # 给购车 FSM 模块开启澄清（测试注入，不改 fixture 定义）
    buy = pattern.module_map["demo_buy"]
    buy.enable_clarify = True
    buy.clarify_stage = ClarifyStage(
        recaller=MultiPathRecaller(
            recall_paths=[KeywordRecallPath(name="kb", documents=KB_DOCS)],
            filters=[ScoreThresholdFilter(threshold=0.1)],
            fusion=WeightedScoreFusion(),
        ),
        rule=ClarifyRouteRule(),
    )

    sessions = {"it": session}

    # 第 1 轮：路由静默分发，buy FSM 首节点 buy_ask_brand 同轮消化该句，
    # brand 槽位 = 整句 query，节点推进到 buy_ask_budget
    r1 = chat_fn("我想买车", "it", sessions)
    assert session.cxt.current_module_code == "demo_buy"
    assert session.cxt.current_node_code == "buy_ask_budget"
    assert session.cxt.filled_slots.get("brand") == "我想买车"
    assert "询问品牌" in r1  # FSMNLG 用转移前节点生成回复

    # 第 2 轮：偏题（应询问预算时反问收费）
    r3 = chat_fn("还要收别的钱吗", "it", sessions)
    clarify_info = session.cxt.metadata["clarify"]
    assert clarify_info["triggered"] is True
    assert clarify_info["mode"] == "kb"
    assert "上牌费与服务费" in r3                    # kb 应答
    assert "预算" in r3                              # 拉回主线
    assert session.cxt.current_node_code == "buy_ask_budget"   # 节点不动
    assert "topic" not in session.cxt.filled_slots   # 澄清槽位未污染
    assert session.cxt.filled_slots.get("brand") == "我想买车"  # 业务槽位保留

    # 第 3 轮：恢复正常（回答预算）
    r4 = chat_fn("20万左右", "it", sessions)
    assert session.cxt.metadata["clarify"]["triggered"] is False   # 元数据已重置
    assert session.cxt.current_node_code == "buy_ask_city"
    assert session.cxt.filled_slots["budget"] == "20万左右"
