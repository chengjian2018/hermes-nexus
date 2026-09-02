"""channel 框架层测试 —— 协议对象、通用 handler、注册表（fake spec，离线）。"""

from types import SimpleNamespace
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


# ============================================================================
# 通用 handler（fake 引擎操作 + FakeChannel / 定制 fake spec）
# ============================================================================

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.channel.base import EngineOps
from src.channel.webhooks import build_channel_router


class HandlerHarness:
    """fake 引擎操作 + 单渠道 app，记录调用供断言（对齐闲鱼测试形态）。"""

    def __init__(self, spec=None, pattern_code="demo_pattern", token=None,
                 stale_seconds=300.0):
        self.spec = spec or FakeChannel()
        if token is not None:
            self.spec.token_env = "FAKE_CHANNEL_TOKEN"
        self.pattern_code = pattern_code
        self.sessions = {}
        self.launch_calls = []
        self.run_calls = []
        self.launch_error = None
        self.run_error = None

        def launch_session(pattern_code, session_id, task_info, request_id, exist_ok=False):
            self.launch_calls.append(
                {"pattern_code": pattern_code, "session_id": session_id,
                 "task_info": task_info, "exist_ok": exist_ok}
            )
            if self.launch_error is not None:
                return None, self.launch_error[0], self.launch_error[1]
            if session_id in self.sessions:
                return self.sessions[session_id], "0", "已存在"
            sess = SimpleNamespace(session_id=session_id)
            self.sessions[session_id] = sess
            return sess, "0", "ok"

        def get_session(session_id):
            return self.sessions.get(session_id)

        def run_chat_turn(session, query):
            self.run_calls.append((session.session_id, query))
            if self.run_error is not None:
                return None, self.run_error
            return f"echo:{query}", None

        app = FastAPI()
        app.include_router(build_channel_router(
            self.spec,
            EngineOps(get_session=get_session, launch_session=launch_session,
                      run_chat_turn=run_chat_turn),
        ))
        self.client = TestClient(app)


def _post(h, json=None, params=None, env=None):
    """带环境变量隔离的 POST（pattern/token env 用完还原）。"""
    import os
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.environ.pop("FAKE_CHANNEL_PATTERN", None)
    for k, v in (env or {}).items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        return h.client.post(
            f"/api/v1/channel/{h.spec.name}",
            json=json if json is not None else {"user_id": "u1", "text": "你好"},
            params=params,
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_handler_success_and_session_prefix():
    """成功路径：session_id 拼渠道前缀，task_info 透传，reply 契约。"""
    h = HandlerHarness()
    resp = _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reply": "echo:你好", "session_id": "fake:u1"}
    call = h.launch_calls[0]
    assert call["session_id"] == "fake:u1"
    assert call["pattern_code"] == "demo_pattern"
    assert call["exist_ok"] is True
    assert call["task_info"] == {"channel": "fake", "user_id": "u1"}
    assert h.run_calls == [("fake:u1", "你好")]


def test_handler_existing_session_skips_launch():
    """会话已存在：复用，不 launch。"""
    h = HandlerHarness()
    _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    _post(h, json={"user_id": "u1", "text": "第二条"}, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert len(h.launch_calls) == 1
    assert h.run_calls[-1] == ("fake:u1", "第二条")


def test_handler_no_pattern_503():
    """会话不存在且 pattern env 未配置：503，提示设置哪个 env。"""
    h = HandlerHarness(pattern_code=None)
    resp = _post(h, env={})
    assert resp.status_code == 503
    assert "FAKE_CHANNEL_PATTERN" in resp.json()["detail"]
    assert h.launch_calls == [] and h.run_calls == []


def test_handler_launch_failure_500():
    h = HandlerHarness()
    h.launch_error = ("404", "pattern 'x' 未注册")
    resp = _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 500
    assert "未注册" in resp.json()["detail"]


def test_handler_run_error_500():
    h = HandlerHarness()
    h.run_error = RuntimeError("LLM 超时")
    resp = _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 500
    assert "LLM 超时" in resp.json()["detail"]


def test_handler_stale_message_swallowed():
    """过期消息：200 + 空 reply，不 launch 不对话（重连重放防护）。"""
    h = HandlerHarness()
    resp = _post(h, json={"user_id": "u1", "text": "hi", "ts": time.time() - 600},
                 env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == ""
    assert h.launch_calls == [] and h.run_calls == []


def test_handler_none_timestamp_bypasses_stale():
    """timestamp=None：不做过期过滤。"""
    h = HandlerHarness()
    resp = _post(h, json={"user_id": "u1", "text": "hi", "ts": None},
                 env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "echo:hi"


def test_handler_fresh_message_passes():
    """新鲜消息正常处理。"""
    h = HandlerHarness()
    resp = _post(h, json={"user_id": "u1", "text": "hi", "ts": time.time()},
                 env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "echo:hi"


def test_handler_token_wrong_403_right_200(monkeypatch):
    """token_env 配置了非空 env 才启用校验：错 403，对放行。"""
    monkeypatch.setenv("FAKE_CHANNEL_TOKEN", "s3cret")
    h = HandlerHarness(token="s3cret")
    resp = _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 403
    assert h.run_calls == []

    resp = _post(h, params={"token": "s3cret"},
                 env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200


def test_handler_token_env_unset_no_check(monkeypatch):
    """token_env 指了 env 名但 env 未配置：不校验（可选密钥语义）。"""
    monkeypatch.delenv("FAKE_CHANNEL_TOKEN", raising=False)
    h = HandlerHarness(token="s3cret")  # token_env 已指向 FAKE_CHANNEL_TOKEN
    resp = _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200


def test_handler_payload_invalid_422():
    """载荷缺必填字段：422（pydantic 自动）。"""
    h = HandlerHarness()
    resp = _post(h, json={"user_id": "u1"}, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 422
