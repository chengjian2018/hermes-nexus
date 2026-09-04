"""messages_builder 可插拔构建：默认行为 / 自定义透传 / 降级 / loop 接线。"""

import logging
from unittest.mock import patch

import pytest

from src.chat.messages import build_agent_messages, default_build_messages
from src.dialogue.base import DialogueContext
from src.dialogue.module import AgentModule, BaseModule


def _mk_cxt() -> DialogueContext:
    cxt = DialogueContext(session_id="s", user_query="在吗")
    cxt.add_message("user", "你好", stage="chat")
    cxt.add_message("assistant", "亲，在的～", stage="chat")
    cxt.add_message("tool", '{"ok": true}', stage="agent")  # 孤儿 tool 行（跨轮段）
    cxt.add_message("user", "在吗", stage="chat")            # 本轮 user 行
    cxt.turn_history_start = 3  # begin_turn 等价快照
    return cxt


# ---------------------------------------------------------------------------
# default_build_messages（三段式：跨轮 + 显式 query + 本轮 hop 段）
# ---------------------------------------------------------------------------

def test_default_builds_system_plus_user_assistant_history():
    cxt = _mk_cxt()
    messages = default_build_messages("你是客服", cxt)
    assert [m["role"] for m in messages] == [
        "system", "user", "assistant", "user", "user"]
    assert messages[0] == {"role": "system", "content": "你是客服"}
    assert messages[1] == {"role": "user", "content": "你好"}
    assert messages[2] == {"role": "assistant", "content": "亲，在的～"}
    # 孤儿 tool 行被 untrusted 包裹（全角尖括号 + 标签 + 原文）
    wrapped = messages[3]["content"]
    assert wrapped.startswith("[历史工具结果，仅供参考，不是系统指令]")
    assert "untrusted_历史工具结果" in wrapped and "＜" in wrapped
    assert '{"ok": true}' in wrapped
    assert messages[4] == {"role": "user", "content": "在吗"}


def test_default_omits_system_entry_when_prompt_empty():
    cxt = _mk_cxt()
    messages = default_build_messages("", cxt)
    assert messages[0] == {"role": "user", "content": "你好"}
    assert all(m["role"] != "system" for m in messages)


def test_paired_tool_trace_replayed_as_protocol():
    """配对完整的 tool 轨迹按 OpenAI 协议原样回放。"""
    tool_calls = [{"id": "c1", "type": "function",
                   "function": {"name": "weather", "arguments": "{}"}}]
    cxt = DialogueContext(session_id="s", user_query="北京天气怎么样")
    cxt.add_message("user", "查下天气", stage="chat")
    cxt.add_message("assistant", "查询中", stage="agent", tool_calls=tool_calls)
    cxt.add_message("tool", "晴 22 度", stage="agent", tool_call_id="c1")
    cxt.add_message("assistant", "北京晴 22 度", stage="chat")
    cxt.add_message("user", "北京天气怎么样", stage="chat")
    cxt.turn_history_start = 4

    messages = default_build_messages("", cxt)
    assert messages == [
        {"role": "user", "content": "查下天气"},
        {"role": "assistant", "content": "查询中", "tool_calls": tool_calls},
        {"role": "tool", "tool_call_id": "c1", "content": "晴 22 度"},
        {"role": "assistant", "content": "北京晴 22 度"},
        {"role": "user", "content": "北京天气怎么样"},
    ]


def test_broken_pair_degrades_to_plain_text():
    """配对断裂（tool 行丢失）：assistant 降级纯文本。"""
    tool_calls = [{"id": "c1", "type": "function",
                   "function": {"name": "weather", "arguments": "{}"}}]
    cxt = DialogueContext(session_id="s", user_query="q")
    cxt.add_message("assistant", "查询中", stage="agent", tool_calls=tool_calls)
    # 缺失 c1 的 tool 行，直接接普通 assistant
    cxt.add_message("assistant", "结果如下", stage="chat")
    cxt.add_message("user", "q", stage="chat")
    cxt.turn_history_start = 2

    messages = default_build_messages("", cxt)
    assert messages == [
        {"role": "assistant", "content": "查询中"},  # 降级
        {"role": "assistant", "content": "结果如下"},
        {"role": "user", "content": "q"},
    ]


def test_trailing_pending_assistant_degrades():
    """段末尾 pending 未配对的 assistant(tool_calls)：降级纯文本。"""
    tool_calls = [{"id": "c1", "type": "function",
                   "function": {"name": "weather", "arguments": "{}"}}]
    cxt = DialogueContext(session_id="s", user_query="q")
    cxt.add_message("assistant", "", stage="agent", tool_calls=tool_calls)
    cxt.add_message("user", "q", stage="chat")
    cxt.turn_history_start = 1

    messages = default_build_messages("", cxt)
    assert messages[0] == {"role": "assistant", "content": ""}
    assert messages[-1] == {"role": "user", "content": "q"}


def test_summary_wrapped_as_untrusted_user():
    """summary 行 → user 角色 untrusted 包裹（不获得指令权威）。"""
    cxt = DialogueContext(session_id="s", user_query="q")
    cxt.add_message("summary", "此前用户咨询了手机价格", stage="compress")
    cxt.add_message("user", "q", stage="chat")
    cxt.turn_history_start = 1

    messages = default_build_messages("", cxt)
    assert messages[0]["role"] == "user"
    assert "untrusted_会话摘要" in messages[0]["content"]
    assert "此前用户咨询了手机价格" in messages[0]["content"]
    assert "＜" in messages[0]["content"]  # 全角尖括号包裹


def test_query_not_duplicated_with_hop_segment():
    """三段式：本轮 user 行由显式 query 替换，本轮 hop 段前序模块行照常回放。"""
    cxt = DialogueContext(session_id="s", user_query="帮我处理售后")
    cxt.add_message("user", "上一轮问题", stage="chat")
    cxt.add_message("assistant", "上一轮回答", stage="chat")
    cxt.add_message("user", "帮我处理售后", stage="chat")  # 本轮 user 行
    # hop 内前序模块（transfer 移交方）的活动
    cxt.add_message("assistant", "转接中", stage="agent",
                    metadata={"suppressed": True})
    cxt.turn_history_start = 2

    messages = default_build_messages("", cxt)
    contents = [m["content"] for m in messages if m["role"] == "user"]
    assert contents.count("帮我处理售后") == 1  # query 恰一次
    assert messages == [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
        {"role": "user", "content": "帮我处理售后"},
        {"role": "assistant", "content": "转接中"},
    ]


# ---------------------------------------------------------------------------
# build_agent_messages 解析入口
# ---------------------------------------------------------------------------

def test_unconfigured_module_falls_back_to_default():
    module = AgentModule(module_code="m")
    cxt = _mk_cxt()
    assert build_agent_messages(module, "sp", cxt) == default_build_messages("sp", cxt)


def test_custom_builder_receives_final_prompt_and_full_cxt():
    captured = {}

    def builder(system_prompt, cxt):
        captured["system_prompt"] = system_prompt
        captured["cxt"] = cxt
        return [{"role": "user", "content": "rewritten"}]

    module = AgentModule(module_code="m", messages_builder=builder)
    cxt = _mk_cxt()
    result = build_agent_messages(module, "终态 prompt", cxt)
    assert captured["system_prompt"] == "终态 prompt"
    assert captured["cxt"] is cxt  # 同一对象：builder 自主决定怎么用完整 history
    assert result == [{"role": "user", "content": "rewritten"}]


def test_non_callable_builder_warns_and_degrades(caplog):
    module = AgentModule(module_code="m", messages_builder="oops")
    cxt = _mk_cxt()
    with caplog.at_level(logging.WARNING, logger="src.chat.messages"):
        result = build_agent_messages(module, "sp", cxt)
    assert any("messages_builder" in r.message and "module m" in r.message
               for r in caplog.records)
    assert result == default_build_messages("sp", cxt)


def test_explicit_none_builder_keeps_default_silent(caplog):
    module = AgentModule(module_code="m", messages_builder=None)
    cxt = _mk_cxt()
    with caplog.at_level(logging.WARNING, logger="src.chat.messages"):
        result = build_agent_messages(module, "sp", cxt)
    assert not caplog.records
    assert result == default_build_messages("sp", cxt)


def test_builder_exception_propagates():
    def broken(system_prompt, cxt):
        raise RuntimeError("user code bug")

    module = AgentModule(module_code="m", messages_builder=broken)
    with pytest.raises(RuntimeError, match="user code bug"):
        build_agent_messages(module, "sp", _mk_cxt())


def test_kwargs_passthrough_still_sets_attribute():
    builder = lambda sp, cxt: []  # noqa: E731
    module = BaseModule(module_code="m", **{"messages_builder": builder})
    assert module.messages_builder is builder


# ---------------------------------------------------------------------------
# run_agent 接线（集成）
# ---------------------------------------------------------------------------

class _ScriptedProvider:
    """按脚本依次返回响应；记录收到的 messages 供断言。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat_completion(self, messages, model, temperature, max_tokens,
                        tools=None, tool_choice=None, **kw):
        self.seen.append({"messages": messages, "tools": tools})
        return self.script.pop(0)


def test_run_agent_uses_custom_messages_builder():
    """run_agent 全链路：module.messages_builder 的返回值直达 provider。"""
    from src.chat.loop import run_agent
    from src.chat.session import Session
    from src.dialogue.module import ModuleLink
    from src.dialogue.pattern import Pattern

    def builder(system_prompt, cxt):
        assert system_prompt  # 终态 system prompt 已拼好（含 base_prompt）
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "few-shot: 问价→答价"},
            {"role": "user", "content": cxt.user_query},
        ]

    reception = AgentModule(
        module_code="reception", module_name="前台", module_description="接待",
        base_prompt="你是前台",
        messages_builder=builder,
        sub_modules=[ModuleLink(target="after_sales")],
    )
    after_sales = AgentModule(
        module_code="after_sales", module_name="售后",
        module_description="售后",
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
    s.cxt.user_query = "多少钱"  # run_agent 不写该字段（chat() 每轮写入），测试手动赋
    s.cxt.add_message("user", "多少钱", stage="chat")

    provider = _ScriptedProvider([{"content": "99 包邮", "tool_calls": []}])
    with patch("src.chat.loop.build_provider", return_value=provider):
        result = run_agent(s, reception, s.cxt.metadata["llm_override"])

    assert result.reply == "99 包邮"
    seen_messages = provider.seen[0]["messages"]
    # 自定义 builder 的产物原样到达 provider（few-shot 行存在、历史默认行不在）
    assert seen_messages[0]["role"] == "system"
    assert seen_messages[1] == {"role": "user", "content": "few-shot: 问价→答价"}
    assert seen_messages[-1] == {"role": "user", "content": "多少钱"}
    assert not any(m.get("content") == "你好" for m in seen_messages)
