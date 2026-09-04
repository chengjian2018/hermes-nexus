"""ModuleJumpEvent 机制：stage 循环内检测 + hop 消费重路由。

覆盖 chat._detect_jump_after_stage 的三类来源与容错：
- NLU 直接输出 jump_module 字段（nlu_jump）
- next_node 命中节点的 jump_module 配置（route_menu，先推进菜单 + R4）
- 目标不存在 / 自环 → 忽略继续跑剩余 stages（LLM 幻觉容错）
"""

from unittest.mock import patch

from src.chat.chat import _detect_jump_after_stage
from src.chat.session import Session
from src.dialogue.base import DialogueContext, ModuleJumpEvent, PipelineStage
from src.dialogue.module import AgentModule, FSMModule, ModuleLink, RouteModule
from src.dialogue.node import BaseNode
from src.dialogue.pattern import Pattern


# ============================================================================
# 脚手架
# ============================================================================

class _JumpNLU(PipelineStage):
    """写 nlu_result 的桩 NLU：next_node / jump_module 由用例注入。"""

    stage_name = "jump_nlu"

    def __init__(self, nlu_result):
        self.nlu_result = nlu_result

    def execute(self, ctx):
        ctx.nlu_result = dict(self.nlu_result)
        return ctx


class _MarkerNLG(PipelineStage):
    stage_name = "marker_nlg"

    def execute(self, ctx):
        ctx.nlg_result = {"content": "nlg_ran"}
        return ctx


def _route_with_menu(menu_jump=None):
    menu = BaseNode(node_code="menu_a", node_name="菜单A",
                    jump_module=menu_jump)
    root = BaseNode(node_code="root", node_name="根", sub_nodes=["menu_a"])
    route = RouteModule(module_code="r1", module_name="r",
                        module_description="d", module_todo_description="t",
                        module_nodes=[root, menu])
    target = FSMModule(
        module_code="m1", module_name="m", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="f1", node_name="F1")])
    pattern = Pattern(code="pj", name="t", description="t",
                      entry_module_code="r1",
                      modules=[route] if menu_jump is None
                      else [route, target],
                      sub_modules=None)
    return pattern, route, target


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    session.cxt.current_module_code = "r1"
    sessions[sid] = session
    return session


def _chat(sessions, sid, query):
    from src.chat.chat import chat as chat_fn
    return chat_fn(query=query, session_id=sid, all_sessions=sessions)


# ============================================================================
# _detect_jump_after_stage 单测（离线）
# ============================================================================

class TestDetectJump:
    def _ctx(self):
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_module_code = "r1"
        ctx.current_node_code = "root"
        return ctx

    def test_nlu_jump_field_wins(self):
        """nlu_result.jump_module 直接命中 → nlu_jump 事件。"""
        ctx = self._ctx()
        ctx.module_map = {"r1": object(), "m1": object()}
        ctx.nlu_result = {"next_node": "", "jump_module": "m1",
                          "reason": "售后", "slots": {}}
        event = _detect_jump_after_stage(ctx, _route_mod(), before_nlu=None)
        assert event is not None
        assert event.target_module_code == "m1"
        assert event.source == "nlu_jump"
        assert event.reason == "售后"

    def test_node_jump_module_after_advance(self):
        """next_node 命中带 jump_module 的节点 → 先切节点再 route_menu 事件。"""
        ctx = self._ctx()
        menu = BaseNode(node_code="menu_a", node_name="A", jump_module="m1")
        root = BaseNode(node_code="root", node_name="R", sub_nodes=["menu_a"])
        ctx.node_map = {"root": root, "menu_a": menu}
        ctx.module_map = {"r1": object(), "m1": object()}
        ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
        route = RouteModule(module_code="r1", module_name="r",
                            module_description="d", module_todo_description="t",
                            module_nodes=[root, menu])
        event = _detect_jump_after_stage(ctx, route, before_nlu=None)
        assert event is not None
        assert event.target_module_code == "m1"
        assert event.source == "route_menu"
        assert ctx.current_node_code == "menu_a"  # 节点已推进

    def test_self_jump_ignored(self):
        """跳转目标为当前模块（自环）→ 忽略。"""
        ctx = self._ctx()
        ctx.module_map = {"r1": object(), "m1": object()}
        ctx.nlu_result = {"next_node": "", "jump_module": "r1", "slots": {}}
        assert _detect_jump_after_stage(ctx, _route_mod(), None) is None

    def test_unknown_target_ignored(self):
        """目标不在 module_map（LLM 幻觉）→ 忽略。"""
        ctx = self._ctx()
        ctx.module_map = {"r1": object()}
        ctx.nlu_result = {"next_node": "", "jump_module": "ghost", "slots": {}}
        assert _detect_jump_after_stage(ctx, _route_mod(), None) is None

    def test_unchanged_nlu_result_skipped(self):
        """nlu_result 未被本 stage 更新（同对象）→ 不检测（hop 续答防误检）。"""
        ctx = self._ctx()
        ctx.module_map = {"r1": object(), "m1": object()}
        ctx.nlu_result = {"jump_module": "m1", "slots": {}}
        assert _detect_jump_after_stage(ctx, _route_mod(), ctx.nlu_result) is None


def _route_mod():
    return RouteModule(module_code="r1", module_name="r",
                       module_description="d", module_todo_description="t",
                       module_nodes=[BaseNode(node_code="root",
                                              node_name="R")])


# ============================================================================
# e2e：stage 循环中断 + hop 消费
# ============================================================================

def test_nlu_jump_breaks_stages_and_reroutes_same_turn():
    """NLU 输出 jump_module → 剩余 stages 中断（NLG 不跑）、源模块静默，
    chat 层 hop 消费重路由到目标模块同轮续答。"""
    menu = BaseNode(node_code="menu_a", node_name="菜单A")
    root = BaseNode(node_code="root", node_name="根", sub_nodes=["menu_a"],
                    generate={"nlu": _JumpNLU({"next_node": "",
                                               "jump_module": "m1",
                                               "reason": "选车",
                                               "slots": {"brand": "A"}}),
                              "nlg": _MarkerNLG()})
    route = RouteModule(module_code="r1", module_name="r",
                        module_description="d", module_todo_description="t",
                        module_nodes=[root, menu])

    ran = []

    class _TargetNLG(PipelineStage):
        stage_name = "target_nlg"

        def execute(self, ctx):
            ran.append("target_nlg")
            ctx.nlg_result = {"content": "已为您切换到目标模块"}
            return ctx

    target = FSMModule(
        module_code="m1", module_name="m", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="f1", node_name="F1",
                               generate={"nlg": _TargetNLG()})])

    class _FSMNLUStub(PipelineStage):
        stage_name = "fsm_nlu"

        def execute(self, ctx):
            ran.append("fsm_nlu")
            ctx.nlu_result = {"next_node": "", "slots": {}}
            return ctx

    target.generate = {"nlu": _FSMNLUStub(), "nlg": _TargetNLG()}

    pattern = Pattern(code="pj1", name="t", description="t",
                      entry_module_code="r1", modules=[route, target])
    sessions = {}
    _launch(pattern, sessions)
    with patch("src.chat.loop.build_provider"):
        reply = _chat(sessions, "s1", "我要买车")

    assert reply == "已为您切换到目标模块"
    assert "marker_nlg" not in ran          # 源模块 NLG 未执行（stages 中断）
    assert ran == ["fsm_nlu", "target_nlg"]  # 目标模块同轮续答
    assert sessions["s1"].cxt.current_module_code == "m1"
    # 槽位随跳转合并（目标模块承接上下文）
    assert sessions["s1"].cxt.filled_slots.get("brand") == "A"


def test_jump_event_via_actions_snapshot_when_hops_exhausted():
    """超跳数：第二轮 hop 的跳转事件被消费落到最后目标后 force_close 收尾。

    FSM 不产生事件（检测仅 ROUTE），环路由 ROUTE 反复输出 jump_module
    制造：r1 →(route_menu) m1（hop1 消费）→ 下一轮重新进 r1 →(nlu_jump)
    m1（hop2 消费）→ 超限 force_close。
    """
    from src.chat.chat import chat_turn

    # root NLU 第一次跳 m1，再次进入 r1 时直接 jump_module 跳走（制造环）
    class _LoopRouteNLU(PipelineStage):
        stage_name = "loop_route_nlu"

        def __init__(self):
            self.calls = 0

        def execute(self, ctx):
            self.calls += 1
            # 首次经菜单节点 route_menu，后续直接 nlu_jump（两种来源都覆盖）
            if self.calls == 1:
                ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
            else:
                ctx.nlu_result = {"next_node": "", "jump_module": "m1",
                                  "slots": {}}
            return ctx

    menu = BaseNode(node_code="menu_a", node_name="菜单A",
                    jump_module="m1")
    root = BaseNode(node_code="root", node_name="根", sub_nodes=["menu_a"],
                    generate={"nlu": _LoopRouteNLU(), "nlg": _MarkerNLG()})
    route = RouteModule(module_code="r1", module_name="r",
                        module_description="d", module_todo_description="t",
                        module_nodes=[root, menu])

    ran = []

    class _TargetNLG(PipelineStage):
        stage_name = "target_nlg"

        def execute(self, ctx):
            ran.append("target_nlg")
            ctx.nlg_result = {"content": "target 回复"}
            return ctx

    class _TargetNLU(PipelineStage):
        stage_name = "target_nlu"

        def execute(self, ctx):
            ctx.nlu_result = {"next_node": "", "slots": {}}
            return ctx

    target = FSMModule(
        module_code="m1", module_name="m", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="f1", node_name="F1")],
        generate={"nlu": _TargetNLU(), "nlg": _TargetNLG()})

    pattern = Pattern(code="pj2", name="t", description="t",
                      entry_module_code="r1", modules=[route, target],
                      max_hops=2)
    sessions = {}
    _launch(pattern, sessions)
    with patch("src.chat.loop.build_provider"):
        result = chat_turn("选A", "s1", sessions)

    # force_close 落在最后目标 m1（FSM 轮不检测跳转，stages 跑完出回复）
    assert result.text == "target 回复"
    assert sessions["s1"].cxt.current_module_code == "m1"
    # 事件通道已清空（超跳 pending 消费后 force_close 不再产生事件）
    assert not [a for a in result.actions if "module_jump" in a]
