"""闲鱼 channel 适配器测试 —— 单元层（注入 fake 引擎操作）+ main.app 集成层。

单元层自建 FastAPI app 注入 fake launch/get/run，覆盖 session 派生、自动
launch、过期消息吞掉、token 校验与错误响应契约；集成层走 main.app 全链路
（会话治理 + store 落盘），main.chat 打桩保持离线。
"""

import os
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.chat.store import SessionStore
from src.channel.base import EngineOps
from src.channel.webhooks import build_channel_router
from src.channel.xianyu import XianyuChannel


# ============================================================================
# 单元层：注入 fake 引擎操作的 router 测试台
# ============================================================================

class ChannelHarness:
    """fake 引擎操作 + 独立 FastAPI app，记录调用供断言。

    pattern/token 通过环境变量注入（通用 handler 每请求读取），post() 帮助
    方法在请求期间设置并在结束后还原。
    """

    def __init__(self, pattern_code="demo_pattern", token=None):
        self.pattern_code = pattern_code
        self.token = token
        self._env = {}
        if pattern_code is not None:
            self._env["XIANYU_CHANNEL_PATTERN"] = pattern_code
        if token is not None:
            self._env["XIANYU_CHANNEL_TOKEN"] = token
        self.sessions = {}
        self.launch_calls = []
        self.run_calls = []
        self.launch_error = None  # (code, message)，模拟 launch 失败
        self.run_error = None

        def launch_session(pattern_code, session_id, task_info, request_id, exist_ok=False):
            self.launch_calls.append(
                {
                    "pattern_code": pattern_code,
                    "session_id": session_id,
                    "task_info": task_info,
                    "exist_ok": exist_ok,
                }
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
            XianyuChannel(),
            EngineOps(
                get_session=get_session,
                launch_session=launch_session,
                run_chat_turn=run_chat_turn,
            ),
        ))
        self.client = TestClient(app)

    def post(self, path, json=None, params=None):
        """带 env 注入的 POST：请求期间设置环境变量，结束还原。"""
        saved = {k: os.environ.get(k) for k in self._env}
        try:
            os.environ.update(self._env)
            return self.client.post(path, json=json, params=params)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def inbound(**overrides):
    """标准入站载荷，字段可覆盖。"""
    payload = {
        "account_id": "acc1",
        "message": "你好",
        "chat_id": "chat1",
        "item_id": "item1",
        "send_user_id": "buyer1",
        "send_user_name": "买家小张",
    }
    payload.update(overrides)
    return payload


def test_first_message_auto_launches():
    """首条消息自动 launch：session_id 派生、task_info 提取、exist_ok 语义。"""
    h = ChannelHarness()
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "echo:你好"
    assert body["session_id"] == "xianyu:acc1:chat1"

    assert len(h.launch_calls) == 1
    call = h.launch_calls[0]
    assert call["session_id"] == "xianyu:acc1:chat1"
    assert call["pattern_code"] == "demo_pattern"
    assert call["exist_ok"] is True
    assert call["task_info"] == {
        "channel": "xianyu",
        "account_id": "acc1",
        "item_id": "item1",
        "buyer_user_id": "buyer1",
        "buyer_user_name": "买家小张",
    }


def test_second_message_reuses_session():
    """同会话第二条消息复用既有 session，不再 launch。"""
    h = ChannelHarness()
    h.post("/api/v1/channel/xianyu", json=inbound())
    h.post("/api/v1/channel/xianyu", json=inbound(message="多少钱"))

    assert len(h.launch_calls) == 1
    assert h.run_calls == [
        ("xianyu:acc1:chat1", "你好"),
        ("xianyu:acc1:chat1", "多少钱"),
    ]


def test_unknown_session_without_pattern_503():
    """会话不存在且未配置 pattern：503，不触碰引擎。"""
    h = ChannelHarness(pattern_code=None)
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 503
    assert h.launch_calls == [] and h.run_calls == []


def test_launch_failure_maps_500():
    """自动 launch 失败（如 pattern 未注册）映射 500。"""
    h = ChannelHarness()
    h.launch_error = ("404", "pattern_code 'x' 未注册")
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 500
    assert "未注册" in resp.json()["detail"]


def test_run_error_maps_500():
    """引擎单轮异常映射 500，不带 reply（对方不会发送任何内容）。"""
    h = ChannelHarness()
    h.run_error = RuntimeError("LLM 超时")
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 500
    assert "LLM 超时" in resp.json()["detail"]


def test_stale_message_swallowed():
    """过期消息（重连重放）吞掉：200 + 空 reply，不 launch 不对话。"""
    h = ChannelHarness()
    stale_ms = str(int((time.time() - 600) * 1000))
    resp = h.post(
        "/api/v1/channel/xianyu", json=inbound(msg_time=stale_ms)
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == ""
    assert h.launch_calls == [] and h.run_calls == []


def test_unparseable_msg_time_passes_through():
    """msg_time 格式无法识别时不过滤，正常对话。"""
    h = ChannelHarness()
    resp = h.post(
        "/api/v1/channel/xianyu", json=inbound(msg_time="不是时间")
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "echo:你好"


def test_token_rejects_wrong_and_accepts_right():
    """配置 token 后：错 token 403，对 token 放行。"""
    h = ChannelHarness(token="s3cret")
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 403

    resp = h.post(
        "/api/v1/channel/xianyu", json=inbound(), params={"token": "s3cret"}
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "echo:你好"


def test_missing_required_field_422():
    """缺必填字段：422（非 200，对方不发送）。"""
    h = ChannelHarness()
    payload = inbound()
    del payload["message"]
    resp = h.post("/api/v1/channel/xianyu", json=payload)
    assert resp.status_code == 422


def test_success_body_has_no_fallback_keys():
    """成功响应体不得携带 data/content/message 键：reply 为空时对方会依次
    取这三个键，误带会把调试信息发给买家。"""
    h = ChannelHarness()
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert set(resp.json().keys()) <= {"reply", "session_id"}


# ============================================================================
# 集成层：main.app 全链路（会话治理 + store 落盘；main.chat 打桩离线）
# ============================================================================

@pytest.fixture(scope="module")
def client():
    import main  # noqa: F401 -- 导入即完成 discover + channel 接线
    return TestClient(main.app)


@pytest.fixture()
def store(tmp_path):
    """给 main 注入 tmp DB 的 store，用完还原并关闭。"""
    import main

    s = SessionStore(str(tmp_path / "channel.db"))
    prev = main.store
    main.store = s
    yield s
    main.store = prev
    s.close()


@pytest.fixture()
def registry_guard():
    """清空全局会话注册表，用例结束后还原快照（同持久化测试）。"""
    import main

    with main._sessions_lock:
        snap_sessions = dict(main.all_sessions)
        snap_ts = dict(main._session_last_active)
        main.all_sessions.clear()
        main._session_last_active.clear()
    yield
    with main._sessions_lock:
        main.all_sessions.clear()
        main.all_sessions.update(snap_sessions)
        main._session_last_active.clear()
        main._session_last_active.update(snap_ts)


@pytest.fixture()
def fake_chat(monkeypatch):
    """把 main.chat 打桩为固定回复并按真实行为落 history，保持测试离线；
    返回 (session_id, query) 调用记录。"""
    import main

    calls = []

    def _chat(query, session_id, all_sessions):
        calls.append((session_id, query))
        session = all_sessions[session_id]
        session.cxt.add_message("user", query, stage="chat")
        reply = f"auto:{query}"
        session.cxt.add_message("assistant", reply, stage="chat")
        return reply

    monkeypatch.setattr(main, "chat", _chat)
    return calls


def test_channel_end_to_end(client, store, registry_guard, fake_chat, monkeypatch):
    """首条消息：自动 launch（真治理 + 落盘）→ 引擎对话 → reply 契约。"""
    monkeypatch.setenv("XIANYU_CHANNEL_PATTERN", "car_sales_route")
    resp = client.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "auto:你好"
    assert body["session_id"] == "xianyu:acc1:chat1"

    import main

    session = main.all_sessions["xianyu:acc1:chat1"]
    assert session.pattern_code == "car_sales_route"
    assert session.cxt.metadata["task_info"]["item_id"] == "item1"

    rows = store.list_sessions()
    assert [r["session_id"] for r in rows] == ["xianyu:acc1:chat1"]
    assert rows[0]["pattern_code"] == "car_sales_route"

    msgs = store.get_messages("xianyu:acc1:chat1")
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "你好"


def test_channel_second_turn_appends(
    client, store, registry_guard, fake_chat, monkeypatch
):
    """第二条消息复用会话：落盘消息追加，会话行不重复。"""
    monkeypatch.setenv("XIANYU_CHANNEL_PATTERN", "car_sales_route")
    client.post("/api/v1/channel/xianyu", json=inbound())
    first_count = len(store.get_messages("xianyu:acc1:chat1"))

    client.post("/api/v1/channel/xianyu", json=inbound(message="能便宜点吗"))
    msgs = store.get_messages("xianyu:acc1:chat1")
    assert len(msgs) > first_count
    assert len(store.list_sessions()) == 1
    assert fake_chat[-1] == ("xianyu:acc1:chat1", "能便宜点吗")


def test_channel_no_pattern_503(client, registry_guard, fake_chat, monkeypatch):
    """未配置 XIANYU_CHANNEL_PATTERN 且会话不存在：503 不对话。"""
    monkeypatch.delenv("XIANYU_CHANNEL_PATTERN", raising=False)
    resp = client.post("/api/v1/channel/xianyu", json=inbound(chat_id="chat-new"))
    assert resp.status_code == 503
    assert fake_chat == []
