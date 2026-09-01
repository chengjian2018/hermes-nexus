"""dispatch() 转移原语测试：校验、转移、记账、回弹拒绝。"""

from src.dialogue.base import DialogueContext
from src.dialogue.dispatch import ModuleDispatch, dispatch
from src.dialogue.module import AgentModule
from src.dialogue.pattern import Pattern


def _mk_ctx():
    a = AgentModule(module_code="a", sub_modules=["b"])
    b = AgentModule(module_code="b", sub_modules=["a"])
    p = Pattern(code="p", name="t", description="t",
                entry_module_code="a", modules=[a, b])
    ctx = DialogueContext(session_id="s", user_query="q")
    ctx.module_map = p.module_map
    ctx.node_map = p.node_map
    ctx.current_module_code = "a"
    ctx.metadata["dispatch_graph"] = p.dispatch_graph
    return ctx


def test_dispatch_moves_state_and_logs():
    ctx = _mk_ctx()
    ok = dispatch(ctx, ModuleDispatch(target_module_code="b", reason="售后深入", source="handoff_tool"))
    assert ok is True
    assert ctx.current_module_code == "b"
    assert ctx.current_node_code is None
    log = ctx.metadata["dispatch_log"]
    assert log == [{"from": "a", "to": "b", "source": "handoff_tool", "reason": "售后深入"}]
    assert ctx.metadata["handoff_context"] == {
        "from": "a", "reason": "售后深入",
    }


def test_dispatch_rejects_non_adjacent_target():
    ctx = _mk_ctx()
    ok = dispatch(ctx, ModuleDispatch(target_module_code="ghost"))
    assert ok is False
    assert ctx.current_module_code == "a"
    assert ctx.metadata.get("dispatch_log") is None or ctx.metadata["dispatch_log"] == []


def test_dispatch_rejects_bounce_back_same_turn():
    """A→B 后同轮 B 立即转回 A：拒绝（防移交环 spec §4.2）。"""
    ctx = _mk_ctx()
    dispatch(ctx, ModuleDispatch(target_module_code="b"))
    ok = dispatch(ctx, ModuleDispatch(target_module_code="a"))
    assert ok is False
    assert ctx.current_module_code == "b"


def test_dispatch_no_graph_denies():
    ctx = _mk_ctx()
    ctx.metadata.pop("dispatch_graph")
    assert dispatch(ctx, ModuleDispatch(target_module_code="b")) is False
