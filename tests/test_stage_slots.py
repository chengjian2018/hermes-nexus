"""管线槽位（pre_recall/query/post_recall/generate）与三层延迟解析测试。

核心契约（stage_slots.py 设计单一事实源的直接对应）：
- 三层优先级 node > module > pattern；generate 双形态（single / dict 恰含 nlu+nlg）
- 校验失败整层降级（全部槽位统一）；三层全空 → 召回/改写 no-op、generate builtin
- generate 展开为惰性子部件：nlu/nlg 在各自执行时刻独立三层解析
  （ROUTE 下 nlg 在菜单节点命中——时机修复的核心断言）
- 槽位直接 execute 必须 fail fast
"""

import pytest

from src.dialogue.base import DialogueContext
from src.dialogue.module import FSMModule, ModuleType, RouteModule
from src.dialogue.node import BaseNode
from src.dialogue.stage_slots import (
    GenerateSlot,
    PostRecallSlot,
    PreRecallSlot,
    QuerySlot,
    is_valid_stage,
    normalize_generate,
    resolve_stage,
)


class _Marker:
    """鸭子类型标记 stage：记录执行时的 (节点, 名字)。"""

    def __init__(self, name):
        self.stage_name = name

    def execute(self, ctx):
        ran.append((ctx.current_node_code, self.stage_name))
        return ctx


ran = []  # _Marker 全类共享执行记录（每个用例先清空）


@pytest.fixture(autouse=True)
def _clean_ran():
    ran.clear()
    yield
    ran.clear()


def _fsm_module(generate=None, query=None, pre_recall=None, post_recall=None,
                enable_clarify=False, clarify_stage=None):
    return FSMModule(
        module_code="m1", module_name="m1", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="n1", node_name="节点一")],
        generate=generate, query=query, pre_recall=pre_recall,
        post_recall=post_recall, enable_clarify=enable_clarify,
        clarify_stage=clarify_stage,
    )


def _route_module(**kw):
    return RouteModule(
        module_code="r1", module_name="r", module_description="d",
        module_todo_description="t", sub_modules=[], **kw
    )


def _ctx(node_code="n1", node=None, module_code="m1"):
    ctx = DialogueContext(session_id="t", user_query="q")
    ctx.current_module_code = module_code
    ctx.current_node_code = node_code
    ctx.node_map = {"n1": BaseNode(node_code="n1", node_name="节点一"),
                    "n2": node or BaseNode(node_code="n2", node_name="节点二"),
                    "root": BaseNode(node_code="root", node_name="根"),
                    "menu_a": BaseNode(node_code="menu_a", node_name="菜单A")}
    ctx.module_map = {module_code: _fsm_module()}
    return ctx


def _pattern(generate=None, query=None):
    from src.dialogue.pattern import Pattern
    return Pattern(code="p1", name="t", description="t",
                   entry_module_code="m1",
                   modules=[_fsm_module()],
                   generate=generate, query=query)


# ============================================================================
# is_valid_stage / normalize_generate
# ============================================================================

def test_is_valid_stage():
    assert is_valid_stage(_Marker("x")) is True
    assert is_valid_stage("not a stage") is False
    assert is_valid_stage(None) is False
    class _Broken:
        execute = "not callable"
    assert is_valid_stage(_Broken()) is False


def test_normalize_generate_forms():
    nlu, nlg, single = _Marker("nlu"), _Marker("nlg"), _Marker("single")
    assert normalize_generate({"nlu": nlu, "nlg": nlg}) == ("dict", nlu, nlg)
    assert normalize_generate(single) == ("single", single, None)
    # 非法：缺键 / 多键 / 值非法 / 类型错误
    assert normalize_generate({"nlu": nlu}) is None
    assert normalize_generate({"nlg": nlg}) is None
    assert normalize_generate({"nlu": nlu, "nlg": nlg, "extra": 1}) is None
    assert normalize_generate({"nlu": nlu, "nlg": "bad"}) is None
    assert normalize_generate("bad") is None
    assert normalize_generate(None) is None


# ============================================================================
# 召回/改写槽位：三层解析 + 降级 + no-op
# ============================================================================

def test_query_slot_three_layers_and_noop():
    ctx = _ctx()
    # 三层全空 → no-op
    assert resolve_stage(QuerySlot(), ctx, _fsm_module(), None) == []
    # module 层命中
    out = resolve_stage(QuerySlot(), ctx,
                        _fsm_module(query=_Marker("mod_query")), None)
    assert [s.stage_name for s in out] == ["mod_query"]


def test_query_slot_node_over_module_and_lazy_resolution():
    n2 = BaseNode(node_code="n2", node_name="节点二", query=_Marker("n2_query"))
    module = _fsm_module(query=_Marker("mod_query"))
    ctx = _ctx(node_code="n1", node=n2)

    out = resolve_stage(QuerySlot(), ctx, module, None)
    out[0].execute(ctx)  # n1 无配置 → module 层
    assert ran == [("n1", "mod_query")]

    ctx.current_node_code = "n2"  # 换节点后重新解析 → node 层命中
    out = resolve_stage(QuerySlot(), ctx, module, None)
    out[0].execute(ctx)
    assert ran[-1] == ("n2", "n2_query")


def test_query_slot_invalid_layers_degrade_to_noop():
    """node/module 层全非法 → 警告 + no-op（不抛异常）。"""
    ctx = _ctx()
    ctx.node_map["n1"] = BaseNode(node_code="n1", node_name="节点一",
                                  query="bad")
    module = _fsm_module(query=42)
    assert resolve_stage(QuerySlot(), ctx, module, None) == []


def test_recall_slots_share_same_semantics():
    ctx = _ctx()
    module = _fsm_module(pre_recall=_Marker("pre"), post_recall=None)
    out = resolve_stage(PreRecallSlot(), ctx, module, None)
    assert [s.stage_name for s in out] == ["pre"]
    assert resolve_stage(PostRecallSlot(), ctx, module, None) == []


# ============================================================================
# GenerateSlot：结构展开 + 子部件三层解析 + 降级 + builtin
# ============================================================================

def test_generate_expansion_shapes_by_stage_name():
    """展开结构：FSM 默认 / FSM+clarify / ROUTE。"""
    class _Clarify:
        stage_name = "my_clarify"
        def execute(self, ctx):
            return ctx

    fsm = _fsm_module()
    names = [s.stage_name for s in resolve_stage(GenerateSlot(), _ctx(), fsm, None)]
    assert names == ["generate_nlu_part", "generate_nlg_part"]

    cl = _fsm_module(enable_clarify=True, clarify_stage=_Clarify())
    names = [s.stage_name for s in resolve_stage(GenerateSlot(), _ctx(), cl, None)]
    assert names == ["generate_nlu_part", "my_clarify", "generate_nlg_part"]

    route = _route_module()
    ctx = _ctx(module_code="r1")
    names = [s.stage_name for s in resolve_stage(GenerateSlot(), ctx, route, None)]
    assert names == ["generate_nlu_part", "route_advance", "generate_nlg_part"]


def test_generate_parts_execute_dict_from_node_layer():
    """dict 形态：nlu/nlg 部件各自执行节点层配置。"""
    gen = {"nlu": _Marker("node_nlu"), "nlg": _Marker("node_nlg")}
    ctx = _ctx()
    ctx.node_map["n1"] = BaseNode(node_code="n1", node_name="节点一",
                                  generate=gen)
    module = _fsm_module(generate={"nlu": _Marker("m"), "nlg": _Marker("m")})

    for part in resolve_stage(GenerateSlot(), ctx, module, None):
        part.execute(ctx)

    assert ran == [("n1", "node_nlu"), ("n1", "node_nlg")]


def test_generate_parts_single_stage_runs_once():
    """single 形态：nlu 部件执行该 stage，nlg 部件 no-op。"""
    ctx = _ctx()
    module = _fsm_module(generate=_Marker("unified"))

    for part in resolve_stage(GenerateSlot(), ctx, module, None):
        part.execute(ctx)

    assert ran == [("n1", "unified")]


def test_generate_invalid_node_layer_degrades_to_module():
    """node 层 dict 缺 nlg（非法）→ 整层降级 module 层。"""
    ctx = _ctx()
    ctx.node_map["n1"] = BaseNode(node_code="n1", node_name="节点一",
                                  generate={"nlu": _Marker("broken")})
    module = _fsm_module(generate=_Marker("mod_unified"))

    for part in resolve_stage(GenerateSlot(), ctx, module, None):
        part.execute(ctx)

    assert ran == [("n1", "mod_unified")]


def test_generate_all_layers_empty_falls_to_builtin():
    from src.dialogue.nlu import FSMNLU
    from src.dialogue.nlg import FSMNLG
    # builtin 真实 stage 会走 LLM——这里只验证类装配，不打桩执行：
    names = [type(p).__name__ for p in
             resolve_stage(GenerateSlot(), _ctx(module_code="r1"),
                           _route_module(), None)]
    assert names == ["_GenerateNLUPart", "_RouteNodeAdvance", "_GenerateNLGPart"]
    # FSM builtin 冒烟：nlu part 解析出 FSMNLU（monkeypatch 其 execute 免 LLM）
    orig = FSMNLU.execute
    FSMNLU.execute = lambda self, ctx: ran.append(("builtin", "fsm_nlu")) or ctx
    orig_nlg = FSMNLG.execute
    FSMNLG.execute = lambda self, ctx: ran.append(("builtin", "fsm_nlg")) or ctx
    try:
        ctx = _ctx()
        for part in resolve_stage(GenerateSlot(), ctx, _fsm_module(), None):
            part.execute(ctx)
    finally:
        FSMNLU.execute = orig
        FSMNLG.execute = orig_nlg
    assert ran == [("builtin", "fsm_nlu"), ("builtin", "fsm_nlg")]


def test_generate_pattern_layer_used_when_node_module_unset():
    pattern = _pattern(generate=_Marker("pat_unified"))
    ctx = _ctx()
    for part in resolve_stage(GenerateSlot(), ctx, _fsm_module(), pattern):
        part.execute(ctx)
    assert ran == [("n1", "pat_unified")]


# ============================================================================
# ROUTE 时机修复（核心）：nlg 部件在节点切换后解析
# ============================================================================

def test_route_menu_node_nlg_resolves_after_advance():
    """ROUTE：root 执行 nlu，advance 切菜单后 nlg 部件按菜单节点层解析。"""
    ctx = _ctx(node_code="root", module_code="r1")
    ctx.node_map["root"] = BaseNode(
        node_code="root", node_name="根",
        generate={"nlu": _Marker("root_nlu"), "nlg": _Marker("root_nlg")})
    ctx.node_map["menu_a"] = BaseNode(
        node_code="menu_a", node_name="菜单A",
        generate={"nlu": _Marker("menu_nlu"), "nlg": _Marker("menu_nlg")})
    ctx.module_map = {"r1": _route_module()}
    # advance 需要 nlu_result 指向合法菜单节点
    ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
    ctx.module_map["r1"].module_nodes = [
        ctx.node_map["root"], ctx.node_map["menu_a"]]

    for stage in resolve_stage(GenerateSlot(), ctx, ctx.module_map["r1"], None):
        stage.execute(ctx)

    # nlu 来自 root 层；advance 已切节点；nlg 来自 menu_a 层（时机修复）
    assert ran == [("root", "root_nlu"), ("menu_a", "menu_nlg")]
    assert ctx.current_node_code == "menu_a"


# ============================================================================
# 非槽位原样放行 + 槽位 fail fast
# ============================================================================

def test_non_slot_stage_passthrough():
    concrete = _Marker("concrete")
    out = resolve_stage(concrete, _ctx(), _fsm_module(), None)
    assert out == [concrete]


def test_slot_direct_execute_raises():
    with pytest.raises(NotImplementedError):
        GenerateSlot().execute(_ctx())
