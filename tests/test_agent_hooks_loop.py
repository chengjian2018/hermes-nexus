"""run_agent 挂载侧 hooks 集成测试：P1 注入 / P2/P3 观察 / P6 transfer / P7 出口 /
ROUTE 不触发。

习语沿 test_agent_inject_transfer.py：模块级注册中性 mock 工具 +
ScriptedProvider + patch("src.chat.loop.build_provider")。
"""

import json
from unittest.mock import patch

from src.chat.session import Session
from src.dialogue.base import PipelineStage
from src.dialogue.module import AgentModule, RouteModule
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern
from src.tools.register import registry as tool_registry


# ---------------------------------------------------------------------------
# 中性 mock 工具（独立 toolset / 工具名，与既有测试互不干扰）
# ---------------------------------------------------------------------------

def _echo_handler(args, **kwargs):
    return json.dumps({"ok": True, "tool": "hook_echo_tool", "args": args},
                      ensure_ascii=False)


tool_registry.register(
    name="hook_echo_tool",
    toolset="test_hooks",
    schema={
        "name": "hook_echo_tool",
        "description": "hooks 测试回声工具",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市"}},
        },
    },
    handler=_echo_handler,
    allowed_patterns={"ph": ["main"]},
)


def _mk_hooks_session(pattern_hooks=None, module_hooks=None,
                      with_transfer=False):
    main = AgentModule(
        module_code="main",
        module_name="主模块",
        module_description="主模块描述",
        use_tools=["hook_echo_tool"],
        agent_hooks=module_hooks,
        sub_modules=["peer"] if with_transfer else None,
    )
    peer = AgentModule(module_code="peer", module_name="同侪",
                       module_description="同侪模块", use_tools=["hook_echo_tool"])
    p = Pattern(code="ph", name="t", description="t",
                entry_module_code="main", modules=[main, peer],
                agent_hooks=pattern_hooks)
    s = Session(session_id="sh", pattern_code="ph")
    s.pattern = p
    s.cxt.module_map = p.module_map
    s.cxt.node_map = p.node_map
    s.cxt.current_module_code = "main"
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
        return self.script.pop(0)


def _run(s, provider):
    from src.chat.loop import run_agent
    with patch("src.chat.loop.build_provider", return_value=provider):
        return run_agent(s, s.cxt.module_map["main"],
                         s.cxt.metadata["llm_override"])


def _tool_call(cid="c1", name="hook_echo_tool", arguments="{}"):
    return {"id": cid, "function": {"name": name, "arguments": arguments}}


# ---------------------------------------------------------------------------
# P1：注入片段进 system prompt
# ---------------------------------------------------------------------------

def test_p1_fragments_injected_into_system_prompt():
    """多 hook 片段按声明序拼入 "## 扩展上下文" 块。"""
    s = _mk_hooks_session(pattern_hooks={
        "on_agent_start": [lambda e: "店铺在售：A、B",
                           lambda e: "当前时段：午间"],
    })
    provider = ScriptedProvider([{"content": "好的。", "tool_calls": []}])
    result = _run(s, provider)
    assert result.reply == "好的。"
    system = provider.seen[0]["messages"][0]["content"]
    assert system.strip().startswith("## 扩展上下文")
    assert "店铺在售：A、B" in system and "当前时段：午间" in system
    assert system.index("店铺在售") < system.index("当前时段")


def test_p1_hook_failure_degrades_silently():
    """P1 hook 抛异常：对话照常，片段缺失不报错。"""
    def boom(e):
        raise RuntimeError("取数失败")

    s = _mk_hooks_session(pattern_hooks={"on_agent_start": [boom]})
    provider = ScriptedProvider([{"content": "ok", "tool_calls": []}])
    result = _run(s, provider)
    assert result.reply == "ok"
    assert all("扩展上下文" not in (m.get("content") or "")
               for m in provider.seen[0]["messages"])


def test_p1_injection_precedes_force_close_suffix():
    """force_close：注入块在"勿再移交"后缀之前。"""
    from src.chat.loop import run_agent
    s = _mk_hooks_session(pattern_hooks={
        "on_agent_start": [lambda e: "店铺在售：A"],
    })
    provider = ScriptedProvider([{"content": "直接答", "tool_calls": []}])
    with patch("src.chat.loop.build_provider", return_value=provider):
        run_agent(s, s.cxt.module_map["main"],
                  s.cxt.metadata["llm_override"], force_close=True)
    system = provider.seen[0]["messages"][0]["content"]
    assert system.index("店铺在售") < system.index("勿再移交")


# ---------------------------------------------------------------------------
# P2 / P3：观察点
# ---------------------------------------------------------------------------

def test_p2_p3_observer_events():
    calls, responses = [], []
    s = _mk_hooks_session(pattern_hooks={
        "on_llm_call": [calls.append],
        "on_llm_response": [responses.append],
    })
    provider = ScriptedProvider([
        {"content": None, "tool_calls": [_tool_call()]},
        {"content": "done", "tool_calls": []},
    ])
    result = _run(s, provider)
    assert result.reply == "done"
    assert len(calls) == 2 and len(responses) == 2
    # P2：messages 为真实发送物（引用同一列表）、model、round_idx
    assert calls[0].messages is provider.seen[0]["messages"]
    assert calls[0].model == "m"
    assert [c.round_idx for c in calls] == [0, 1]
    # P3：content / tool_calls
    assert responses[0].tool_calls[0]["function"]["name"] == "hook_echo_tool"
    assert responses[1].content == "done"


# ---------------------------------------------------------------------------
# P6 / P7：transfer 与三个出口
# ---------------------------------------------------------------------------

def test_p6_p7_transfer_outcome():
    seen = []
    s = _mk_hooks_session(with_transfer=True, pattern_hooks={
        "on_transfer": [lambda e: seen.append(("transfer", e.target, e.reason))],
        "on_agent_end": [lambda e: seen.append(("end", e.outcome,
                                                e.transfer_target))],
    })
    provider = ScriptedProvider([
        {"content": "转接", "tool_calls": [_tool_call(
            name="transfer_to_peer", arguments='{"reason": "深入流程"}')]},
    ])
    result = _run(s, provider)
    assert result.reply in (None, "")
    assert ("transfer", "peer", "深入流程") in seen
    assert ("end", "transfer", "peer") in seen


def test_p7_reply_and_max_rounds_outcomes():
    ends = []
    # 直接答出口
    s = _mk_hooks_session(pattern_hooks={"on_agent_end": [ends.append]})
    provider = ScriptedProvider([{"content": "答案", "tool_calls": []}])
    result = _run(s, provider)
    assert result.reply == "答案"
    assert ends[-1].outcome == "reply" and ends[-1].reply == "答案"
    assert ends[-1].rounds == 1

    # 超轮次出口：连续工具调用 10 轮
    ends.clear()
    s2 = _mk_hooks_session(pattern_hooks={"on_agent_end": [ends.append]})
    script = [{"content": None, "tool_calls": [_tool_call(cid=f"c{i}")]}
              for i in range(10)]
    result2 = _run(s2, ScriptedProvider(script))
    assert result2.reply == "抱歉，处理超时，请稍后重试。"
    assert ends[-1].outcome == "max_rounds" and ends[-1].rounds == 10


def test_module_hooks_replace_pattern_hooks_in_loop():
    """module.agent_hooks 整体替换：pattern 级 hook 在该模块轮不触发。"""
    fired = []
    s = _mk_hooks_session(
        pattern_hooks={"on_agent_start": [lambda e: fired.append("pat")]},
        module_hooks={"on_agent_start": [lambda e: fired.append("mod")]},
    )
    provider = ScriptedProvider([{"content": "ok", "tool_calls": []}])
    _run(s, provider)
    assert fired == ["mod"]


# ---------------------------------------------------------------------------
# 范围守卫：非 AGENT 模块不触发 hooks
# ---------------------------------------------------------------------------

class _StaticNLG(PipelineStage):
    """自写 nlg_result 的静态 stage（绕开 LLM，验证 ROUTE 轮零触发）。"""

    stage_name = "static_nlg"

    def execute(self, ctx):
        ctx.nlg_result = {"content": "静态回复"}
        return ctx


def test_route_module_turn_does_not_fire_agent_hooks():
    from src.chat.chat import chat_turn
    fired = []
    route = RouteModule(
        module_code="root", module_name="路由", module_description="",
        module_nodes=[BaseNode(node_code="root", node_name="路由")],
    )
    p = Pattern(code="phr", name="t", description="t",
                entry_module_code="root", modules=[route],
                stages=[_StaticNLG()],
                agent_hooks={pt: [fired.append] for pt in
                             ("on_agent_start", "on_llm_call", "on_tool_call",
                              "on_tool_result", "on_transfer", "on_agent_end")})
    s = Session(session_id="shr", pattern_code="phr")
    s.pattern = p
    s.cxt.module_map = p.module_map
    s.cxt.node_map = p.node_map
    with patch("src.chat.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        result = chat_turn("你好", "shr", {"shr": s})
    assert result.text == "静态回复"
    assert fired == []
