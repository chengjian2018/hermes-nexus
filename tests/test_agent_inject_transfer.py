"""run_agent：投影注入 / transfer 拦截 / tool 往返落盘测试。"""

import json
from unittest.mock import patch

from src.chat.session import Session
from src.dialogue.base import DialogueContext, SessionMessage
from src.dialogue.module import AgentModule, ModuleLink
from src.dialogue.pattern import Pattern
from src.tools.register import registry as tool_registry


# ---------------------------------------------------------------------------
# 中性 mock 工具：真实 workorder 工具由 Task 8 引入，此处用独立名避免冲突
# ---------------------------------------------------------------------------

def _mock_lent_tool_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "mock_lent_tool", "args": args},
                      ensure_ascii=False)


tool_registry.register(
    name="mock_lent_tool",
    toolset="test_lent",
    schema={
        "name": "mock_lent_tool",
        "description": "测试用借出工具",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "查询内容"}},
        },
    },
    handler=_mock_lent_tool_handler,
    allowed_patterns={"p": ["after_sales"]},
)


def _mk_session():
    after_sales = AgentModule(
        module_code="after_sales",
        module_name="售后维保",
        module_description="保养预约、维修工单办理",
        module_todo_description="查改保养预约",
        answer_examples=["已为您改到{时间}。"],
        use_tools=["mock_lent_tool"],
        sub_modules=["reception"],
    )
    reception = AgentModule(
        module_code="reception",
        module_name="前台接待",
        module_description="接待与分诊",
        sub_modules=[
            ModuleLink(target="after_sales",
                       lend_tools=["mock_lent_tool"]),
        ],
    )
    p = Pattern(code="p", name="t", description="t",
                entry_module_code="reception",
                modules=[reception, after_sales])
    s = Session(session_id="s", pattern_code="p")
    s.pattern = p
    s.cxt.module_map = p.module_map
    s.cxt.node_map = p.node_map
    s.cxt.current_module_code = "reception"
    s.cxt.metadata["dispatch_graph"] = p.dispatch_graph
    s.cxt.llm_config = {"code": "x", "model": "m"}
    return s


class ScriptedProvider:
    """按脚本依次返回响应；记录收到的 messages/tools 供断言。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat_completion(self, messages, model, temperature, max_tokens,
                        tools=None, tool_choice=None, **kw):
        self.seen.append({"messages": messages, "tools": tools})
        item = self.script.pop(0)
        return item


def test_projection_block_contains_knowledge_and_tools():
    from src.chat.loop import build_projection_block
    s = _mk_session()
    block = build_projection_block(
        s.cxt.module_map["reception"], s.cxt.module_map)
    assert "售后维保" in block
    assert "保养预约" in block
    assert "mock_lent_tool" in block   # 借出工具列在投影块


def test_transfer_tools_generated_per_link():
    from src.chat.loop import build_transfer_tools
    s = _mk_session()
    tools = build_transfer_tools(
        s.cxt.module_map["reception"], s.cxt.module_map)
    names = [t["function"]["name"] for t in tools]
    assert names == ["transfer_to_after_sales"]
    desc = tools[0]["function"]["description"]
    assert "售后维保" in desc


def test_run_agent_direct_reply_with_lent_tool():
    """inject 路径：A 借工具答完 → TurnResult(reply, None) + lent_by 记账。"""
    from src.chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "查下我的工单", stage="chat")
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [{"id": "c1", "function": {
            "name": "mock_lent_tool", "arguments": "{}"}}]},
        {"content": "您的工单已查到，预计明天完工。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.llm_config)
    assert result.reply == "您的工单已查到，预计明天完工。"
    assert result.dispatch_event is None
    assert s.cxt.metadata["served_by_projection"] == "after_sales"
    # tool 往返落 history
    tool_msgs = [m for m in s.cxt.history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].metadata.get("lent_by") == "after_sales"


def test_run_agent_transfer_returns_dispatch_event():
    """transfer 路径：A 调 transfer 工具 → 立即返回 dispatch_event，reply 不出口。"""
    from src.chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "我要投诉整个售后流程", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的这就为您处理", "tool_calls": [{
            "id": "c1", "function": {"name": "transfer_to_after_sales",
                                     "arguments": '{"reason": "售后投诉"}'}}]},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.llm_config)
    assert result.reply is None or result.reply == ""
    assert result.dispatch_event is not None
    assert result.dispatch_event.target_module_code == "after_sales"
    assert result.dispatch_event.reason == "售后投诉"
    # 状态已由 run_agent 内部 dispatch() 转移
    assert s.cxt.current_module_code == "after_sales"


def test_takeover_block_injected_for_target():
    """承接块：transfer 后目标模块首轮 prompt 含承接上下文。"""
    from src.chat.loop import run_agent
    s = _mk_session()
    s.cxt.current_module_code = "after_sales"
    s.cxt.metadata["handoff_context"] = {"from": "reception", "reason": "售后投诉"}
    s.cxt.add_message("user", "我要投诉整个售后流程", stage="chat")
    provider = ScriptedProvider([
        {"content": "看到您要投诉，我先记录一下。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["after_sales"], s.cxt.llm_config)
    assert "reception" in provider.seen[0]["messages"][0]["content"]
    assert "售后投诉" in provider.seen[0]["messages"][0]["content"]


def test_run_agent_transfer_rejected_backfills_error_and_continues():
    """transfer 被 dispatch 拒绝（非法目标）→ 错误回填 tool result，继续 loop 普通回复。"""
    from src.chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "我要办个神奇业务", stage="chat")
    provider = ScriptedProvider([
        {"content": "尝试移交", "tool_calls": [{
            "id": "c1", "function": {"name": "transfer_to_ghost",
                                     "arguments": '{"reason": "不存在"}'}}]},
        {"content": "好的，我直接为您处理。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.llm_config)
    assert result.reply == "好的，我直接为您处理。"
    assert result.dispatch_event is None
    # 状态未变
    assert s.cxt.current_module_code == "reception"
    # 错误回填 tool 消息落 history
    tool_msgs = [m for m in s.cxt.history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].metadata.get("tool_name") == "transfer_to_ghost"
    assert "转移被拒绝" in tool_msgs[0].content
    # LLM 第二轮收到了回填的 tool 结果
    second = provider.seen[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert "转移被拒绝" in second[-1]["content"]
    # 失败路径 content 不 suppress
    assistant_msgs = [m for m in s.cxt.history
                      if m.role == "assistant" and m.metadata.get("suppressed")]
    assert not assistant_msgs
