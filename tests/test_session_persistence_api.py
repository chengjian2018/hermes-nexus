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


def test_list_sessions_endpoint(client, store, registry_guard):
    """GET /sessions：默认列表 + pattern_code 过滤 + 分页参数。"""
    register_fake_provider()
    launch(client, "api-a")
    launch(client, "api-a2")
    launch(client, "api-b", pattern_code="not_registered_is_rejected")

    resp = client.get("/api/v1/sessions")
    body = resp.json()
    assert resp.status_code == 200
    assert body["code"] == "0"
    ids = [s["session_id"] for s in body["data"]["sessions"]]
    assert "api-a" in ids and "api-b" not in ids  # 未注册 pattern 的 launch 被拒

    resp = client.get("/api/v1/sessions", params={"pattern_code": "car_sales_route", "limit": 1})
    sessions = resp.json()["data"]["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["pattern_code"] == "car_sales_route"


def test_messages_endpoint_and_404(client, store, registry_guard):
    """GET /sessions/{id}/messages：全程消息；不存在返回 404 信封。"""
    register_fake_provider()
    launch(client, "api-msg")
    _use_fake_llm("api-msg")
    chat(client, "api-msg", "你好")

    resp = client.get("/api/v1/sessions/api-msg/messages")
    body = resp.json()
    assert resp.status_code == 200
    assert body["code"] == "0"
    msgs = body["data"]["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[-1]["role"] == "assistant"
    assert all("stage" in m and "created_at" in m for m in msgs)

    resp = client.get("/api/v1/sessions/no-such/messages")
    body = resp.json()
    assert resp.status_code == 200  # 业务码在信封里
    assert body["code"] == "404"
    assert body["status"] is False


def test_audit_endpoints_degraded_when_no_store(client, registry_guard):
    """store 未启用时审计端点返回 500 信封（降级可见）。"""
    import main

    prev = main.store
    main.store = None
    try:
        assert client.get("/api/v1/sessions").json()["code"] == "500"
        assert client.get("/api/v1/sessions/x/messages").json()["code"] == "500"
    finally:
        main.store = prev


def test_launch_persist_failure_degrades(client, store, registry_guard, monkeypatch):
    """launch 落盘失败：launch 响应不受影响，会话仍在内存可用。"""
    import main

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "create_session", boom)
    body = launch(client, "audit-degraded2")
    assert body["status"] is True
    assert "audit-degraded2" in main.all_sessions


def test_restart_recovery_restores_and_continues(client, store, registry_guard):
    """模拟重启：清空内存 → _restore_sessions → 会话还原且能继续对话、DB 流水连续。"""
    import main

    register_fake_provider()
    launch(client, "rs-1")
    _use_fake_llm("rs-1")
    chat(client, "rs-1", "你好")
    count_before = len(store.get_messages("rs-1"))

    # 模拟重启：内存清空
    with main._sessions_lock:
        main.all_sessions.clear()
        main._session_last_active.clear()

    restored = main._restore_sessions()
    assert restored >= 1
    session = main.all_sessions["rs-1"]
    assert session.pattern is not None  # pattern 从注册中心重新解析
    assert session.cxt.node_map and session.cxt.module_map  # 管线地图重新注入
    assert len(session.cxt.history) >= 2  # history 从 DB 还原
    assert "rs-1" in main._session_last_active  # 活跃时间已换算登记

    # 恢复后继续对话：新消息接在还原 history 之后，DB 侧流水连续
    session.cxt.llm_config = fake_llm_config()
    body = chat(client, "rs-1", "我想买车")
    assert body["status"] is True, body["message"]

    msgs = store.get_messages("rs-1")
    assert len(msgs) == count_before + 2  # user + assistant
    assert msgs[count_before]["role"] == "user"


def test_restore_skips_unregistered_pattern(client, store, registry_guard):
    """pattern_code 未注册的会话跳过恢复（不抛、不进内存）。"""
    import main
    from src.chat.session import Session

    store.create_session(Session(session_id="ghost", pattern_code="no_such_pattern"))
    restored = main._restore_sessions()
    assert "ghost" not in main.all_sessions
    assert restored == 0


def test_init_store_degrades_on_failure(monkeypatch):
    """配置/DB 初始化失败 → store=None 降级，不抛异常。"""
    import main

    prev = main.store

    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(main, "get_session_db_path", boom)
    main._init_store()
    assert main.store is None
    main.store = prev


def test_restore_failure_does_not_block(client, store, registry_guard, monkeypatch):
    """恢复过程异常不阻断：store 级抛错返回 0，单会话抛错跳过，均不向外传播。"""
    import main
    from src.chat.session import Session

    # 1) store 级失败（如 DB 读异常）：不抛，返回 0
    def store_boom(ttl):
        raise RuntimeError("db read down")

    monkeypatch.setattr(store, "load_active_sessions", store_boom)
    assert main._restore_sessions() == 0

    # 2) 单会话失败（如行数据损坏触发的任意异常）：跳过该会话，不阻断整体
    good = Session(session_id="rs-good", pattern_code="car_sales_route")
    bad = Session(session_id="rs-bad", pattern_code="car_sales_route")

    def fake_load(ttl):
        return [(good, main.time.time()), (bad, main.time.time())]

    monkeypatch.setattr(store, "load_active_sessions", fake_load)

    real_get = main.pattern_registry.get
    calls = []

    def get_or_raise(code):
        calls.append(code)
        if len(calls) == 2:
            raise RuntimeError("corrupted row")
        return real_get(code)

    monkeypatch.setattr(main.pattern_registry, "get", get_or_raise)

    restored = main._restore_sessions()
    assert restored == 1
    assert "rs-good" in main.all_sessions
    assert "rs-bad" not in main.all_sessions
