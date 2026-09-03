"""xianyu_agent（ROUTE 模式）离线测试 —— 复刻 xianyu-auto-reply agent 对话管理。

通过脚本化 FakeProvider 模拟 LLM 输出（不访问真实 API），覆盖：
1. Pattern 结构与 AST 自动发现注册
2. 本地意图检测关键词表（price/tech/default，复刻 detect_intent）
3. 意图路由：议价/技术/通用 → 对应菜单节点，轮末回 root
4. 议价轮数控制：第 max_bargain_rounds 次砍价起固定拒绝话术且零 LLM
5. 议价参数注入：bargain_count/max_* 随 slots 进入 filled_slots 供 NLG
6. 自定义议价设置：metadata.bargain_settings 覆盖默认值
"""

import logging

import pytest

from fake_provider import (
    FakeProvider,
    fake_llm_config,
    register_fake_provider,
)

logging.basicConfig(level=logging.WARNING)

REFUSE_TEXT = "抱歉，这个价格已经是最优惠的了，不能再便宜了哦！"


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="session", autouse=True)
def _fake_provider():
    """注册脚本化 provider，测试全程复用。"""
    register_fake_provider()


@pytest.fixture(scope="module")
def pattern():
    """发现内置 pattern 并返回 xianyu_agent。"""
    from src.dialogue.register import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "src.dialogue.xianyu_agent_route" in imported, (
        f"xianyu_agent_route 未被自动发现，已发现: {imported}"
    )
    return registry.get("xianyu_agent")


@pytest.fixture()
def sessions():
    """每次测试独立的会话容器。"""
    return {}


def launch(pattern, sessions, session_id="s1", bargain_settings=None):
    """模拟 main.py 的 launch 流程：注册会话并注入管线上下文。"""
    from src.chat.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = {
        "channel": "xianyu", "account_id": "acc1", "item_id": "item1",
    }
    if bargain_settings is not None:
        session.cxt.metadata["bargain_settings"] = bargain_settings
    session.cxt.metadata["llm_override"] = fake_llm_config()
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
    """Pattern 可被 AST 自动发现，模块/节点结构与 ROUTE 语义正确。"""
    from src.dialogue.module import ModuleType

    assert pattern.code == "xianyu_agent"
    assert pattern.entry_module_code == "xianyu_root"
    assert set(pattern.module_map) == {"xianyu_root"}

    root = pattern.module_map["xianyu_root"]
    assert root.type == ModuleType.ROUTE

    # 路由模块节点顺序：root 必须位于 module_nodes[0]（首节点）
    assert [n.node_code for n in root.module_nodes] == [
        "xy_route_root", "xy_menu_price", "xy_menu_price_refuse",
        "xy_menu_tech", "xy_menu_default",
    ]

    # 意图菜单全部无 jump_module：留在路由模块，每轮回 root
    for node in root.module_nodes[1:]:
        assert not getattr(node, "jump_module", None)

    # 意图菜单节点挂了意图级 NLG 模板（拒绝节点除外：走固定话术）
    assert pattern.node_map["xy_menu_price"].base_nlg_prompt
    assert pattern.node_map["xy_menu_tech"].base_nlg_prompt
    assert pattern.node_map["xy_menu_default"].base_nlg_prompt


def test_generate_wired_at_module_level(pattern):
    """XianyuIntentNLU / FixedNLG 挂在模块级 generate dict（nlu/nlg 位）。"""
    from src.dialogue.xianyu_agent_route import FixedNLG, XianyuIntentNLU

    root = pattern.module_map["xianyu_root"]
    generate = root.generate
    assert isinstance(generate, dict)
    assert isinstance(generate["nlu"], XianyuIntentNLU)
    assert isinstance(generate["nlg"], FixedNLG)


def test_query_slot_wired_with_time_aug(pattern):
    """pattern 级 query 槽位配置 TimeAugQueryRewriter（时间增强改写）。"""
    from src.dialogue.query import TimeAugQueryRewriter

    assert isinstance(pattern.query, TimeAugQueryRewriter)


# ============================================================================
# 意图检测测试（复刻 detect_intent 关键词表）
# ============================================================================

@pytest.mark.parametrize("query,intent", [
    ("能便宜点吗", "price"),
    ("多少钱", "price"),
    ("可以刀一点吗", "price"),
    ("包个邮吧", "price"),
    ("最低什么价", "price"),
    ("这个怎么用", "tech"),
    ("有什么功能", "tech"),
    ("参数发一下", "tech"),
    ("在吗", "default"),
    ("今天发货吗", "default"),
    ("HELLO 在吗", "default"),  # lower() 后匹配，非关键词仍 default
])
def test_detect_intent_keywords(query, intent):
    """本地关键词意图检测与原实现关键词表一致。"""
    from src.dialogue.xianyu_agent_route import detect_intent

    assert detect_intent(query) == intent


# ============================================================================
# 意图路由测试
# ============================================================================

def test_intent_routing_each_turn(pattern, sessions):
    """三类意图各自路由到对应菜单节点，轮末回 root（每轮独立检测）。"""
    session = launch(pattern, sessions)

    chat(sessions, "s1", "能便宜点吗")
    assert session.cxt.nlu_result["next_node"] == "xy_menu_price"
    assert session.cxt.nlu_result["intent"] == "price"
    assert session.cxt.current_node_code == "xy_route_root"  # 轮末回 root

    chat(sessions, "s1", "这个怎么用")
    assert session.cxt.nlu_result["next_node"] == "xy_menu_tech"

    chat(sessions, "s1", "在吗")
    assert session.cxt.nlu_result["next_node"] == "xy_menu_default"


def test_intent_metadata_written_for_counting(pattern, sessions):
    """每轮 user 消息回填 intent metadata，供议价计数回溯。"""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "多少钱")
    user_msgs = [m for m in session.cxt.history if m.role == "user"]
    assert user_msgs[-1].metadata.get("intent") == "price"

    chat(sessions, "s1", "怎么下载驱动")
    user_msgs = [m for m in session.cxt.history if m.role == "user"]
    assert user_msgs[-1].metadata.get("intent") == "tech"


# ============================================================================
# 议价轮数控制测试
# ============================================================================

def test_bargain_refuse_at_threshold_zero_llm(pattern, sessions):
    """第 max_bargain_rounds 次砍价起：固定拒绝话术 + 零 LLM 调用。"""
    session = launch(pattern, sessions)
    queries = ["能便宜点吗", "还能再少点", "最低多少钱", "再刀50"]

    llm_calls = []
    for i, q in enumerate(queries, 1):
        before = FakeProvider.call_count
        reply = chat(sessions, "s1", q)
        llm_calls.append(FakeProvider.call_count - before)

        if i < 3:
            # 前两次：正常议价节点，单次 LLM 生成
            assert session.cxt.nlu_result["next_node"] == "xy_menu_price"
            assert llm_calls[-1] == 1
        else:
            # 第 3/4 次：count >= max(3) → 固定拒绝，零 LLM
            assert session.cxt.nlu_result["next_node"] == "xy_menu_price_refuse"
            assert reply == REFUSE_TEXT
            assert llm_calls[-1] == 0

    assert session.cxt.nlu_result["slots"]["bargain_count"] == 4


def test_bargain_count_persists_across_interleaved_intents(pattern, sessions):
    """议价计数跨轮持久：中间穿插非议价消息不重置计数。"""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "能便宜点吗")     # price #1
    chat(sessions, "s1", "这个怎么用")     # tech（不计数）
    chat(sessions, "s1", "还能再少点")     # price #2
    chat(sessions, "s1", "在吗")           # default（不计数）
    reply = chat(sessions, "s1", "最低多少钱")  # price #3 → 拒绝

    assert session.cxt.nlu_result["slots"]["bargain_count"] == 3
    assert reply == REFUSE_TEXT


def test_custom_bargain_settings(pattern, sessions):
    """metadata.bargain_settings 覆盖默认议价设置（max=1 → 第 1 次即拒绝）。"""
    session = launch(pattern, sessions,
                     bargain_settings={"max_bargain_rounds": 1})
    reply = chat(sessions, "s1", "能便宜点吗")

    assert session.cxt.nlu_result["next_node"] == "xy_menu_price_refuse"
    assert reply == REFUSE_TEXT


def test_bargain_params_injected_into_slots(pattern, sessions):
    """议价参数（count/max_*）随 slots 合并进 filled_slots，供 NLG 模板注入。"""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "能便宜点吗")

    assert session.cxt.filled_slots["bargain_count"] == 1
    assert session.cxt.filled_slots["max_bargain_rounds"] == 3
    assert session.cxt.filled_slots["max_discount_percent"] == 10
    assert session.cxt.filled_slots["max_discount_amount"] == 100


def test_non_price_intent_no_bargain_params(pattern, sessions):
    """非议价意图 bargain_count=0，参数仍注入（模板可统一引用）。"""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "这个怎么用")

    assert session.cxt.nlu_result["intent"] == "tech"
    assert session.cxt.filled_slots["bargain_count"] == 0


# ============================================================================
# Prompt 组装测试
# ============================================================================

def test_price_prompt_contains_bargain_context(pattern, sessions):
    """议价 NLG prompt 含商品信息/历史/议价设置/买家消息四要素。"""
    session = launch(pattern, sessions)

    captured = {}
    from src.dialogue.nlg import BaseNLG
    original = BaseNLG._call_llm

    def spy(self, prompt, llm_config=None):
        captured["prompt"] = prompt
        return original(self, prompt, llm_config)

    BaseNLG._call_llm = spy
    try:
        chat(sessions, "s1", "能便宜点吗")
    finally:
        BaseNLG._call_llm = original

    prompt = captured["prompt"]
    assert "议价" in prompt
    assert "商品信息" in prompt
    assert "item_id: item1" in prompt          # task_info 商品信息注入
    assert "对话历史" in prompt
    assert "议价设置" in prompt
    assert "bargain_count" in prompt           # 议价参数注入
    assert "能便宜点吗" in prompt              # 买家消息


def test_intent_specific_prompt_selected(pattern, sessions):
    """技术意图走 tech 模板（含"技术专家"人设），通用走 default 模板。"""
    session = launch(pattern, sessions)

    from src.dialogue.nlg import BaseNLG
    original = BaseNLG._call_llm
    captured = []

    def spy(self, prompt, llm_config=None):
        captured.append(prompt)
        return original(self, prompt, llm_config)

    BaseNLG._call_llm = spy
    try:
        chat(sessions, "s1", "这个怎么用")   # tech
        chat(sessions, "s1", "今天发货吗")   # default
    finally:
        BaseNLG._call_llm = original

    assert "技术专家" in captured[0]
    assert "电商卖家" in captured[1]


# ============================================================================
# 时间增强改写贯通测试（query 槽位 → NLU/NLG 消费增强后消息）
# ============================================================================

def test_time_augmented_query_flows_into_prompt(pattern, sessions):
    """含相对时间的买家消息经 TimeAugQueryRewriter 增强后进入 NLG prompt。

    注入固定 time_base（2026-09-03 10:00:00，周四）→ "明天下午3点前"
    增强带绝对时间标注（jionlp 区间解析，含次日 2026-09-04）。
    default 意图走 LLM 兜底，FakeProvider 返回非标签文本回落 default
    菜单 → FixedNLG 单次 LLM。
    """
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-03 10:00:00", "%Y-%m-%d %H:%M:%S"))

    from src.dialogue.nlg import BaseNLG
    original = BaseNLG._call_llm
    captured = {}

    def spy(self, prompt, llm_config=None):
        captured["prompt"] = prompt
        return original(self, prompt, llm_config)

    BaseNLG._call_llm = spy
    try:
        chat(sessions, "s1", "明天下午3点前能发货吗")
    finally:
        BaseNLG._call_llm = original

    # 改写结果进 ctx 与 NLG prompt（增强标注含解析出的绝对时间）
    assert session.cxt.rewritten_queries[0] != "明天下午3点前能发货吗"
    assert "2026-09-04" in session.cxt.rewritten_queries[0]
    assert session.cxt.rewritten_queries[0] in captured["prompt"]
