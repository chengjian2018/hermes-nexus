"""TurnLifecycle / ChatResult 离线单测（不依赖 LLM）。"""

from src.chat.context_lifecycle import TurnLifecycle
from src.chat.response import ChatResult, build_chat_result
from src.dialogue.base import DialogueContext


def _make_cxt() -> DialogueContext:
    """构造一个"上一轮残留"状态的 cxt：各类字段均非空。"""
    cxt = DialogueContext(session_id="s1", user_query="旧问题")
    cxt.current_module_code = "m1"
    cxt.current_node_code = "n1"
    cxt.filled_slots = {"price": "100"}
    cxt.task_basic_info = {"city": "杭州"}
    cxt.nlu_result = {"intent": "old"}
    cxt.nlg_result = {"content": "旧回复"}
    cxt.agent_result = {"reply": "旧agent回复"}
    cxt.pre_recall_results = [{"doc": "旧"}]
    cxt.rewritten_queries = ["旧改写"]
    cxt.post_recall_results = [{"doc": "旧2"}]
    cxt.actions = [{"type": "old_action"}]
    cxt.metadata = {
        "dispatch_graph": {"m1": {"m2"}},
        "bargain_settings": {"max_rounds": 3},
        "task_info": {"order_id": "o1"},
        "llm_override": {"model": "x"},
        "pattern_code": "p1",
        "dispatch_log": [{"to": "m2"}],
        "handoff_context": {"from": "m1", "reason": "旧"},
        "unified": {"used": True},
        "clarify": {"triggered": True, "topic": "旧主题"},
        "served_by_projection": {"module": "m1", "source": "m0"},
    }
    cxt.add_message("user", "旧问题", stage="chat")
    return cxt


class TestBeginTurn:
    def test_user_query_overwritten(self):
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        assert cxt.user_query == "新问题"

    def test_per_turn_result_fields_reset(self):
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        assert cxt.nlu_result is None
        assert cxt.nlg_result is None
        assert cxt.agent_result is None

    def test_per_turn_list_fields_reset(self):
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        assert cxt.pre_recall_results == []
        assert cxt.rewritten_queries == []
        assert cxt.post_recall_results == []
        assert cxt.actions == []

    def test_per_turn_metadata_keys_popped(self):
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        for key in ("dispatch_log", "handoff_context", "unified"):
            assert key not in cxt.metadata

    def test_persistent_fields_survive(self):
        cxt = _make_cxt()
        history_len = len(cxt.history)
        TurnLifecycle().begin_turn(cxt, "新问题")
        assert cxt.current_module_code == "m1"
        assert cxt.current_node_code == "n1"
        assert cxt.filled_slots == {"price": "100"}
        assert cxt.task_basic_info == {"city": "杭州"}
        assert len(cxt.history) == history_len  # history 不清空
        assert cxt.node_map == {} and cxt.module_map == {}  # 保持原引用

    def test_persistent_metadata_keys_survive(self):
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        for key in ("dispatch_graph", "bargain_settings", "task_info",
                    "llm_override", "pattern_code"):
            assert key in cxt.metadata

    def test_stage_managed_metadata_untouched(self):
        """clarify / served_by_projection 由 stage/dispatch 自管理，轮首不清。"""
        cxt = _make_cxt()
        TurnLifecycle().begin_turn(cxt, "新问题")
        assert cxt.metadata["clarify"] == {"triggered": True, "topic": "旧主题"}
        assert cxt.metadata["served_by_projection"] == {
            "module": "m1", "source": "m0"}

    def test_idempotent_between_turns(self):
        """连续两次 begin_turn（模拟两轮）结果一致。"""
        cxt = _make_cxt()
        lc = TurnLifecycle()
        lc.begin_turn(cxt, "q1")
        lc.end_turn(cxt, "回复1")
        lc.begin_turn(cxt, "q2")
        assert cxt.user_query == "q2"
        assert cxt.nlu_result is None
        assert len(cxt.history) == 2  # 旧user + assistant回复1；q2 的 user 消息由 chat 层入


class TestBindPattern:
    def test_setdefault_injects_graph(self):
        cxt = _make_cxt()
        cxt.metadata.pop("dispatch_graph", None)
        pattern = type("P", (), {"dispatch_graph": {"a": {"b"}}})()
        TurnLifecycle().bind_pattern(cxt, pattern)
        assert cxt.metadata["dispatch_graph"] == {"a": {"b"}}

    def test_existing_graph_not_overwritten(self):
        cxt = _make_cxt()  # dispatch_graph 已存在
        pattern = type("P", (), {"dispatch_graph": {"x": {"y"}}})()
        TurnLifecycle().bind_pattern(cxt, pattern)
        assert cxt.metadata["dispatch_graph"] == {"m1": {"m2"}}

    def test_pattern_without_graph_attr(self):
        cxt = _make_cxt()
        cxt.metadata.pop("dispatch_graph", None)
        pattern = object()  # 无 dispatch_graph 属性
        TurnLifecycle().bind_pattern(cxt, pattern)
        assert cxt.metadata["dispatch_graph"] == {}


class TestEndTurn:
    def test_appends_assistant_message(self):
        cxt = _make_cxt()
        TurnLifecycle().end_turn(cxt, "最终回复")
        last = cxt.history[-1]
        assert last.role == "assistant"
        assert last.content == "最终回复"
        assert last.stage == "chat"


class TestMergeSlots:
    def test_merge_overwrites_same_key(self):
        cxt = _make_cxt()
        TurnLifecycle().merge_slots(cxt, {"price": "200", "color": "红"})
        assert cxt.filled_slots == {"price": "200", "color": "红"}

    def test_empty_slots_noop(self):
        cxt = _make_cxt()
        TurnLifecycle().merge_slots(cxt, {})
        assert cxt.filled_slots == {"price": "100"}


class TestChatResult:
    def test_build_snapshots_actions_and_dispatch_chain(self):
        cxt = _make_cxt()
        result = build_chat_result("回复文本", cxt)
        assert result.text == "回复文本"
        assert result.actions == [{"type": "old_action"}]
        assert result.dispatch_chain == [{"to": "m2"}]

    def test_build_with_empty_cxt(self):
        cxt = DialogueContext(session_id="s", user_query="q")
        result = build_chat_result("t", cxt)
        assert result == ChatResult(text="t", actions=[], dispatch_chain=[])

    def test_snapshot_is_copy_not_reference(self):
        """快照须为拷贝：轮首 cxt.actions 重置后不影响已构建的 ChatResult。"""
        cxt = _make_cxt()
        result = build_chat_result("t", cxt)
        cxt.actions.clear()
        assert result.actions == [{"type": "old_action"}]
