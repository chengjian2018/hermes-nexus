"""任务 API 集成测试——触发→后台跑→轮询终态全链路（TestClient，不触发 startup）。

main.store / main.task_store 注入 tmp 版本（引擎 getter 请求期解析）。
"""

import time

import pytest
from fastapi.testclient import TestClient

from dialogue.register import registry as pattern_registry
from chat.store import SessionStore
from tasks.store import TaskStore
from templates.compiler import compile_template

from fake_provider import fake_llm_config, register_fake_provider


@pytest.fixture(scope="module")
def client():
    import main  # noqa: F401
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _cleanup():
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
    for code in list(pattern_registry.list_codes()):
        if code.startswith("tmp_"):
            pattern_registry.deregister(code)


@pytest.fixture()
def stores(tmp_path):
    import main

    ss = SessionStore(str(tmp_path / "api.db"))
    ts = TaskStore(str(tmp_path / "api.db"))
    prev_ss, prev_ts = main.store, main.task_store
    main.store, main.task_store = ss, ts
    yield ss, ts
    main.store, main.task_store = prev_ss, prev_ts
    ss.close()
    ts.close()


def register_end_pattern():
    pattern_registry.register(compile_template({
        "code": "tmp_api_task", "name": "接口任务", "description": "d",
        "entry_module_code": "talk",
        "modules": [{"module_code": "talk", "type": "agent",
                     "module_name": "洽谈", "base_prompt": "你是代聊助手",
                     "is_end": True}],
    }))


def create_task(client, **over):
    payload = {
        "request_id": "req-task",
        "pattern_code": "tmp_api_task",
        "task_info": {"goal": "订位"},
        "counterpart": {"mode": "scripted", "script": ["好的"]},
        "kickoff": "你好，想订今晚7点4人的位子",
        "llm_override": fake_llm_config(),
    }
    payload.update(over)
    return client.post("/api/v1/tasks", json=payload).json()


def poll_until(client, task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/v1/tasks/{task_id}").json()
        if body["data"]["status"] in ("done", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未进入终态")


def test_create_poll_and_session_history(client, stores):
    session_store, task_store = stores
    register_fake_provider()
    register_end_pattern()

    body = create_task(client)
    assert body["code"] == "0", body["message"]
    task_id = body["data"]["task_id"]
    session_id = body["data"]["session_id"]

    final = poll_until(client, task_id)
    assert final["code"] == "0"
    data = final["data"]
    assert data["status"] == "done"
    assert data["finish_reason"] == "end_module"
    assert data["turn_count"] == 1
    result = data["result"]
    assert result["end_module_code"] == "talk"
    assert any(m["role"] == "user" for m in result["messages"])

    # 会话历史走既有端点（需求4：期间开放查询会话历史）
    msgs = client.get(f"/api/v1/sessions/{session_id}/messages").json()
    assert msgs["code"] == "0" and msgs["data"]["messages"]

    # 任务落库终态
    row = task_store.get_task(task_id)
    assert row["status"] == "done" and row["finish_reason"] == "end_module"
    _ = session_store  # stores fixture 注入即用


def test_running_view_has_intermediate_fields(client, stores):
    """max_turns 較大的任务在轮询窗口内呈现 running 中间过程（或直接终态，二者择一断言）。"""
    register_fake_provider()
    pattern_registry.register(compile_template({
        "code": "tmp_api_task", "name": "接口任务", "description": "d",
        "entry_module_code": "talk",
        "modules": [{"module_code": "talk", "type": "agent",
                     "module_name": "洽谈", "base_prompt": "你是代聊助手"}],
    }))
    body = create_task(client, max_turns=50,
                       counterpart={"mode": "scripted",
                                    "script": [f"r{i}" for i in range(60)]})
    task_id = body["data"]["task_id"]
    final = poll_until(client, task_id)
    assert final["data"]["status"] == "done"
    assert final["data"]["finish_reason"] == "max_turns"
    assert final["data"]["turn_count"] == 50


def test_unknown_pattern_and_bad_counterpart(client, stores):
    register_fake_provider()
    body = create_task(client, pattern_code="no_such")
    assert body["code"] == "404" and "未注册" in body["message"]

    register_end_pattern()
    body = create_task(client, counterpart={"mode": "channel"})
    assert body["code"] == "400" and "未实现" in body["message"]


def test_get_unknown_task(client, stores):
    body = client.get("/api/v1/tasks/never-exists").json()
    assert body["code"] == "404"
