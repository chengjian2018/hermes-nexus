"""脚本化 FakeProvider —— 离线测试共用的伪 LLM provider（不访问真实 API）。

按 prompt 内容区分 统一阶段 / NLU / NLG / 重试 四类请求并返回固定结果，
供 route pattern 的逻辑测试与 API 测试复用。
"""

import json

from src.llm import registry as llm_registry
from src.llm.provider import BaseLLMProvider

FAKE_PROVIDER_CODE = "fake_test_provider"


class FakeProvider(BaseLLMProvider):
    """Offline scripted LLM provider."""

    call_count = 0

    def _chat_completion_impl(
        self,
        messages,
        model,
        temperature,
        max_tokens,
        stream=False,
        **kwargs,
    ):
        type(self).call_count += 1
        prompt = messages[0]["content"]
        return {"content": scripted_response(prompt)}


def register_fake_provider() -> None:
    """向 LLM 注册中心注册脚本化 provider（幂等）。"""
    if not llm_registry.is_registered(FAKE_PROVIDER_CODE):
        llm_registry.register(
            code=FAKE_PROVIDER_CODE,
            name="FakeProvider",
            description="offline scripted provider for route pattern tests",
            provider_class=FakeProvider,
            default_model="fake-model",
        )


def fake_llm_config() -> dict:
    """返回使用 FakeProvider 的 llm_config。"""
    return {
        "code": FAKE_PROVIDER_CODE,
        "model": "fake-model",
        "temperature": 0.7,
        "max_tokens": 512,
    }


# ============================================================================
# 脚本化响应逻辑
# ============================================================================

def _extract_node_name(prompt: str) -> str:
    """从 NLU/NLG prompt 的当前节点信息中提取节点名称。"""
    for line in prompt.split("\n"):
        line = line.strip()
        if line.startswith("节点名称:"):
            return line.split(":", 1)[1].strip()
    return ""


def _extract_query(prompt: str) -> str:
    """从 prompt 的「用户输入」段落提取第一行作为用户 query。"""
    marker = "### 用户输入"
    idx = prompt.find(marker)
    if idx == -1:
        return ""
    segment = prompt[idx + len(marker):]
    lines = [l.strip() for l in segment.split("\n") if l.strip()]
    return lines[0] if lines else ""


def _route_nlu(query: str, retry: bool) -> str:
    """路由根节点的脚本化意图分类结果。"""
    if "解析失败重试" in query and not retry:
        return "这不是合法的 JSON 输出"  # 触发第一次解析失败
    if "永远解析失败" in query:
        return "这不是合法的 JSON 输出"
    if any(k in query for k in ("售后", "维修", "保养", "投诉", "理赔")):
        return '{"next_node": "menu_after", "slots": {}}'
    if any(k in query for k in ("你好", "谢谢", "再见", "早上好")):
        return '{"next_node": "menu_chitchat", "slots": {}}'
    if any(k in query for k in ("买车", "购车", "试驾", "看车", "询价", "车型")):
        return '{"next_node": "menu_sales", "slots": {}}'
    return '{"next_node": "", "slots": {}}'  # 未知意图兜底


def _fsm_nlu(node_name: str, query: str) -> str:
    """FSM 节点的脚本化意图/槽位抽取结果。"""
    # 偏题输入 → 澄清意图（固定槽位 topic/keywords）
    if any(k in query for k in ("收别的钱", "其他收费", "额外收费")):
        return json.dumps(
            {
                "next_node": "clarify",
                "slots": {"topic": "费用", "keywords": ["额外收费"]},
            },
            ensure_ascii=False,
        )
    mapping = {
        "询问品牌": {"next_node": "buy_ask_budget", "slots": {"brand": query}},
        "询问预算": {"next_node": "buy_ask_city", "slots": {"budget": query}},
        "询问城市": {"next_node": "buy_confirm", "slots": {"city": query}},
        "确认购车信息": {"next_node": "", "slots": {}},
        "询问问题类型": {
            "next_node": "after_ask_vehicle",
            "slots": {"issue_type": query},
        },
        "询问车辆信息": {
            "next_node": "after_confirm",
            "slots": {"car_info": query},
        },
        "确认售后信息": {"next_node": "", "slots": {}},
    }
    result = mapping.get(node_name, {"next_node": "", "slots": {}})
    return json.dumps(result, ensure_ascii=False)


def _unified(node_name: str, query: str, retry: bool) -> str:
    """统一阶段（单次调用 + structured output）的脚本化结果。

    输出协议：{"reply", "next_node", "slots"}，reply 内嵌目标节点名称便于断言。
    """
    if "解析失败重试" in query and not retry:
        return "这不是合法的 JSON 输出"  # 触发第一次解析失败
    if "永远解析失败" in query:
        return "这不是合法的 JSON 输出"
    if "跳到不存在节点" in query:
        # 模拟模型违反转移边约束，供代码级硬 guard 测试
        return json.dumps(
            {
                "reply": "统一回复: 非法节点",
                "next_node": "not_exist_node",
                "slots": {},
            },
            ensure_ascii=False,
        )
    if "硬造澄清意图" in query:
        # 模拟未开澄清的模块输出了 clarify 信号，供合法集硬 guard 测试
        return json.dumps(
            {
                "reply": "统一回复: 硬造澄清",
                "next_node": "clarify",
                "slots": {"topic": "费用", "keywords": ["硬造"]},
            },
            ensure_ascii=False,
        )

    # 偏题输入 → 澄清意图（固定槽位 topic/keywords，reply 简短承接）
    if any(k in query for k in ("收别的钱", "其他收费", "额外收费")):
        return json.dumps(
            {
                "reply": "统一承接: 这个问题我帮您确认一下",
                "next_node": "clarify",
                "slots": {"topic": "费用", "keywords": ["额外收费"]},
            },
            ensure_ascii=False,
        )

    # 路由根节点：意图分类到菜单
    if node_name == "统一路由根节点":
        if any(k in query for k in ("买车", "购车", "车型", "试驾", "询价")):
            return json.dumps(
                {
                    "reply": "统一回复: 购车菜单",
                    "next_node": "u_menu_sales",
                    "slots": {},
                },
                ensure_ascii=False,
            )
        if any(k in query for k in ("你好", "谢谢", "再见")):
            return json.dumps(
                {
                    "reply": "统一回复: 闲聊菜单",
                    "next_node": "u_menu_chitchat",
                    "slots": {},
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"reply": "统一回复: 统一路由根节点", "next_node": "", "slots": {}},
            ensure_ascii=False,
        )

    # FSM 节点：推进流程 + 槽位抽取，reply 为所选下一节点话术
    mapping = {
        "询问品牌": ("u_ask_budget", "brand", "统一回复: 询问预算"),
        "询问预算": ("u_confirm", "budget", "统一回复: 确认购车信息"),
        "确认购车信息": ("", None, "统一回复: 确认购车信息"),
    }
    next_node, slot_key, reply = mapping.get(
        node_name, ("", None, "统一回复: 未知节点")
    )
    slots = {slot_key: query} if slot_key else {}
    return json.dumps(
        {"reply": reply, "next_node": next_node, "slots": slots},
        ensure_ascii=False,
    )


def scripted_response(prompt: str) -> str:
    """按 prompt 类型返回脚本化 LLM 输出。"""
    node_name = _extract_node_name(prompt)

    # NLU 重试修正 prompt（含「修正要求」段落）→ 按协议返回正确格式
    if "修正要求" in prompt:
        query = _extract_query(prompt).replace("解析失败重试", "")
        if '"reply"' in prompt:
            return _unified(node_name, query, retry=True)
        return _route_nlu(query, retry=True)

    # 统一阶段 prompt：含 reply + next_node 的三字段 JSON 协议（先于 NLU 判定）
    if '"reply"' in prompt and '"next_node"' in prompt:
        return _unified(node_name, _extract_query(prompt), retry=False)

    # NLU prompt：含 next_node JSON 输出要求
    if '"next_node"' in prompt:
        query = _extract_query(prompt)
        if node_name == "路由根节点":
            return _route_nlu(query, retry=False)
        return _fsm_nlu(node_name, query)

    # 澄清 prompt（含「知识库召回内容」段落且非 NLU JSON 协议）→ 按模式返回
    if "知识库召回内容" in prompt and '"next_node"' not in prompt:
        if "召回内容为空或无相关内容" in prompt or "（无相关知识库内容）" in prompt:
            return "承接：该问题暂无法详细解答。请问您的预算大概是多少呢？"
        return "解答：除车价外仅收取上牌费与服务费。请问您的预算大概是多少呢？"

    # NLG prompt：回复文本带上当前节点名称，便于断言 NLG 使用了哪个节点
    if node_name:
        return f"回复: {node_name}"
    return "回复: 无当前节点"
