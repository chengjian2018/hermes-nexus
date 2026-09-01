"""统一阶段（单次调用 + structured output）离线测试。

通过脚本化 FakeProvider 模拟 LLM 输出（不访问真实 API），覆盖：
1. Pattern 自动发现 + module 级统一阶段注入（ROUTE / FSM 双形态）
2. 端到端流程：每轮恰好 1 次 LLM 调用（两阶段为 2 次）、跳转与槽位正确
3. prompt 装配：候选节点带回答范式、next_node 合法取值列表
4. 非法 next_node 代码级硬 guard（保持当前节点，回复保留）
5. 解析失败重试成功 / 重试耗尽兜底不崩溃
6. PassThroughNLG 保留已生成回复
7. 双轨澄清组合：偏题轮 clarify 信号 → kb 应答 + 拉回（澄清轮 2 次调用，
   正常轮仍 1 次）；未开澄清模块的 clarify 信号被合法集硬 guard 拒绝
"""

import logging

import pytest

from fake_provider import (
    FakeProvider,
    fake_llm_config,
    register_fake_provider,
)

logging.basicConfig(level=logging.WARNING)


# ============================================================================
# Fixtures & helpers（与 test_car_sales_route.py 同构）
# ============================================================================

@pytest.fixture(scope="session", autouse=True)
def _fake_provider():
    """注册脚本化 provider，测试全程复用。"""
    register_fake_provider()


@pytest.fixture(scope="module")
def pattern():
    """发现内置 pattern 并返回 car_sales_unified。"""
    from src.dialogue.register import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "src.dialogue.car_sales_unified_route" in imported, (
        f"car_sales_unified_route 未被自动发现，已发现: {imported}"
    )
    return registry.get("car_sales_unified")


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


def chat_once(pattern, sessions, query):
    """处理一轮对话并断言恰好消耗 1 次 LLM 调用（统一阶段核心收益）。"""
    before = FakeProvider.call_count
    reply = chat(sessions, "s1", query)
    assert FakeProvider.call_count - before == 1, (
        f"统一阶段每轮应恰好 1 次 LLM 调用，实际 {FakeProvider.call_count - before} 次"
    )
    return reply


# ============================================================================
# 结构与装配测试
# ============================================================================

def test_pattern_discovered_and_stage_wiring(pattern):
    """Pattern 可被 AST 自动发现；ROUTE/FSM 模块均注入统一阶段。"""
    from src.dialogue.module import ModuleType
    from src.dialogue.unified import (
        FSMUnifiedNLU,
        PassThroughNLG,
        RouteUnifiedNLU,
    )

    assert pattern.code == "car_sales_unified"
    assert pattern.entry_module_code == "unified_root"

    root_module = pattern.module_map["unified_root"]
    buy_module = pattern.module_map["unified_buy"]
    assert root_module.type == ModuleType.ROUTE
    assert buy_module.type == ModuleType.FSM

    # module 级统一阶段注入（node > module > default 优先级中的 module 层）
    assert isinstance(root_module.nlu_stage, RouteUnifiedNLU)
    assert isinstance(root_module.nlg_stage, PassThroughNLG)
    assert isinstance(buy_module.nlu_stage, FSMUnifiedNLU)
    assert isinstance(buy_module.nlg_stage, PassThroughNLG)

    # 路由结构与菜单分发
    assert pattern.node_map["u_route_root"].sub_nodes == [
        "u_menu_sales", "u_menu_chitchat",
    ]
    assert pattern.node_map["u_menu_sales"].jump_module == "unified_buy"
    assert not hasattr(pattern.node_map["u_menu_chitchat"], "jump_module")

    # FSM 节点链与终节点
    assert pattern.node_map["u_ask_brand"].sub_nodes == ["u_ask_budget"]
    assert pattern.node_map["u_ask_budget"].sub_nodes == ["u_confirm"]
    assert pattern.node_map["u_confirm"].is_end is True


def test_prompt_embeds_candidates_and_valid_values(pattern):
    """统一阶段 prompt 携带候选节点回答范式与 next_node 合法取值。"""
    from src.dialogue.base import DialogueContext
    from src.dialogue.unified import FSMUnifiedNLU

    ctx = DialogueContext(session_id="s-prompt", user_query="比亚迪")
    ctx.module_map = pattern.module_map
    ctx.node_map = pattern.node_map
    ctx.current_module_code = "unified_buy"
    ctx.current_node_code = "u_ask_brand"

    prompt = FSMUnifiedNLU().prompt_build(ctx)

    # 候选节点完整信息：编码 + 名称 + 槽位定义 + 回答范式
    assert "u_ask_budget" in prompt
    assert "询问预算" in prompt
    assert "预算区间" in prompt  # 候选节点槽位定义
    assert "回答范式" in prompt  # 候选节点回答范式被带出
    # 当前节点回答范式（保持当前节点时使用）
    assert "汽车品牌" in prompt
    # next_node 合法取值列表（JSON 数组，含空串与候选编码）
    assert '"u_ask_budget"' in prompt
    assert '""' in prompt


# ============================================================================
# 端到端流程测试（每轮单次调用）
# ============================================================================

def test_route_then_fsm_full_flow_single_call_per_turn(pattern, sessions):
    """购车流程全链路：路由分发 → 品牌 → 预算 → 确认，每轮恰好 1 次调用。"""
    session = launch(pattern, sessions)

    # 第 1 轮：顶层路由（统一阶段一次产出意图分类 + 菜单回复），分发到购车子模块
    reply = chat_once(pattern, sessions, "我想买车，看看有什么车型")
    assert "购车菜单" in reply, f"回复应来自统一阶段的菜单话术，实际: {reply!r}"
    assert session.cxt.current_module_code == "unified_buy"
    assert session.cxt.current_node_code is None  # 下一轮从子模块首节点开始

    # 第 2 轮：FSM 首节点询问品牌，一次产出 brand 槽位 + 预算引导话术 + 跳转
    reply = chat_once(pattern, sessions, "比亚迪")
    assert "询问预算" in reply
    assert session.cxt.current_node_code == "u_ask_budget"
    assert session.cxt.filled_slots["brand"] == "比亚迪"

    # 第 3 轮：询问预算
    reply = chat_once(pattern, sessions, "预算20万左右")
    assert "确认购车信息" in reply
    assert session.cxt.current_node_code == "u_confirm"
    assert session.cxt.filled_slots["budget"] == "预算20万左右"

    # 第 4 轮：终节点，next_node 为空保持不动
    reply = chat_once(pattern, sessions, "好的，没问题")
    assert "确认购车信息" in reply
    assert session.cxt.current_node_code == "u_confirm"

    # 槽位贯穿始终；统一阶段观测元数据写入
    assert session.cxt.filled_slots == {
        "brand": "比亚迪",
        "budget": "预算20万左右",
    }
    assert session.cxt.metadata["unified"]["triggered"] is True
    assert "reply" in session.cxt.metadata["unified"]


def test_chitchat_stays_route_root(pattern, sessions):
    """闲聊意图：单次调用回复后重置回根节点，下一轮仍可正常路由。"""
    session = launch(pattern, sessions)

    reply = chat_once(pattern, sessions, "你好呀")
    assert "闲聊菜单" in reply
    assert session.cxt.current_module_code == "unified_root"
    assert session.cxt.current_node_code == "u_route_root"

    # 下一轮仍可路由到购车子模块
    reply = chat_once(pattern, sessions, "我想买车")
    assert session.cxt.current_module_code == "unified_buy"


# ============================================================================
# 硬 guard 与降级测试
# ============================================================================

def test_invalid_next_node_guarded(pattern, sessions):
    """模型输出非法 next_node：代码级 guard 保持当前节点，回复保留。"""
    session = launch(pattern, sessions)

    reply = chat_once(pattern, sessions, "跳到不存在节点")

    # 非法转移边被拒绝：节点保持在根节点
    assert session.cxt.current_module_code == "unified_root"
    assert session.cxt.current_node_code == "u_route_root"
    assert session.cxt.nlu_result["next_node"] == ""
    # 回复保留（仍返回给用户），观测元数据记录非法取值
    assert "非法节点" in reply
    assert session.cxt.metadata["unified"]["invalid_next_node"] == "not_exist_node"


def test_parse_failure_retry_recovers(pattern, sessions):
    """首次输出非 JSON → 重试修正成功 → 正常分发（共 2 次调用）。"""
    session = launch(pattern, sessions)
    before = FakeProvider.call_count

    reply = chat(sessions, "s1", "解析失败重试 买车")

    assert FakeProvider.call_count - before == 2  # 失败 + 重试
    assert session.cxt.current_module_code == "unified_buy"
    assert "购车菜单" in reply


def test_parse_failure_exhausted_falls_back(pattern, sessions):
    """重试后仍解析失败 → 兜底回复 + 保持当前节点，不抛异常。"""
    from src.dialogue.unified import FSMUnifiedNLU

    session = launch(pattern, sessions)
    before = FakeProvider.call_count

    reply = chat(sessions, "s1", "永远解析失败")

    assert FakeProvider.call_count - before == 2  # 失败 + 重试耗尽
    assert reply == FSMUnifiedNLU.fallback_reply
    assert session.cxt.metadata["unified"]["parse_failed"] is True
    assert session.cxt.nlu_result == {"next_node": "", "slots": {}}
    assert session.cxt.current_module_code == "unified_root"
    assert session.cxt.current_node_code == "u_route_root"


def test_pass_through_nlg_keeps_existing_result():
    """PassThroughNLG：有已生成回复时原样保留；缺失时置空并告警不崩溃。"""
    from src.dialogue.base import DialogueContext
    from src.dialogue.unified import PassThroughNLG

    stage = PassThroughNLG()

    ctx = DialogueContext(session_id="s-1", user_query="q")
    ctx.nlg_result = {"content": "已生成的回复"}
    ctx = stage.execute(ctx)
    assert ctx.nlg_result == {"content": "已生成的回复"}

    ctx_empty = DialogueContext(session_id="s-2", user_query="q")
    ctx_empty = stage.execute(ctx_empty)
    assert ctx_empty.nlg_result == {"content": ""}


# ============================================================================
# 双轨澄清组合测试（enable_clarify=True 的 FSM 模块）
# ============================================================================

def test_clarify_next_node_rejected_when_disabled(pattern, sessions):
    """未开澄清的模块输出 clarify 信号：合法集硬 guard 回落为保持当前节点。

    模型 reply 是"帮您确认"类承接承诺，但模块未装配澄清环节不会兑现，
    因此回复一并替换为兜底话术（避免空承诺）。
    """
    from src.dialogue.unified import FSMUnifiedNLU

    session = launch(pattern, sessions)

    reply = chat_once(pattern, sessions, "硬造澄清意图")

    assert session.cxt.current_module_code == "unified_root"
    assert session.cxt.current_node_code == "u_route_root"
    assert session.cxt.nlu_result["next_node"] == ""
    assert session.cxt.metadata["unified"]["invalid_next_node"] == "clarify"
    assert reply == FSMUnifiedNLU.fallback_reply  # 空承诺回复被兜底替换
    assert "clarify" not in session.cxt.metadata


def test_unified_with_clarify_off_topic_turn(pattern, sessions):
    """统一阶段 + 双轨澄清：偏题轮 kb 应答 + 拉回，节点不动、槽位不污染。

    管线为 [FSMUnifiedNLU, ClarifyStage, PassThroughNLG]：
    澄清轮 = 统一调用 + 澄清生成共 2 次 LLM 调用（与两阶段+澄清持平），
    正常轮仍为 1 次。
    """
    from src.clarify import ClarifyRouteRule, ClarifyStage
    from src.dialogue.recaller import (
        KeywordRecallPath,
        MultiPathRecaller,
        ScoreThresholdFilter,
        WeightedScoreFusion,
    )

    kb_docs = [
        {
            "id": "fee_policy",
            "content": "除车价外仅收取上牌费与服务费，无其他收费",
            "metadata": {"keywords": ["收费", "服务费", "上牌费"]},
        },
    ]

    # 测试注入：给购车子模块开澄清（测完恢复，不污染同文件其他用例）
    buy = pattern.module_map["unified_buy"]
    saved_flag = getattr(buy, "enable_clarify", False)
    saved_stage = getattr(buy, "clarify_stage", None)
    buy.enable_clarify = True
    buy.clarify_stage = ClarifyStage(
        recaller=MultiPathRecaller(
            recall_paths=[KeywordRecallPath(name="kb", documents=kb_docs)],
            filters=[ScoreThresholdFilter(threshold=0.1)],
            fusion=WeightedScoreFusion(),
        ),
        rule=ClarifyRouteRule(),
    )

    try:
        session = launch(pattern, sessions)

        # 第 1 轮：路由进入购车子模块
        chat_once(pattern, sessions, "我想买车")
        assert session.cxt.current_module_code == "unified_buy"

        # 第 2 轮：品牌 → 询问预算节点（正常轮 1 次调用）
        chat_once(pattern, sessions, "比亚迪")
        assert session.cxt.current_node_code == "u_ask_budget"

        # 第 3 轮：偏题（应问预算时反问收费）→ 统一阶段发 clarify 信号，
        # ClarifyStage 覆写回复为 kb 应答 + 拉回
        before = FakeProvider.call_count
        reply = chat(sessions, "s1", "还要收别的钱吗")
        assert FakeProvider.call_count - before == 2  # 统一 + 澄清生成

        clarify_info = session.cxt.metadata["clarify"]
        assert clarify_info["triggered"] is True
        assert clarify_info["mode"] == "kb"
        assert "上牌费与服务费" in reply  # kb 应答
        assert "预算" in reply  # 拉回主线
        assert session.cxt.current_node_code == "u_ask_budget"  # 节点不动
        assert "topic" not in session.cxt.filled_slots  # 澄清槽位未污染
        assert session.cxt.filled_slots.get("brand") == "比亚迪"  # 业务槽位保留

        # 第 4 轮：恢复正常（回答预算）→ 澄清元数据重置，流程继续推进
        reply = chat_once(pattern, sessions, "20万左右")
        assert session.cxt.metadata["clarify"]["triggered"] is False
        assert session.cxt.current_node_code == "u_confirm"
        assert session.cxt.filled_slots["budget"] == "20万左右"
    finally:
        buy.enable_clarify = saved_flag
        buy.clarify_stage = saved_stage
