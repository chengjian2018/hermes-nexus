"""框架 wiring 测试 —— 管线插入、NLG 跳过、槽位不合并、节点不动。"""

import pytest

from src.chat.chat import _build_default_stages, _handle_node_transition
from src.dialogue.base import DialogueContext
from src.dialogue.module import FSMModule, RouteModule
from src.dialogue.nlg import FSMNLG


def make_fsm_module(enable_clarify):
    return FSMModule(
        module_code="m_fsm",
        module_nodes=[type("N", (), {"node_code": "n1", "nlu_stage": None,
                                      "nlg_stage": None})()],
        enable_clarify=enable_clarify,
    )


class TestBuildStages:

    def test_disabled_module_no_clarify_stage(self):
        stages = _build_default_stages(
            make_fsm_module(False).module_nodes[0], make_fsm_module(False))
        names = [type(s).__name__ for s in stages]
        assert "ClarifyStage" not in names

    def test_enabled_module_has_clarify_stage_between_nlu_nlg(self):
        module = make_fsm_module(True)
        stages = _build_default_stages(module.module_nodes[0], module)
        assert len(stages) == 3
        assert type(stages[0]).__name__ == "FSMNLU"
        assert type(stages[1]).__name__ == "ClarifyStage"
        assert type(stages[2]).__name__ == "FSMNLG"

    def test_route_module_never_has_clarify_stage(self):
        route = RouteModule(module_code="m_route")
        node = type("N", (), {"node_code": "n1", "nlu_stage": None,
                              "nlg_stage": None})()
        stages = _build_default_stages(node, route)
        assert all(type(s).__name__ != "ClarifyStage" for s in stages)


class TestNlgSkipGuard:

    def test_fsmnlg_skips_when_clarify_triggered(self):
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.metadata["clarify"] = {"triggered": True, "mode": "kb"}
        ctx.nlg_result = {"content": "[clarify:kb] 已生成"}
        calls = []
        FSMNLG._call_llm = lambda self, prompt, cfg=None: calls.append(prompt) or "x"
        try:
            FSMNLG().execute(ctx)
        finally:
            del FSMNLG._call_llm
        assert calls == []
        assert ctx.nlg_result == {"content": "[clarify:kb] 已生成"}


class TestTransitionGuard:

    def test_clarify_turn_skips_slot_merge_and_keeps_node(self):
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_node_code = "n1"
        ctx.nlu_result = {"next_node": "clarify",
                          "slots": {"topic": "费用", "keywords": ["收费"]}}
        ctx.metadata["clarify"] = {"triggered": True, "mode": "kb"}
        module = make_fsm_module(True)
        _handle_node_transition(ctx, module)
        assert ctx.filled_slots == {}            # topic/keywords 未污染业务槽位
        assert ctx.current_node_code == "n1"     # 节点不动
