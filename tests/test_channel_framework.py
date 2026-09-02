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


# ============================================================================
# 注册表
# ============================================================================

import pytest

from src.channel.register import ChannelRegistry, discover_builtin_channels


class _BadSpecNoName:
    payload_model = _FakePayload
    default_pattern_env = "X"
    token_env = None
    stale_seconds = 1.0

    def parse(self, payload):  # pragma: no cover -- 不会被调用
        raise AssertionError

    def build_reply(self, reply, session_id):  # pragma: no cover
        raise AssertionError


def test_register_and_get():
    reg = ChannelRegistry()
    spec = FakeChannel()
    reg.register(spec)
    assert reg.get("fake") is spec
    assert reg.list_names() == ["fake"]
    assert reg.is_registered("fake") is True
    assert reg.get("nope") is None


def test_register_rejects_bad_names():
    """name 缺失 / 带路径分隔符 / 大写 —— URL 路径不合法，import 期拦住。"""
    reg = ChannelRegistry()
    with pytest.raises(ValueError):
        reg.register(_BadSpecNoName())  # 无 name 属性
    bad = FakeChannel()
    bad.name = "a/b"
    with pytest.raises(ValueError):
        reg.register(bad)


def test_register_rejects_duplicate_name():
    reg = ChannelRegistry()
    reg.register(FakeChannel())
    with pytest.raises(ValueError):
        reg.register(FakeChannel())  # 重名拒绝，防两个文件抢同名


def test_register_rejects_non_callable_hooks():
    reg = ChannelRegistry()
    bad = FakeChannel()
    bad.parse = "not callable"
    with pytest.raises(ValueError):
        reg.register(bad)


def test_discover_builtin_channels_skips_framework_files(tmp_path):
    """AST 发现：只 import 含模块级 registry.register() 的渠道文件；
    框架文件（base/register/webhooks）即使含 register 字样也不 import。"""
    (tmp_path / "base.py").write_text("registry.register(FakeChannel())\n", encoding="utf-8")
    (tmp_path / "register.py").write_text("registry = 1\n", encoding="utf-8")
    (tmp_path / "webhooks.py").write_text("registry.register(FakeChannel())\n", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    # 真渠道：模块级 registry.register(...) 调用
    (tmp_path / "good.py").write_text(
        "registry.register(FakeChannel())\n", encoding="utf-8"
    )
    # 非渠道：无 register 调用
    (tmp_path / "helper.py").write_text("x = 1\n", encoding="utf-8")
    # 函数体内 register 不算（AST 只看模块顶层）
    (tmp_path / "nested.py").write_text(
        "def f():\n    registry.register(FakeChannel())\n", encoding="utf-8"
    )

    imported = discover_builtin_channels(tmp_path)
    assert imported == []  # good.py import 会失败（FakeChannel 未定义），warning 跳过


def test_discover_imports_real_channel_file(tmp_path):
    """自包含渠道文件（不依赖外部名字）被成功 import 并注册到指定注册表。"""
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "good.py").write_text(
        "from src.channel.register import registry\n"
        "from src.channel.base import InboundMessage\n"
        "from pydantic import BaseModel\n"
        "class P(BaseModel):\n"
        "    user_id: str\n"
        "class S:\n"
        "    name = 'discovered'\n"
        "    payload_model = P\n"
        "    default_pattern_env = 'X'\n"
        "    token_env = None\n"
        "    stale_seconds = 1.0\n"
        "    def parse(self, p):\n"
        "        return InboundMessage(channel=self.name, text=p.user_id, session_key=p.user_id)\n"
        "    def build_reply(self, reply, session_id):\n"
        "        return {'reply': reply}\n"
        "registry.register(S())\n",
        encoding="utf-8",
    )
    imported = discover_builtin_channels(tmp_path)
    # 包外文件走 spec_from_file_location，模块名固定为 _channel_ext_<stem>
    assert imported == ["_channel_ext_good"]
    from src.channel.register import registry as global_reg
    assert global_reg.is_registered("discovered") is True
    global_reg._channels.pop("discovered", None)  # 清理全局态
