"""会话持久化 API 集成测试 —— launch/chat 落盘、审计端点、重启恢复。

TestClient 不用 with（不触发 startup），store 由 fixture 显式注入
tmp DB；registry_guard 清空/还原全局会话表防污染。
"""

import pytest
from fastapi.testclient import TestClient

from fake_provider import fake_llm_config, register_fake_provider
from src.chat.store import SessionStore


@pytest.fixture(scope="module")
def client():
    import main  # noqa: F401 -- 导入即完成 discover_builtin_tools/patterns
    return TestClient(main.app)


@pytest.fixture()
def store(tmp_path):
    """给 main 注入 tmp DB 的 store，用完还原并关闭。"""
    import main

    s = SessionStore(str(tmp_path / "audit.db"))
    prev = main.store
    main.store = s
    yield s
    main.store = prev
    s.close()


@pytest.fixture()
def registry_guard():
    """清空全局会话注册表，用例结束后还原快照（同治理测试）。"""
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


def launch(client, session_id, pattern_code="car_sales_route"):
    resp = client.post(
        "/api/v1/launch",
        json={
            "request_id": f"req-{session_id}",
            "session_id": session_id,
            "pattern_code": pattern_code,
            "task_info": {"caller": "pytest"},
        },
    )
    assert resp.status_code == 200
    return resp.json()


def chat(client, session_id, query):
    resp = client.post(
        "/api/v1/chat",
        json={
            "request_id": f"req-chat-{session_id}",
            "session_id": session_id,
            "query": query,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def _use_fake_llm(session_id):
    import main

    main.all_sessions[session_id].cxt.llm_config = fake_llm_config()


def test_launch_chat_persisted(client, store, registry_guard):
    """launch → chat：sessions 行 + 增量消息落盘（首条 user、末条 assistant）。"""
    register_fake_provider()
    assert launch(client, "audit-1")["status"] is True
    _use_fake_llm("audit-1")

    body = chat(client, "audit-1", "你好")
    assert body["status"] is True, body["message"]

    rows = store.list_sessions()
    assert [r["session_id"] for r in rows] == ["audit-1"]
    assert rows[0]["pattern_code"] == "car_sales_route"
    assert rows[0]["message_count"] >= 2

    msgs = store.get_messages("audit-1")
    assert msgs[0]["role"] == "user" and msgs[0]["stage"] == "chat"
    assert msgs[-1]["role"] == "assistant"


def test_chat_turn_incremental_append(client, store, registry_guard):
    """第二轮只追加第二轮消息，history 在 DB 侧连续。"""
    register_fake_provider()
    launch(client, "audit-2")
    _use_fake_llm("audit-2")
    chat(client, "audit-2", "你好")
    first_count = len(store.get_messages("audit-2"))

    chat(client, "audit-2", "我想买车")
    second_count = len(store.get_messages("audit-2"))
    assert second_count > first_count
    msgs = store.get_messages("audit-2")
    assert msgs[first_count]["role"] == "user"  # 新一轮从 user 开始


def test_store_failure_does_not_block(client, store, registry_guard, monkeypatch):
    """DB 写失败：仅记日志，chat 响应不受影响。"""
    register_fake_provider()
    launch(client, "audit-degraded")
    _use_fake_llm("audit-degraded")

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "save_turn", boom)
    body = chat(client, "audit-degraded", "你好")
    assert body["status"] is True, body["message"]


def test_launch_persist_failure_degrades(client, store, registry_guard, monkeypatch):
    """launch 落盘失败：launch 响应不受影响，会话仍在内存可用。"""
    import main

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "create_session", boom)
    body = launch(client, "audit-degraded2")
    assert body["status"] is True
    assert "audit-degraded2" in main.all_sessions
