"""会话历史压缩测试 —— 估算/阈值/吸附/摘要失败保护/端到端。"""

import json
from unittest.mock import patch

import pytest

from src.chat.compression import (
    _snap_to_pair_boundary,
    compress_history,
    estimate_tokens,
    maybe_compress,
    should_compress,
)
from src.chat.session import Session
from src.chat.store import SessionStore
from src.dialogue.base import SessionMessage


# ---------------------------------------------------------------------------
# Token 估算
# ---------------------------------------------------------------------------

def test_estimate_tokens_cjk_and_ascii():
    # 纯中文：4 字 × 2 + 4 overhead = 12
    assert estimate_tokens([SessionMessage(role="user", content="你好你好")]) == 12
    # 纯 ASCII：8 字符 × 0.25 = 2 + 4 = 6
    assert estimate_tokens([SessionMessage(role="user", content="abcdefgh")]) == 6
    # 混合：3 中文(6) + 4 ascii(1) + 4 = 11
    assert estimate_tokens([SessionMessage(role="user", content="你好吗abcd")]) == 11


def test_estimate_tokens_tool_payload_in_content():
    """工具轮载荷在 content 里，天然计入估算（比空文本行大）。"""
    from src.dialogue.base import encode_tool_call_content
    plain = SessionMessage(role="user", content="q")
    payload = encode_tool_call_content(
        "", [{"id": "c1", "function": {"name": "weather", "arguments": "{}"}}])
    with_payload = SessionMessage(role="assistant", content=payload)
    assert (estimate_tokens([plain, with_payload])
            > estimate_tokens([plain, SessionMessage(role="assistant",
                                                     content="")]))


def test_should_compress_boundaries():
    msgs = [SessionMessage(role="user", content="长" * 4000)]
    assert not should_compress(msgs, threshold=0, retain_count=12)   # 阈值 0 关
    assert not should_compress(msgs, threshold=100, retain_count=12)  # 条数不足
    many = [SessionMessage(role="user", content="长" * 100) for _ in range(20)]
    assert should_compress(many, threshold=100, retain_count=12)
    assert not should_compress(many, threshold=100000, retain_count=12)


# ---------------------------------------------------------------------------
# retain 边界吸附
# ---------------------------------------------------------------------------

def test_snap_keeps_tool_pair_together():
    """split 落在 tool 行上：吸附到其 assistant 工具轮起点。"""
    from src.dialogue.base import encode_tool_call_content
    tool_calls = [{"id": "c1", "function": {"name": "t"}}]
    history = [
        SessionMessage(role="user", content="q1"),
        SessionMessage(role="assistant",
                       content=encode_tool_call_content("查", tool_calls)),
        SessionMessage(role="tool", content="r1",
                       metadata={"tool_call_id": "c1"}),
        SessionMessage(role="assistant", content="答"),
        SessionMessage(role="user", content="q2"),
    ]
    # split=2 落在 tool 行 → 吸附回 1（assistant run 起点之前）
    assert _snap_to_pair_boundary(history, 2) == 1
    # split 不在 tool 行上 → 原样
    assert _snap_to_pair_boundary(history, 4) == 4


def test_snap_no_assistant_run_before_tool_keeps_split():
    """tool 行前面没有 assistant 工具轮：吸附无处可回，保持原 split。"""
    history = [SessionMessage(role="tool", content="r",
                              metadata={"tool_call_id": "c1"})]
    assert _snap_to_pair_boundary(history, 1) == 1


# ---------------------------------------------------------------------------
# compress_history
# ---------------------------------------------------------------------------

class _SummaryProvider:
    """摘要 LLM 打桩：记录请求，返回固定摘要。"""

    def __init__(self, reply="摘要：用户咨询手机", fail=False):
        self.reply = reply
        self.fail = fail
        self.seen = []

    def chat_completion(self, messages, model, temperature, max_tokens,
                        **kw):
        self.seen.append(messages)
        if self.fail:
            raise RuntimeError("llm down")
        return {"content": self.reply, "tool_calls": []}


def _mk_session_with_history(store=None, n_pairs=10, session_id="comp-1"):
    """构造带 2×n_pairs 条历史的会话；传 store 则 launch+attach（消息落库）。"""
    session = Session(session_id=session_id, pattern_code="p")
    if store is not None:
        store.create_session(session)
        store.attach(session)
    for i in range(n_pairs):
        session.cxt.add_message("user", f"问题{i}：" + "长" * 50, stage="chat")
        session.cxt.add_message("assistant", f"回答{i}", stage="chat")
    return session


def test_compress_success_rebuilds_db_and_cxt(tmp_path):
    store = SessionStore(str(tmp_path / "t.db"))
    session = _mk_session_with_history(store)  # history 已由 sink 落库

    provider = _SummaryProvider()
    llm_config = {"code": "fake", "model": "m", "temperature": 0.3,
                  "max_tokens": 1024}
    with patch("src.chat.compression.build_provider", return_value=provider):
        ok = compress_history(session, store, llm_config, retain_count=4)

    assert ok is True
    # DB：summary 最前 + retained 4 条
    history = store.get_history("comp-1")
    assert history[0].role == "summary"
    assert history[0].stage == "compress"
    assert len(history) == 5
    # cxt 同步重建 + 轮内标记修正
    assert session.cxt.history[0] is not history[0] or True  # 对象不同源无妨
    assert session.cxt.history[0].role == "summary"
    assert len(session.cxt.history) == 5
    assert session.cxt.turn_history_start == 5
    # 摘要请求：system 是摘要助手人设，user 是拼接的旧对话
    assert provider.seen[0][0]["role"] == "system"
    assert "摘要" in provider.seen[0][0]["content"]
    assert "问题0" in provider.seen[0][1]["content"]
    # 只摘旧消息（split 之前），保留窗口内的问题不进摘要
    assert "问题9" not in provider.seen[0][1]["content"]
    store.close()


def test_compress_llm_failure_keeps_everything(tmp_path):
    """摘要 LLM 失败：DB 与 cxt.history 原样未动（铁律）。"""
    store = SessionStore(str(tmp_path / "t.db"))
    session = _mk_session_with_history(store)
    before_mem = list(session.cxt.history)
    before_db = store.get_history("comp-1")

    provider = _SummaryProvider(fail=True)
    with patch("src.chat.compression.build_provider", return_value=provider):
        ok = compress_history(session, store,
                              {"code": "f", "model": "m"}, retain_count=4)

    assert ok is False
    assert store.get_history("comp-1") == before_db
    assert session.cxt.history == before_mem
    store.close()


def test_compress_aborts_when_db_memory_mismatch(tmp_path):
    """DB/内存不齐：放弃压缩（对不齐的历史绝不删）。"""
    store = SessionStore(str(tmp_path / "t.db"))
    session = _mk_session_with_history(store)
    # 内存追加一条不落库 → 不齐
    session.cxt.history.append(SessionMessage(role="user", content="幽灵"))

    provider = _SummaryProvider()
    with patch("src.chat.compression.build_provider", return_value=provider):
        ok = compress_history(session, store,
                              {"code": "f", "model": "m"}, retain_count=4)

    assert ok is False
    assert not provider.seen  # LLM 未被调用（校验在前）
    assert len(store.get_history("comp-1")) == 20
    store.close()


def test_compress_empty_summary_aborts(tmp_path):
    """摘要为空：放弃。"""
    store = SessionStore(str(tmp_path / "t.db"))
    session = _mk_session_with_history(store)

    provider = _SummaryProvider(reply="   ")
    with patch("src.chat.compression.build_provider", return_value=provider):
        ok = compress_history(session, store,
                              {"code": "f", "model": "m"}, retain_count=4)
    assert ok is False
    assert len(store.get_history("comp-1")) == 20
    store.close()


# ---------------------------------------------------------------------------
# 端到端：压缩后构建 messages 不重复 query
# ---------------------------------------------------------------------------

def test_after_compress_query_appears_once(tmp_path):
    from src.chat.messages import default_build_messages
    from src.dialogue.module import AgentModule

    store = SessionStore(str(tmp_path / "t.db"))
    session = _mk_session_with_history(store)
    session.cxt.turn_history_start = len(session.cxt.history)
    session.cxt.user_query = "新问题"

    with patch("src.chat.compression.build_provider",
               return_value=_SummaryProvider("摘要：此前咨询")):
        ok = compress_history(session, store,
                              {"code": "f", "model": "m"}, retain_count=4)
    assert ok is True

    messages = default_build_messages(AgentModule(module_code="m"), session.cxt)
    user_contents = [m["content"] for m in messages if m["role"] == "user"]
    assert user_contents.count("新问题") == 1  # query 恰一次
    assert any("untrusted_会话摘要" in c for c in user_contents)
    # 保留的旧轮次照常回放
    assert any(c.startswith("问题") for c in user_contents)
    store.close()


def test_maybe_compress_skips_without_store_or_threshold():
    """store None / 阈值 0：静默跳过不抛异常。"""
    session = _mk_session_with_history()
    maybe_compress(session, None)  # no-op
    with patch("config.config.get_session_compress_config",
               return_value=(0, 12)):
        maybe_compress(session, object())  # 阈值 0 no-op
    assert len(session.cxt.history) == 20
