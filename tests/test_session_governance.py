"""Session governance (main.py) unit tests.

Covers: launch rejecting a duplicate session_id, TTL expiry cleanup,
evicting the oldest once the session cap is reached, chat sliding renewal,
and concurrent launch races. All in-process calls, no real API access.
"""

import threading
import time

import pytest

from fake_provider import fake_llm_config, register_fake_provider


def launch(client, session_id, pattern_code="xianyu_agent"):
    """Launch a dialogue task and return the response JSON."""
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
    """Send a chat request and return the response JSON."""
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
    """Import main.py (triggering tool/pattern auto-discovery) and return a TestClient."""
    from fastapi.testclient import TestClient

    import main  # noqa: F401 -- importing it completes discover_builtin_tools/patterns

    return TestClient(main.app)


@pytest.fixture()
def registry_guard():
    """Clear the global session registry and restore the snapshot afterwards, avoiding cross-test/cross-file pollution."""
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
    """launch with a duplicate session_id -> 409 business code; the original session is not overwritten."""
    import main

    assert launch(client, "gov-dup")["status"] is True
    original = main.all_sessions["gov-dup"]

    body = launch(client, "gov-dup")
    assert body["status"] is False
    assert body["code"] == "409"
    assert "已存在" in body["message"]

    # the original session object was not silently replaced
    assert main.all_sessions["gov-dup"] is original


def test_session_ttl_expiry(client, registry_guard, monkeypatch):
    """Sessions inactive beyond the TTL are cleaned up: chat returns 404 and the same id can launch again."""
    import main

    assert launch(client, "gov-ttl")["status"] is True

    # Roll the last-active timestamp back before the TTL to simulate a long idle period
    monkeypatch.setattr(main, "SESSION_TTL_SECONDS", 60)
    with main._sessions_lock:
        main._session_last_active["gov-ttl"] = time.monotonic() - 61

    body = chat(client, "gov-ttl", "你好")
    assert body["status"] is False
    assert body["code"] == "404"
    assert "已过期" in body["message"]

    # The expired session was cleaned up; the same session_id can launch again
    assert "gov-ttl" not in main.all_sessions
    assert launch(client, "gov-ttl")["status"] is True


def test_max_sessions_evicts_oldest(client, registry_guard, monkeypatch):
    """When the session count reaches the cap, the least recently active session is evicted."""
    import main

    monkeypatch.setattr(main, "MAX_SESSIONS", 2)

    launch(client, "gov-a")
    time.sleep(0.01)  # ensure the last-active timestamps are orderable
    launch(client, "gov-b")
    time.sleep(0.01)
    launch(client, "gov-c")  # add one more after reaching the cap

    assert len(main.all_sessions) == 2
    assert "gov-a" not in main.all_sessions  # the oldest was evicted
    assert "gov-b" in main.all_sessions
    assert "gov-c" in main.all_sessions
    assert set(main._session_last_active) == set(main.all_sessions)


def test_chat_refreshes_ttl(client, registry_guard):
    """chat refreshes the last-active time when the session is hit (sliding renewal)."""
    import main

    register_fake_provider()
    launch(client, "gov-refresh")
    main.all_sessions["gov-refresh"].cxt.metadata["llm_override"] = fake_llm_config()

    # Simulate the last-active time frozen one second ago
    with main._sessions_lock:
        main._session_last_active["gov-refresh"] = time.monotonic() - 1

    body = chat(client, "gov-refresh", "你好")
    assert body["status"] is True, body["message"]

    with main._sessions_lock:
        refreshed_ts = main._session_last_active["gov-refresh"]
    assert refreshed_ts > time.monotonic() - 1


def test_concurrent_duplicate_launch(registry_guard):
    """Concurrent launches of the same session_id: exactly one succeeds, the rest get 409.

    Calls the sync endpoint function directly (FastAPI's thread pool runs it
    multi-threaded the same way), verifying that the duplicate check plus
    registration are atomic under the lock.
    """
    import main
    from main import DialogueRequest

    barrier = threading.Barrier(8)
    results = []

    def worker():
        request = DialogueRequest(
            request_id="req-gov-race",
            session_id="gov-race",
            pattern_code="xianyu_agent",
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
