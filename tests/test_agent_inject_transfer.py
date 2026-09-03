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


def _acl_locked_tool_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "acl_locked_tool"},
                      ensure_ascii=False)


tool_registry.register(
    name="acl_locked_tool",
    toolset="test_lent",
    schema={
        "name": "acl_locked_tool",
        "description": "仅授权给其他 pattern 的工具（ACL 锁定）",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "查询内容"}},
        },
    },
    handler=_acl_locked_tool_handler,
    allowed_patterns={"other_pattern": ["after_sales"]},
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
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
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
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert result.reply == "您的工单已查到，预计明天完工。"
    assert result.dispatch_event is None
    assert s.cxt.metadata["served_by_projection"] == {
        "module": "reception", "source": "after_sales"}
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
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
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
        result = run_agent(s, s.cxt.module_map["after_sales"], s.cxt.metadata["llm_override"])
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
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
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


def test_lent_tools_respect_pattern_acl():
    """I-1：借出路径同样受 pattern 级工具 ACL 约束（deny-by-default 不被架空）。"""
    from src.chat.loop import _resolve_lent_tools
    s = _mk_session()
    reception = s.cxt.module_map["reception"]
    p = s.pattern
    schemas, lent_by = _resolve_lent_tools(reception, p)
    names = [t["function"]["name"] for t in schemas]
    # ACL 未授权 p/after_sales 的工具借不到
    assert "acl_locked_tool" not in names
    assert "acl_locked_tool" not in lent_by
    # ACL 已授权的仍可借
    assert "mock_lent_tool" in names
    assert lent_by["mock_lent_tool"] == "after_sales"


def test_handoff_context_cleared_next_turn():
    """I-2：承接块仅首轮注入，下一轮开头 chat() 清除 handoff_context。"""
    from src.chat.chat import chat as chat_fn
    from src.chat.session import Session
    from src.dialogue.module import AgentModule, ModuleLink
    from src.dialogue.pattern import Pattern

    after_sales = AgentModule(
        module_code="after_sales", module_name="售后维保",
        module_description="售后", module_todo_description="售后流程",
        sub_modules=["reception"])
    reception = AgentModule(
        module_code="reception", module_name="前台", module_description="接待",
        sub_modules=[ModuleLink(target="after_sales")])
    p = Pattern(code="p3", name="t", description="t",
                entry_module_code="reception", modules=[reception, after_sales])
    s = Session(session_id="s3", pattern_code="p3")
    s.pattern = p
    s.cxt.module_map = p.module_map
    s.cxt.node_map = p.node_map
    s.cxt.metadata["dispatch_graph"] = p.dispatch_graph
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions = {"s3": s}

    provider = ScriptedProvider([
        # A：transfer
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_after_sales",
            "arguments": '{"reason": "售后深入"}'}}]},
        # B 首轮：承接
        {"content": "看到您有售后需求。", "tool_calls": []},
        # B 第二轮：不应再有承接块
        {"content": "好的，具体说说。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        chat_fn(query="帮我处理售后", session_id="s3", all_sessions=sessions)
        assert "承接上下文" in provider.seen[1]["messages"][0]["content"]
        chat_fn(query="还要改期", session_id="s3", all_sessions=sessions)
    second_prompt = provider.seen[2]["messages"][0]["content"]
    assert "承接上下文" not in second_prompt
    assert "售后深入" not in second_prompt


def test_projection_recall_scoped_to_borrower():
    """I-3：回看块仅在借方自身轮次注入；dispatch 离开借方后键被清除。"""
    from src.chat.loop import run_agent
    s = _mk_session()
    s.cxt.metadata["served_by_projection"] = {
        "module": "reception", "source": "after_sales"}
    # 模拟借答后 dispatch 回 source 模块：reception → after_sales 合法
    from src.dialogue.dispatch import ModuleDispatch, dispatch
    assert dispatch(s.cxt, ModuleDispatch(target_module_code="after_sales",
                                          source="handoff_tool")) is True
    # 离开借方 → 键被清除
    assert "served_by_projection" not in s.cxt.metadata
    # 借方自身轮次仍注入回看块
    s2 = _mk_session()
    s2.cxt.metadata["served_by_projection"] = {
        "module": "reception", "source": "after_sales"}
    s2.cxt.add_message("user", "继续", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，继续为您处理。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        run_agent(s2, s2.cxt.module_map["reception"], s2.cxt.metadata["llm_override"])
    assert "上一轮提示" in provider.seen[0]["messages"][0]["content"]


def test_rejected_transfer_backfills_all_tool_calls():
    """M-5：同轮普通工具 + 非法 transfer，被拒时两者都回填（避免 API 400）。"""
    from src.chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "查工单顺便办个神奇业务", stage="chat")
    provider = ScriptedProvider([
        {"content": "查询并尝试移交", "tool_calls": [
            {"id": "c1", "function": {"name": "mock_lent_tool",
                                      "arguments": '{"query": "工单"}'}},
            {"id": "c2", "function": {"name": "transfer_to_ghost",
                                      "arguments": '{"reason": "不存在"}'}},
        ]},
        {"content": "好的，为您处理完毕。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert result.reply == "好的，为您处理完毕。"
    # 第二轮收到的 messages 尾部有两条 role=tool（全部 tool_call_id 有应答）
    second = provider.seen[1]["messages"]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}
    assert "转移被拒绝" in tool_msgs[1]["content"]
    # 两个 tool 结果都落 history
    hist_tools = [m for m in s.cxt.history if m.role == "tool"]
    assert len(hist_tools) == 2


def test_force_close_no_transfer_tools_and_prompt():
    """M-6(b)：force_close 时不注入 transfer 工具且 prompt 含"勿再移交"。"""
    from src.chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "帮我处理售后", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，我直接处理。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"],
                  force_close=True)
    first = provider.seen[0]
    assert first["tools"] is not None
    tool_names = [t["function"]["name"] for t in first["tools"]]
    assert not any(n.startswith("transfer_to_") for n in tool_names)
    assert "勿再移交" in first["messages"][0]["content"]
