"""chat 层重入循环：same-turn transfer / ROUTE 静默分发 / 防环强制收尾。"""

from unittest.mock import patch

from src.chat.session import Session
from src.dialogue.module import AgentModule, ModuleLink, RouteModule
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern
from test_agent_inject_transfer import ScriptedProvider, _mk_session  # 复用


def _agent_pattern(**kw):
    """两 agent（reception → after_sales）+ 可选 max_hops。"""
    after_sales = AgentModule(
        module_code="after_sales", module_name="售后维保",
        module_description="售后", module_todo_description="售后流程",
        sub_modules=["reception"])
    reception = AgentModule(
        module_code="reception", module_name="前台", module_description="接待",
        sub_modules=[ModuleLink(target="after_sales")])
    return Pattern(code="p2", name="t", description="t",
                   entry_module_code="reception",
                   modules=[reception, after_sales], **kw)


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["dispatch_graph"] = pattern.dispatch_graph
    session.cxt.llm_config = {"code": "x", "model": "m"}
    sessions[sid] = session
    return session


def _chat(sessions, sid, query):
    from src.chat.chat import chat as chat_fn
    return chat_fn(query=query, session_id=sid, all_sessions=sessions)


def test_same_turn_transfer_b_replies():
    """A transfer → 同轮 B 接话，用户只听到 B。"""
    sessions = {}
    _launch(_agent_pattern(), sessions)
    provider = ScriptedProvider([
        # A：决定移交（content 被抑制）
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_after_sales",
            "arguments": '{"reason": "售后深入"}'}}]},
        # B：承接回复
        {"content": "看到您有售后需求，我先了解一下具体情况。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        reply = _chat(sessions, "s1", "帮我处理售后")
    assert reply == "看到您有售后需求，我先了解一下具体情况。"
    assert sessions["s1"].cxt.current_module_code == "after_sales"


def test_max_hops_exceeded_force_close():
    """连续 transfer 超过 max_hops=1：以当前模块强制收尾（prompt 注入勿再移交）。"""
    sessions = {}
    _launch(_agent_pattern(max_hops=1), sessions)
    provider = ScriptedProvider([
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_after_sales", "arguments": "{}"}}]},
        # 强制收尾轮：B 仍想转回，但已超限 → 应直接回复
        {"content": "好的，我来处理您的售后问题。", "tool_calls": []},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        reply = _chat(sessions, "s1", "帮我处理售后")
    assert reply == "好的，我来处理您的售后问题。"
