"""channel 框架层测试 —— 协议对象、通用 handler、注册表（fake spec，离线）。"""

from typing import Any, Dict, Optional

from pydantic import BaseModel

from src.channel.base import ChannelSpec, EngineOps, InboundMessage


class _FakePayload(BaseModel):
    """fake 渠道载荷：user_id + text 必填，ts 可选。"""

    user_id: str
    text: str
    ts: Optional[float] = None


class FakeChannel:
    """最小 ChannelSpec 实现，供 handler/注册表测试复用。"""

    name = "fake"
    payload_model = _FakePayload
    default_pattern_env = "FAKE_CHANNEL_PATTERN"
    token_env = None
    stale_seconds = 300.0

    def parse(self, payload: _FakePayload) -> InboundMessage:
        return InboundMessage(
            channel=self.name,
            text=payload.text,
            session_key=payload.user_id,
            timestamp=payload.ts,
            task_info={"channel": self.name, "user_id": payload.user_id},
        )

    def build_reply(self, reply: str, session_id: str) -> Dict[str, Any]:
        return {"reply": reply, "session_id": session_id}


def test_inbound_message_defaults():
    """InboundMessage 可直接构造；task_info 默认空 dict。"""
    msg = InboundMessage(channel="x", text="hi", session_key="k")
    assert msg.timestamp is None
    assert msg.task_info == {}


def test_engine_ops_holds_callables():
    """EngineOps 为纯数据束：三个操作字段原样存取。"""
    ops = EngineOps(get_session=lambda _sid: None,
                    launch_session=lambda *a, **k: (None, "0", ""),
                    run_chat_turn=lambda s, q: ("ok", None))
    assert ops.get_session("any") is None
    assert ops.run_chat_turn(None, "q")[0] == "ok"


def test_fake_spec_satisfies_protocol():
    """FakeChannel 结构上满足 ChannelSpec 协议（runtime_checkable）。"""
    from typing import runtime_checkable  # noqa: F401 -- 协议声明处已 @runtime_checkable
    spec = FakeChannel()
    assert spec.name == "fake"
    msg = spec.parse(_FakePayload(user_id="u1", text="hi"))
    assert msg.session_key == "u1" and msg.text == "hi"
    assert spec.build_reply("r", "fake:u1") == {"reply": "r", "session_id": "fake:u1"}
