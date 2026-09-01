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


def test_force_close_route_returns_nonempty_reply():
    """I-4：max_hops=1，agent transfer 进 ROUTE 后强制收尾——跳过 dispatch
    （含 jump_module 命中），消费 NLG 回复，不产生空回复。"""
    from src.dialogue.module import RouteModule
    from src.dialogue.node import BaseNode
    from src.dialogue.base import PipelineStage

    class _FakeRouteNLU(PipelineStage):
        stage_name = "fake_route_nlu"

        def execute(self, ctx):
            ctx.nlu_result = {"next_node": "menu_buy", "slots": {}}
            return ctx

    class _FakeRouteNLG(PipelineStage):
        stage_name = "fake_route_nlg"

        def execute(self, ctx):
            ctx.nlg_result = {"content": "购车咨询由我来介绍吧"}
            return ctx

    root = BaseNode(node_code="route_root", node_name="路由根",
                    sub_nodes=["menu_buy"])
    menu = BaseNode(node_code="menu_buy", node_name="购车咨询菜单",
                    jump_module="buy_agent")
    router = RouteModule(
        module_code="router", module_name="路由",
        module_nodes=[root, menu], sub_modules=["buy_agent"],
        nlu_stage=_FakeRouteNLU(), nlg_stage=_FakeRouteNLG())
    reception = AgentModule(
        module_code="reception", module_name="前台", module_description="接待",
        sub_modules=[ModuleLink(target="router")])
    buy_agent = AgentModule(
        module_code="buy_agent", module_name="购车专员", module_description="购车")
    pattern = Pattern(code="p_route", name="t", description="t",
                      entry_module_code="reception",
                      modules=[reception, router, buy_agent], max_hops=1)

    sessions = {}
    _launch(pattern, sessions, sid="sr")
    provider = ScriptedProvider([
        # hop 0：前台 transfer 进 ROUTE 路由模块（同轮重入）
        {"content": "转接中", "tool_calls": [{"id": "c1", "function": {
            "name": "transfer_to_router",
            "arguments": '{"reason": "购车"}'}}]},
    ])
    with patch("src.chat.loop.build_provider", return_value=provider):
        reply = _chat(sessions, "sr", "我想买车")
    # force_close 落在 ROUTE：不重跑 dispatch（否则 menu_buy 命中 buy_agent → 空回复）
    assert isinstance(reply, str) and reply, f"force_close 后回复不应为空: {reply!r}"
    assert reply == "购车咨询由我来介绍吧"
    assert sessions["sr"].cxt.current_module_code == "router"
