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
    cxt.add_message("tool", '{"ok": true}', stage="agent")  # 应被默认构建过滤
    cxt.add_message("user", "在吗", stage="chat")
    return cxt


# ---------------------------------------------------------------------------
# default_build_messages（loop 旧 _build_messages 平移行为锁定）
# ---------------------------------------------------------------------------

def test_default_builds_system_plus_user_assistant_history():
    cxt = _mk_cxt()
    messages = default_build_messages("你是客服", cxt)
    assert messages == [
        {"role": "system", "content": "你是客服"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "亲，在的～"},
        {"role": "user", "content": "在吗"},
    ]


def test_default_omits_system_entry_when_prompt_empty():
    cxt = _mk_cxt()
    messages = default_build_messages("", cxt)
    assert messages[0] == {"role": "user", "content": "你好"}
    assert all(m["role"] != "system" for m in messages)


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
