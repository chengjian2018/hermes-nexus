"""handlers / agents 离线单测（不依赖 LLM）。

钉住：AgentRunner 可注入性、默认实现委托、resolve_handler 映射、
FsmHandler 澄清守卫、RouteHandler 静默分发 / root 重置。
"""

from src.chat.agents import AgentRunner, LoopAgentRunner
from src.chat.handlers import (
    AgentHandler,
    FsmHandler,
    RouteHandler,
    default_skeleton,
    fsm_node_transition,
    resolve_handler,
)
from src.chat.loop import TurnResult
from src.dialogue.base import DialogueContext, PipelineStage
from src.dialogue.module import ModuleType
from src.chat.session import Session


def _noop_refresh(session, module_code="", node_code=""):
    session.cxt.llm_config = {"code": "t", "model": "m"}


class _RecordingRunner:
    """记录调用参数的 stub runner，证明可注入。"""

    def __init__(self, reply="stub回复"):
        self.reply = reply
        self.calls = []

    def run(self, session, module, llm_config, force_close=False):
        self.calls.append({
            "module": module,
            "llm_config": llm_config,
            "force_close": force_close,
        })
        return TurnResult(reply=self.reply)


class TestAgentRunnerProtocol:
    def test_recording_runner_satisfies_protocol(self):
        assert isinstance(_RecordingRunner(), AgentRunner)

    def test_loop_agent_runner_satisfies_protocol(self):
        assert isinstance(LoopAgentRunner(), AgentRunner)


class TestLoopAgentRunner:
    def test_delegates_to_run_agent(self, monkeypatch):
        """默认实现委托 loop.run_agent 且透传 force_close。"""
        recorded = {}

        def fake_run_agent(session, module, llm_config, force_close=False):
            recorded["args"] = (session, module, llm_config, force_close)
            return TurnResult(reply="来自loop")

        import src.chat.agents as agents_mod
        monkeypatch.setattr(agents_mod, "run_agent", fake_run_agent)

        session = object()
        module = object()
        result = LoopAgentRunner().run(session, module, {"model": "m"}, force_close=True)

        assert result.reply == "来自loop"
        assert recorded["args"] == (session, module, {"model": "m"}, True)


# ---------------------------------------------------------------------------
# resolve_handler 映射
# ---------------------------------------------------------------------------

class TestResolveHandler:
    def test_agent_type_maps_to_agent_handler(self):
        h = resolve_handler(ModuleType.AGENT, _noop_refresh)
        assert isinstance(h, AgentHandler)

    def test_fsm_type_maps_to_fsm_handler(self):
        assert isinstance(resolve_handler(ModuleType.FSM, _noop_refresh), FsmHandler)

    def test_route_type_maps_to_route_handler(self):
        assert isinstance(resolve_handler(ModuleType.ROUTE, _noop_refresh), RouteHandler)

    def test_unknown_type_raises(self):
        import pytest
        with pytest.raises(ValueError):
            resolve_handler("bogus", _noop_refresh)

    def test_agent_runner_injected(self):
        runner = _RecordingRunner()
        h = resolve_handler(ModuleType.AGENT, _noop_refresh, agent_runner=runner)
        assert h.agent_runner is runner


# ---------------------------------------------------------------------------
# FsmHandler
# ---------------------------------------------------------------------------

class _FakeNLU(PipelineStage):
    stage_name = "fake_nlu"

    def __init__(self, next_node="", slots=None):
        self.next_node = next_node
        self.slots = slots or {}

    def execute(self, ctx):
        ctx.nlu_result = {"next_node": self.next_node, "slots": self.slots}
        return ctx


class _FakeNLG(PipelineStage):
    stage_name = "fake_nlg"

    def execute(self, ctx):
        ctx.nlg_result = {"content": "FSM回复"}
        return ctx


class TestFsmHandler:
    def _make_module(self):
        from src.dialogue.module import FSMModule

        n1 = type("N", (), {"node_code": "n1", "nlu_stage": None,
                            "nlg_stage": None})()
        n2 = type("N", (), {"node_code": "n2", "nlu_stage": None,
                            "nlg_stage": None})()
        module = FSMModule(module_code="m_fsm", module_nodes=[n1, n2])
        module.generate = {"nlu": _FakeNLU(next_node="n2",
                                          slots={"price": "10万"}),
                           "nlg": _FakeNLG()}
        return module

    def _make_session(self, module):
        from src.dialogue.pattern import Pattern

        pattern = Pattern(code="pf", name="t", description="t",
                          entry_module_code="m_fsm", modules=[module])
        session = Session(session_id="s", pattern_code="pf")
        session.pattern = pattern
        session.cxt.module_map = pattern.module_map
        session.cxt.node_map = pattern.node_map
        return session

    def test_handle_runs_pipeline_and_jumps_node(self):
        module = self._make_module()
        session = self._make_session(module)
        result = FsmHandler(_noop_refresh).handle(session, module)
        assert result.reply == "FSM回复"
        assert result.dispatch_event is None
        assert session.cxt.current_node_code == "n2"   # next_node 跳转
        assert session.cxt.filled_slots == {"price": "10万"}  # 槽位增量合并

    def test_clarify_turn_keeps_node_and_slots(self):
        """澄清轮：跳过槽位合并与节点跳转（与旧 _handle_node_transition 一致）。"""
        module = self._make_module()
        module.generate["nlu"] = _FakeNLU(next_node="clarify",
                                          slots={"topic": "费用"})
        session = self._make_session(module)
        session.cxt.current_node_code = "n1"
        session.cxt.metadata["clarify"] = {"triggered": True, "mode": "kb"}
        FsmHandler(_noop_refresh).handle(session, module)
        assert session.cxt.current_node_code == "n1"
        assert session.cxt.filled_slots == {}

    def test_first_entry_uses_first_node(self):
        module = self._make_module()
        session = self._make_session(module)
        assert session.cxt.current_node_code is None
        FsmHandler(_noop_refresh).handle(session, module)
        assert session.cxt.current_node_code == "n2"  # n1 起步 → 跳到 n2


# ---------------------------------------------------------------------------
# RouteHandler
# ---------------------------------------------------------------------------

class TestRouteHandler:
    def _make_pattern(self, jump_target="buy_agent"):
        from src.dialogue.module import AgentModule, ModuleLink, RouteModule
        from src.dialogue.node import BaseNode
        from src.dialogue.pattern import Pattern

        class _RouteNLU(PipelineStage):
            stage_name = "fake_route_nlu"

            def execute(self, ctx):
                ctx.nlu_result = {"next_node": "menu_buy", "slots": {"car": "suv"}}
                return ctx

        class _RouteNLG(PipelineStage):
            stage_name = "fake_route_nlg"

            def execute(self, ctx):
                ctx.nlg_result = {"content": "路由回复"}
                return ctx

        root = BaseNode(node_code="route_root", node_name="根",
                        sub_nodes=["menu_buy"])
        menu = BaseNode(node_code="menu_buy", node_name="菜单",
                        jump_module=jump_target)
        router = RouteModule(
            module_code="router", module_name="路由",
            module_nodes=[root, menu], sub_modules=["buy_agent"],
            generate={"nlu": _RouteNLU(), "nlg": _RouteNLG()})
        buy_agent = AgentModule(
            module_code="buy_agent", module_name="购车", module_description="购车")
        return Pattern(code="pr", name="t", description="t",
                       entry_module_code="router",
                       modules=[router, buy_agent])

    def _make_session(self, pattern):
        session = Session(session_id="s", pattern_code="pr")
        session.pattern = pattern
        session.cxt.module_map = pattern.module_map
        session.cxt.node_map = pattern.node_map
        # 真实流程 chat() 在调 handler 前写入（_RouteNodeAdvance 依赖它路由）
        session.cxt.current_module_code = "router"
        session.cxt.metadata["dispatch_graph"] = pattern.dispatch_graph
        return session

    def test_silent_dispatch_returns_event(self):
        """菜单节点命中 jump_module：静默分发，返回 dispatch_event 不出文本。"""
        session = self._make_session(self._make_pattern())
        router = session.pattern.module_map["router"]
        result = RouteHandler(_noop_refresh).handle(session, router)
        assert result.reply is None
        assert result.dispatch_event is not None
        assert result.dispatch_event.target_module_code == "buy_agent"
        assert session.cxt.current_module_code == "buy_agent"  # dispatch 已转移
        assert session.cxt.filled_slots == {"car": "suv"}       # 槽位仍合并

    def test_no_jump_resets_to_root(self):
        """菜单节点无合法 jump_module：不出 dispatch，轮末重置回 root。"""
        session = self._make_session(self._make_pattern(jump_target=None))
        router = session.pattern.module_map["router"]
        result = RouteHandler(_noop_refresh).handle(session, router)
        assert result.reply == "路由回复"
        assert result.dispatch_event is None
        assert session.cxt.current_node_code == "route_root"

    def test_force_close_skips_dispatch_keeps_reply(self):
        """force_close：跳过 dispatch（含 jump_module 命中）但消费 NLG 回复 + 回 root。"""
        session = self._make_session(self._make_pattern())
        router = session.pattern.module_map["router"]
        result = RouteHandler(_noop_refresh).handle(session, router, force_close=True)
        assert result.reply == "路由回复"
        assert result.dispatch_event is None
        assert session.cxt.current_module_code == "router"   # 未 dispatch
        assert session.cxt.current_node_code == "route_root"

    def test_clarify_turn_keeps_menu_node(self):
        """澄清轮：不走静默分发也不重置 root，节点保持（菜单节点续轮）。"""
        session = self._make_session(self._make_pattern())
        session.cxt.metadata["clarify"] = {"triggered": True, "mode": "kb"}
        router = session.pattern.module_map["router"]
        result = RouteHandler(_noop_refresh).handle(session, router)
        assert result.reply == "路由回复"
        # _RouteNodeAdvance 已把节点切到 menu_buy；澄清守卫保持它
        assert session.cxt.current_node_code == "menu_buy"


# ---------------------------------------------------------------------------
# default_skeleton（从 chat 迁移后的兼容面）
# ---------------------------------------------------------------------------

class TestDefaultSkeleton:
    def test_four_slots_in_order(self):
        names = [type(s).__name__ for s in default_skeleton(object())]
        assert names == ["PreRecallSlot", "QuerySlot", "PostRecallSlot",
                         "GenerateSlot"]


# ---------------------------------------------------------------------------
# fsm_node_transition（澄清守卫，等价旧 _handle_node_transition）
# ---------------------------------------------------------------------------

class TestFsmNodeTransition:
    def test_guard_skips_on_clarify(self):
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_node_code = "n1"
        ctx.nlu_result = {"next_node": "n2", "slots": {"a": 1}}
        ctx.metadata["clarify"] = {"triggered": True}
        fsm_node_transition(ctx, object())
        assert ctx.current_node_code == "n1"
        assert ctx.filled_slots == {}

    def test_jumps_to_valid_next_node(self):
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_node_code = "n1"
        ctx.nlu_result = {"next_node": "n2", "slots": {}}
        ctx.node_map = {"n2": object()}
        fsm_node_transition(ctx, object())
        assert ctx.current_node_code == "n2"

    def test_invalid_next_node_keeps_current(self):
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_node_code = "n1"
        ctx.nlu_result = {"next_node": "ghost", "slots": {}}
        ctx.node_map = {}
        fsm_node_transition(ctx, object())
        assert ctx.current_node_code == "n1"
