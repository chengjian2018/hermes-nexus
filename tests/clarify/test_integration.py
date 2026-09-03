"""偏题轮全链路集成测试 —— FakeProvider + 内存知识库。"""

import pytest

from fake_provider import fake_llm_config, register_fake_provider

from src.chat.chat import chat as chat_fn
from src.chat.session import Session
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


def test_off_topic_turn_routes_kb_and_keeps_node():
    """偏题轮：kb 应答 + 拉回；节点不动、槽位不污染；下一轮恢复正常。"""
    from src.dialogue.register import discover_builtin_patterns, registry

    discover_builtin_patterns()
    pattern = registry.get("car_sales_route")

    session = Session(session_id="it", pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = {}
    session.cxt.metadata["llm_override"] = fake_llm_config()

    # 给购车 FSM 模块开启澄清（测试注入，不改产品 pattern 定义）
    buy = pattern.module_map["car_sales_buy"]
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
    assert session.cxt.current_module_code == "car_sales_buy"
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
