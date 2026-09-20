"""同 session 并发轮次串行化测试——Session.turn_lock 保证轮次原子性。

T1 轮次进行中（LLM 调用阻塞）时，T2 的并发轮次必须等在锁上：
begin_turn 不得提前改写 user_query / history（否则轮次状态互相踩踏）。
"""

import threading
import time
from unittest.mock import patch

from chat.session import Session
from dialogue.module import AgentModule
from dialogue.pattern import Pattern


def _mk_session():
    talk = AgentModule(
        module_code="talk",
        module_name="闲聊",
        module_description="自由对话",
        module_todo_description="闲聊应对",
    )
    p = Pattern(code="p", name="t", description="t",
                entry_module_code="talk", modules=[talk])
    s = Session(session_id="s", pattern_code="p")
    s.pattern = p
    s.cxt.module_map = p.module_map
    s.cxt.node_map = p.node_map
    s.cxt.current_module_code = "talk"
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    return s


class BlockingProvider:
    """第一次调用阻塞到 release 置位；之后直通——用于观察轮次互斥。"""

    def __init__(self):
        self.in_flight = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def chat_completion(self, messages, model, temperature, max_tokens,
                        tools=None, tool_choice=None, **kw):
        self.calls += 1
        if self.calls == 1:
            self.in_flight.set()
            assert self.release.wait(timeout=5), "第一轮 LLM 调用未被释放"
        return {"content": "收到", "tool_calls": []}


def test_concurrent_turns_serialize_on_session_lock():
    from chat.chat import chat as chat_fn

    s = _mk_session()
    provider = BlockingProvider()
    sessions = {"s": s}

    with patch("chat.loop.build_provider", return_value=provider):
        t1 = threading.Thread(
            target=lambda: chat_fn(query="第一条消息", session_id="s",
                                   all_sessions=sessions))
        t1.start()
        # T1 已进入 LLM 调用（轮次进行中）
        assert provider.in_flight.wait(timeout=5)

        t2 = threading.Thread(
            target=lambda: chat_fn(query="第二条消息", session_id="s",
                                   all_sessions=sessions))
        t2.start()
        time.sleep(0.3)

        # T2 必须还等在锁上：begin_turn 未执行（user_query 仍是 T1 的），
        # T2 的用户消息未入 history
        assert s.cxt.user_query == "第一条消息"
        assert not any(m.role == "user" and m.content == "第二条消息"
                       for m in s.cxt.history)

        provider.release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not t1.is_alive() and not t2.is_alive()

    # 两轮完整落地：各一条 user + 一条 assistant，顺序不交错
    trail = [(m.role, m.content) for m in s.cxt.history
             if m.content in ("第一条消息", "第二条消息", "收到")]
    assert trail == [
        ("user", "第一条消息"), ("assistant", "收到"),
        ("user", "第二条消息"), ("assistant", "收到"),
    ]
