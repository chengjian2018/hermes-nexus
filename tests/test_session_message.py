"""SessionMessage 数据类扩展 —— tool 轨迹字段 + summary role + sink 透传。"""

from src.dialogue.base import DialogueContext, SessionMessage


# ---------------------------------------------------------------------------
# 字段默认值与序列化
# ---------------------------------------------------------------------------

def test_new_fields_default_none():
    msg = SessionMessage(role="user", content="你好")
    assert msg.tool_call_id is None
    assert msg.tool_calls is None


def test_to_dict_roundtrip_with_tool_fields():
    tool_calls = [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "weather", "arguments": "{\"city\": \"北京\"}"},
    }]
    msg = SessionMessage(
        role="assistant", content="", stage="agent",
        tool_calls=tool_calls,
    )
    restored = SessionMessage.from_dict(msg.to_dict())
    assert restored.tool_calls == tool_calls
    assert restored.role == "assistant"
    assert restored.stage == "agent"


def test_to_dict_omits_none_tool_fields():
    d = SessionMessage(role="user", content="你好").to_dict()
    assert "tool_call_id" not in d
    assert "tool_calls" not in d


def test_from_dict_tolerates_missing_tool_fields():
    msg = SessionMessage.from_dict({"role": "tool", "content": "{}"})
    assert msg.tool_call_id is None
    assert msg.tool_calls is None


def test_from_dict_restores_tool_call_id():
    msg = SessionMessage.from_dict({
        "role": "tool", "content": "22 度", "tool_call_id": "call_9",
    })
    assert msg.tool_call_id == "call_9"


def test_summary_role_is_valid():
    msg = SessionMessage(role="summary", content="对话要点...", stage="compress")
    assert msg.to_dict()["role"] == "summary"


# ---------------------------------------------------------------------------
# add_message 透传
# ---------------------------------------------------------------------------

def test_add_message_passes_tool_fields():
    cxt = DialogueContext(session_id="s1", user_query="q")
    tool_calls = [{"id": "c1", "function": {"name": "t"}}]
    cxt.add_message("assistant", "", stage="agent", tool_calls=tool_calls)
    cxt.add_message("tool", "ok", stage="agent", tool_call_id="c1")
    assert cxt.history[0].tool_calls == tool_calls
    assert cxt.history[1].tool_call_id == "c1"


def test_message_sink_receives_appended_message():
    received = []
    cxt = DialogueContext(session_id="s1", user_query="q",
                          message_sink=received.append)
    cxt.add_message("user", "你好", stage="chat")
    assert len(received) == 1
    assert received[0] is cxt.history[0]


def test_message_sink_failure_does_not_break_dialogue():
    def bad_sink(msg):
        raise RuntimeError("db down")

    cxt = DialogueContext(session_id="s1", user_query="q", message_sink=bad_sink)
    cxt.add_message("user", "你好", stage="chat")  # 不应外抛
    assert len(cxt.history) == 1
    assert cxt.history[0].content == "你好"


# ---------------------------------------------------------------------------
# format_history 过滤（tool 轮 assistant 空行不进业务模板）
# ---------------------------------------------------------------------------

def test_format_history_skips_empty_content():
    cxt = DialogueContext(session_id="s1", user_query="q")
    cxt.add_message("user", "你好", stage="chat")
    cxt.add_message("assistant", "", stage="agent",
                    tool_calls=[{"id": "c1", "function": {"name": "t"}}])
    cxt.add_message("assistant", "在的～", stage="chat")
    formatted = cxt.format_history()
    assert "你好" in formatted
    assert "在的～" in formatted
    assert formatted.count("assistant:") == 1
