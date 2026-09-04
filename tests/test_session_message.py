"""SessionMessage —— tool 轨迹 JSON 载荷 + summary role + sink 透传。"""

from src.dialogue.base import (
    DialogueContext,
    SessionMessage,
    decode_tool_call_content,
    encode_tool_call_content,
)


# ---------------------------------------------------------------------------
# 载荷编码/解码
# ---------------------------------------------------------------------------

TOOL_CALLS = [{
    "id": "call_1",
    "type": "function",
    "function": {"name": "weather", "arguments": "{\"city\": \"北京\"}"},
}]


def test_encode_decode_roundtrip():
    payload = encode_tool_call_content("查询中", TOOL_CALLS)
    decoded = decode_tool_call_content(payload)
    assert decoded == ("查询中", TOOL_CALLS)


def test_decode_plain_content_returns_none():
    assert decode_tool_call_content("你好") is None
    assert decode_tool_call_content("") is None
    assert decode_tool_call_content("[已移交至模块 X]") is None  # 非工具轮文本


def test_decode_json_without_tool_calls_returns_none():
    # JSON 但无 tool_calls 键（普通 JSON 回复不误判）
    assert decode_tool_call_content('{"content": "你好"}') is None
    # 空列表视为普通文本（loop 只在 tool_calls 非空时编码）
    assert decode_tool_call_content('{"content": "你好", "tool_calls": []}') is None


def test_decode_malformed_json_returns_none():
    assert decode_tool_call_content('{"content": "截断...') is None
    assert decode_tool_call_content(None) is None


def test_summary_role_is_valid():
    msg = SessionMessage(role="summary", content="对话要点...", stage="compress")
    assert msg.to_dict()["role"] == "summary"


def test_to_from_dict_shape_unchanged():
    """载荷方案下 SessionMessage 序列化保持四字段（轨迹在 content 内）。"""
    msg = SessionMessage(role="assistant",
                         content=encode_tool_call_content("", TOOL_CALLS),
                         stage="agent")
    d = msg.to_dict()
    assert set(d) == {"role", "content", "stage", "metadata"}
    restored = SessionMessage.from_dict(d)
    assert decode_tool_call_content(restored.content) == ("", TOOL_CALLS)


# ---------------------------------------------------------------------------
# add_message + sink
# ---------------------------------------------------------------------------

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
# format_history：工具轮 JSON 载荷取内层文本
# ---------------------------------------------------------------------------

def test_format_history_decodes_tool_payload():
    cxt = DialogueContext(session_id="s1", user_query="q")
    cxt.add_message("user", "你好", stage="chat")
    cxt.add_message(
        "assistant", encode_tool_call_content("", TOOL_CALLS), stage="agent")
    cxt.add_message("tool", "晴 22 度", stage="agent",
                    metadata={"tool_call_id": "call_1"})
    cxt.add_message(
        "assistant",
        encode_tool_call_content("先查一下天气", TOOL_CALLS),
        stage="agent")
    cxt.add_message("assistant", "北京晴 22 度", stage="chat")

    formatted = cxt.format_history()
    assert "你好" in formatted
    assert "北京晴 22 度" in formatted
    assert "先查一下天气" in formatted      # 工具轮内层文本可见
    assert "tool_calls" not in formatted     # 载荷原文不泄漏
    lines = formatted.split("\n")
    assert "tool: 晴 22 度" not in lines     # tool 行不进业务模板
    assert formatted.count("assistant:") == 2  # 内层为空的工具轮不占行
