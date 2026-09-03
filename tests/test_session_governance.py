"""会话治理（main.py）单元测试。

覆盖：launch 重复 session_id 报错、TTL 过期清理、数量上限逐出最旧、
chat 滑动续期、并发 launch 竞争。均为进程内调用，不访问真实 API。
"""

import threading
import time

import pytest

from fake_provider import fake_llm_config, register_fake_provider


def launch(client, session_id, pattern_code="car_sales_route"):
    """发起对话任务并返回响应 JSON。"""
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
    """发起对话请求并返回响应 JSON。"""
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


@pytest.fixture(scope="module")
def client():
    """导入 main.py（触发工具/pattern 自动发现）并返回 TestClient。"""
    from fastapi.testclient import TestClient

    import main  # noqa: F401 -- 导入即完成 discover_builtin_tools/patterns

    return TestClient(main.app)


@pytest.fixture()
def registry_guard():
    """清空全局会话注册表，用例结束后还原快照，避免跨用例/跨文件互相污染。"""
    import main

    with main._sessions_lock:
        snapshot_sessions = dict(main.all_sessions)
        snapshot_ts = dict(main._session_last_active)
        main.all_sessions.clear()
        main._session_last_active.clear()
    yield
    with main._sessions_lock:
        main.all_sessions.clear()
        main.all_sessions.update(snapshot_sessions)
        main._session_last_active.clear()
        main._session_last_active.update(snapshot_ts)


def test_launch_duplicate_session_id(client, registry_guard):
    """重复 session_id 的 launch -> 返回 409 业务码，原会话不被覆盖。"""
    import main

    assert launch(client, "gov-dup")["status"] is True
    original = main.all_sessions["gov-dup"]

    body = launch(client, "gov-dup")
    assert body["status"] is False
    assert body["code"] == "409"
    assert "已存在" in body["message"]

    # 原会话对象未被静默替换
    assert main.all_sessions["gov-dup"] is original


def test_session_ttl_expiry(client, registry_guard, monkeypatch):
    """超过 TTL 未活跃的会话被清理：chat 返回 404，同 id 可重新 launch。"""
    import main

    assert launch(client, "gov-ttl")["status"] is True

    # 将活跃时间回拨到 TTL 之前，模拟长时间无活动
    monkeypatch.setattr(main, "SESSION_TTL_SECONDS", 60)
    with main._sessions_lock:
        main._session_last_active["gov-ttl"] = time.monotonic() - 61

    body = chat(client, "gov-ttl", "你好")
    assert body["status"] is False
    assert body["code"] == "404"
    assert "已过期" in body["message"]

    # 已过期的会话被清理，同一 session_id 可重新发起
    assert "gov-ttl" not in main.all_sessions
    assert launch(client, "gov-ttl")["status"] is True


def test_max_sessions_evicts_oldest(client, registry_guard, monkeypatch):
    """会话数达到上限时逐出最近活跃最早的会话。"""
    import main

    monkeypatch.setattr(main, "MAX_SESSIONS", 2)

    launch(client, "gov-a")
    time.sleep(0.01)  # 保证活跃时间戳可区分先后
    launch(client, "gov-b")
    time.sleep(0.01)
    launch(client, "gov-c")  # 达到上限后再新增

    assert len(main.all_sessions) == 2
    assert "gov-a" not in main.all_sessions  # 最旧的被逐出
    assert "gov-b" in main.all_sessions
    assert "gov-c" in main.all_sessions
    assert set(main._session_last_active) == set(main.all_sessions)


def test_chat_refreshes_ttl(client, registry_guard):
    """chat 命中会话时刷新活跃时间（滑动续期）。"""
    import main

    register_fake_provider()
    launch(client, "gov-refresh")
    main.all_sessions["gov-refresh"].cxt.metadata["llm_override"] = fake_llm_config()

    # 模拟活跃时间停在 1 秒前
    with main._sessions_lock:
        main._session_last_active["gov-refresh"] = time.monotonic() - 1

    body = chat(client, "gov-refresh", "你好")
    assert body["status"] is True, body["message"]

    with main._sessions_lock:
        refreshed_ts = main._session_last_active["gov-refresh"]
    assert refreshed_ts > time.monotonic() - 1


def test_concurrent_duplicate_launch(registry_guard):
    """并发 launch 同一 session_id：仅一个成功，其余返回 409。

    直接调用同步端点函数（FastAPI 线程池中同样以多线程方式执行），
    验证锁保证下的重复检查与登记的原子性。
    """
    import main
    from main import DialogueRequest

    barrier = threading.Barrier(8)
    results = []

    def worker():
        request = DialogueRequest(
            request_id="req-gov-race",
            session_id="gov-race",
            pattern_code="car_sales_route",
            task_info={"caller": "pytest"},
        )
        barrier.wait()
        response = main.launch_dialogue(request)
        results.append(response.code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("0") == 1
    assert results.count("409") == 7
    assert list(main.all_sessions) == ["gov-race"]
    assert list(main._session_last_active) == ["gov-race"]
