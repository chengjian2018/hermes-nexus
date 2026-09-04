"""run_agent：投影注入 / transfer 跳转事件 / tool 往返落盘测试。"""

import json
from unittest.mock import patch

from chat.session import Session
from dialogue.base import DialogueContext, ModuleJumpEvent, SessionMessage
from dialogue.module import AgentModule, ModuleLink
from dialogue.pattern import Pattern
from tools.register import registry as tool_registry


# ---------------------------------------------------------------------------
# 中性 mock 工具：模块级自注册，与内置工具互不干扰
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
    from chat.messages import build_projection_block
    s = _mk_session()
    block = build_projection_block(
        s.cxt.module_map["reception"], s.cxt.module_map)
    assert "售后维保" in block
    assert "保养预约" in block
    assert "mock_lent_tool" in block   # 借出工具列在投影块


def test_transfer_tools_generated_per_link():
    from chat.loop import build_transfer_tools
    s = _mk_session()
    tools = build_transfer_tools(
        s.cxt.module_map["reception"], s.cxt.module_map)
    names = [t["function"]["name"] for t in tools]
    assert names == ["transfer_to_after_sales"]
    desc = tools[0]["function"]["description"]
    assert "售后维保" in desc


def test_run_agent_direct_reply_with_lent_tool():
    """inject 路径：A 借工具答完 → TurnResult(reply) + lent_by 记账。"""
    from chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "查下我的工单", stage="chat")
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [{"id": "c1", "function": {
            "name": "mock_lent_tool", "arguments": "{}"}}]},
        {"content": "您的工单已查到，预计明天完工。", "tool_calls": []},
    ])
    with patch("chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert result.reply == "您的工单已查到，预计明天完工。"
    assert not [a for a in s.cxt.actions if isinstance(a, ModuleJumpEvent)]
    assert s.cxt.metadata["served_by_projection"] == {
        "module": "reception", "source": "after_sales"}
    # tool 往返落 history
    tool_msgs = [m for m in s.cxt.history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].metadata.get("lent_by") == "after_sales"


def test_run_agent_transfer_writes_jump_event():
    """transfer 路径：A 调 transfer 工具 → 写 ModuleJumpEvent 到 cxt.actions，
    reply 为空不出口；状态转移交由 chat 层 hop 循环消费（run_agent 不改状态）。"""
    from chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "我要投诉整个售后流程", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的这就为您处理", "tool_calls": [{
            "id": "c1", "function": {"name": "transfer_to_after_sales",
                                     "arguments": '{"reason": "售后投诉"}'}}]},
    ])
    with patch("chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert result.reply is None or result.reply == ""
    events = [a for a in s.cxt.actions if isinstance(a, ModuleJumpEvent)]
    assert len(events) == 1
    assert events[0].target_module_code == "after_sales"
    assert events[0].reason == "售后投诉"
    assert events[0].source == "handoff_tool"
    # 状态未由 run_agent 转移（chat 层消费事件时才转移）
    assert s.cxt.current_module_code == "reception"
    # A 的 content 不出口但保留进 history（suppressed）
    suppressed = [m for m in s.cxt.history
                  if m.role == "assistant" and m.metadata.get("suppressed")]
    assert len(suppressed) == 1


def test_run_agent_transfer_rejected_backfills_error_and_continues():
    """transfer 目标不存在于 module_map → 错误回填 tool result，继续 loop 普通回复。"""
    from chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "我要办个神奇业务", stage="chat")
    provider = ScriptedProvider([
        {"content": "尝试移交", "tool_calls": [{
            "id": "c1", "function": {"name": "transfer_to_ghost",
                                     "arguments": '{"reason": "不存在"}'}}]},
        {"content": "好的，我直接为您处理。", "tool_calls": []},
    ])
    with patch("chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert result.reply == "好的，我直接为您处理。"
    # 状态未变、无跳转事件
    assert s.cxt.current_module_code == "reception"
    assert not [a for a in s.cxt.actions if isinstance(a, ModuleJumpEvent)]
    # 错误回填 tool 消息落 history
    tool_msgs = [m for m in s.cxt.history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].metadata.get("tool_name") == "transfer_to_ghost"
    assert "转移目标不存在" in tool_msgs[0].content
    # LLM 第二轮收到了回填的 tool 结果
    second = provider.seen[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert "转移目标不存在" in second[-1]["content"]
    # 失败路径 content 不 suppress
    assistant_msgs = [m for m in s.cxt.history
                      if m.role == "assistant" and m.metadata.get("suppressed")]
    assert not assistant_msgs


def test_lent_tools_respect_pattern_acl():
    """I-1：借出路径同样受 pattern 级工具 ACL 约束（deny-by-default 不被架空）。"""
    from chat.loop import _resolve_lent_tools
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


def test_projection_recall_scoped_to_borrower():
    """I-3：回看块仅在借方自身轮次注入（served_by_projection 轮首重置）。"""
    from chat.loop import run_agent
    s = _mk_session()
    s.cxt.metadata["served_by_projection"] = {
        "module": "reception", "source": "after_sales"}
    s.cxt.add_message("user", "继续", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，继续为您处理。", "tool_calls": []},
    ])
    with patch("chat.loop.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert "上一轮提示" in provider.seen[0]["messages"][0]["content"]


def test_rejected_transfer_backfills_all_tool_calls():
    """M-5：同轮普通工具 + 非法 transfer，被拒时两者都回填（避免 API 400）。"""
    from chat.loop import run_agent
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
    with patch("chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])
    assert result.reply == "好的，为您处理完毕。"
    # 第二轮收到的 messages 尾部有两条 role=tool（全部 tool_call_id 有应答）
    second = provider.seen[1]["messages"]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}
    assert "转移目标不存在" in tool_msgs[1]["content"]
    # 两个 tool 结果都落 history
    hist_tools = [m for m in s.cxt.history if m.role == "tool"]
    assert len(hist_tools) == 2


def test_force_close_no_transfer_tools_and_prompt():
    """M-6(b)：force_close 时不注入 transfer 工具且 prompt 含"勿再移交"。"""
    from chat.loop import run_agent
    s = _mk_session()
    s.cxt.add_message("user", "帮我处理售后", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，我直接处理。", "tool_calls": []},
    ])
    with patch("chat.loop.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"],
                  force_close=True)
    first = provider.seen[0]
    assert first["tools"] is not None
    tool_names = [t["function"]["name"] for t in first["tools"]]
    assert not any(n.startswith("transfer_to_") for n in tool_names)
    assert "勿再移交" in first["messages"][0]["content"]


def test_chat_hop_consumes_transfer_event_same_turn():
    """transfer 事件经 chat 层 hop 循环消费：目标模块同轮续答。"""
    from chat.chat import chat as chat_fn

    s = _mk_session()
    sessions = {"s": s}
    provider = ScriptedProvider([
        # A（reception）：transfer
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_after_sales",
            "arguments": '{"reason": "售后深入"}'}}]},
        # B（after_sales）同轮续答
        {"content": "看到您有售后需求，已为您登记。", "tool_calls": []},
    ])
    with patch("chat.loop.build_provider", return_value=provider):
        text = chat_fn(query="帮我处理售后", session_id="s", all_sessions=sessions)
    assert text == "看到您有售后需求，已为您登记。"
    # 状态已转移到目标模块（hop 消费后）
    assert s.cxt.current_module_code == "after_sales"
    # 事件已被消费（actions 无 ModuleJumpEvent 残留）
    assert not [a for a in s.cxt.actions if isinstance(a, ModuleJumpEvent)]
    # B 的回复入口走 agent loop（两次 LLM 调用：A transfer + B 答复）
    assert len(provider.seen) == 2


# ---------------------------------------------------------------------------
# tool 轨迹完整记录（载荷形态：assistant 工具轮 content JSON + tool 行 metadata id）
# ---------------------------------------------------------------------------

def test_tool_round_ids_paired_in_history():
    """普通工具轮：assistant 载荷 tool_calls 与 tool 行 metadata id 一一配对。"""
    from chat.loop import run_agent
    from dialogue.base import decode_tool_call_content
    s = _mk_session()
    s.cxt.add_message("user", "查下我的工单", stage="chat")
    provider = ScriptedProvider([
        {"content": "查询中", "tool_calls": [{"id": "c1", "function": {
            "name": "mock_lent_tool", "arguments": '{}'}}]},
        {"content": "查到了。", "tool_calls": []},
    ])
    with patch("chat.loop.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])

    hist = [m for m in s.cxt.history if m.stage == "agent"]
    assistant_calls = [m for m in hist
                       if m.role == "assistant"
                       and decode_tool_call_content(m.content) is not None]
    assert len(assistant_calls) == 1
    text, calls = decode_tool_call_content(assistant_calls[0].content)
    assert text == "查询中"
    call_ids = {tc["id"] for tc in calls}
    tool_ids = {m.metadata.get("tool_call_id")
                for m in hist if m.role == "tool"}
    assert call_ids == tool_ids == {"c1"}


def test_transfer_turn_synthesizes_all_tool_results():
    """transfer 轮：同响应混合普通工具 + transfer，全部合成 tool 行且 id 全配对。"""
    from chat.loop import run_agent
    from dialogue.base import decode_tool_call_content
    s = _mk_session()
    s.cxt.add_message("user", "查完给我转售后", stage="chat")
    provider = ScriptedProvider([
        {"content": "好的，查完就转", "tool_calls": [
            {"id": "c1", "function": {"name": "mock_lent_tool",
                                      "arguments": '{}'}},
            {"id": "c2", "function": {"name": "transfer_to_after_sales",
                                      "arguments": '{"reason": "售后深入"}'}},
        ]},
    ])
    with patch("chat.loop.build_provider", return_value=provider):
        result = run_agent(s, s.cxt.module_map["reception"],
                           s.cxt.metadata["llm_override"])
    assert result.reply in (None, "")

    suppressed = [m for m in s.cxt.history
                  if m.role == "assistant" and m.metadata.get("suppressed")]
    assert len(suppressed) == 1
    text, calls = decode_tool_call_content(suppressed[0].content)
    assert text == "好的，查完就转"
    assert len(calls) == 2

    synthetic = [m for m in s.cxt.history if m.role == "tool"]
    assert len(synthetic) == 2  # 每个tool call 一条，全配对
    assert {m.metadata.get("tool_call_id") for m in synthetic} == {"c1", "c2"}
    assert all(m.metadata.get("synthetic") for m in synthetic)
    moved = [m for m in synthetic if m.content.startswith("[已移交至模块")]
    skipped = [m for m in synthetic if m.content.startswith("[未执行")]
    assert len(moved) == 1 and len(skipped) == 1


def test_rejected_transfer_records_tool_calls_on_assistant():
    """幻觉目标错误回填路径：assistant 载荷带 tool_calls、tool 行带 id。"""
    from chat.loop import run_agent
    from dialogue.base import decode_tool_call_content
    s = _mk_session()
    s.cxt.add_message("user", "我要办个神奇业务", stage="chat")
    provider = ScriptedProvider([
        {"content": "尝试移交", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_ghost", "arguments": '{}'}}]},
        {"content": "好的，我直接处理。", "tool_calls": []},
    ])
    with patch("chat.loop.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["reception"], s.cxt.metadata["llm_override"])

    agent_hist = [m for m in s.cxt.history if m.stage == "agent"]
    call_assistants = [m for m in agent_hist
                       if m.role == "assistant"
                       and decode_tool_call_content(m.content) is not None]
    assert len(call_assistants) == 1
    text, calls = decode_tool_call_content(call_assistants[0].content)
    assert text == "尝试移交"
    assert calls[0]["id"] == "c1"
    tools = [m for m in agent_hist if m.role == "tool"]
    assert tools[0].metadata["tool_call_id"] == "c1"
