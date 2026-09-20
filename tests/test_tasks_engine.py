"""任务引擎单测——终态检测（end_module/max_turns/timeout/对端耗尽）+ TaskStore 往返。

全离线：pattern 侧与 llm 对端均走 fake_provider（llm_override 注入）；
对端用 scripted 脚本驱动。引擎依赖注入 main 的 core 函数（同
TestClient 测试的注入风格），全局会话表/注册表用 guard 保护。
"""

import time

import pytest

from dialogue.register import registry as pattern_registry
from chat.store import SessionStore
from tasks.engine import EngineDeps, TaskEngine
from tasks.store import TaskStore
from templates.compiler import compile_template

from fake_provider import fake_llm_config, register_fake_provider


@pytest.fixture(autouse=True)
def _registry_cleanup():
    yield
    for code in list(pattern_registry.list_codes()):
        if code.startswith("tmp_"):
            pattern_registry.deregister(code)


@pytest.fixture(autouse=True)
def _sessions_guard():
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
def session_store(tmp_path):
    import main

    s = SessionStore(str(tmp_path / "sessions.db"))
    prev = main.store
    main.store = s
    yield s
    main.store = prev
    s.close()


@pytest.fixture()
def task_store(tmp_path):
    return TaskStore(str(tmp_path / "sessions.db"))  # 与会话同库文件（多连接 WAL）


@pytest.fixture()
def engine(session_store, task_store):
    import main

    return TaskEngine(EngineDeps(
        launch_session=main._launch_session_core,
        touch_session=main._touch_session_threadsafe,
        all_sessions=main.all_sessions,
        get_session_store=lambda: main.store,
        get_task_store=lambda: task_store,
    ))


def register_agent_pattern(code, entry_is_end):
    pattern_registry.register(compile_template({
        "code": code, "name": "引擎测试", "description": "d",
        "entry_module_code": "talk",
        "modules": [
            {"module_code": "talk", "type": "agent", "module_name": "洽谈",
             "base_prompt": "你是代聊助手", "is_end": entry_is_end},
        ],
    }))


def wait_terminal(engine, task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = engine.get(task_id)
        if view and view["status"] in ("done", "failed"):
            return view
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内进入终态")


def test_end_module_on_first_turn(engine, task_store):
    register_fake_provider()
    register_agent_pattern("tmp_task_end", entry_is_end=True)

    task_id, session_id, err = engine.start(
        pattern_code="tmp_task_end",
        task_info={"goal": "订今晚7点的位子"},
        counterpart={"mode": "scripted", "script": ["好的"]},
        kickoff="你好，我想订今晚7点4个人的位子",
        llm_override=fake_llm_config(),
    )
    assert err is None and task_id is not None

    view = wait_terminal(engine, task_id)
    assert view["status"] == "done"
    assert view["finish_reason"] == "end_module"
    assert view["turn_count"] == 1
    assert view["result"]["end_module_code"] == "talk"
    roles = [m["role"] for m in view["result"]["messages"]]
    assert roles[0] == "user" and "assistant" in roles  # kickoff + 模版回复
    # 落库终态一致
    row = task_store.get_task(task_id)
    assert row["status"] == "done" and row["finish_reason"] == "end_module"
    assert row["result"]["turn_count"] == 1


def test_max_turns(engine):
    register_fake_provider()
    register_agent_pattern("tmp_task_max", entry_is_end=False)

    task_id, _, err = engine.start(
        pattern_code="tmp_task_max",
        counterpart={"mode": "scripted",
                     "script": [f"第{i}条" for i in range(20)]},
        max_turns=3,
        kickoff="开始",
        llm_override=fake_llm_config(),
    )
    assert err is None
    view = wait_terminal(engine, task_id)
    assert view["status"] == "done"
    assert view["finish_reason"] == "max_turns"
    assert view["turn_count"] == 3


def test_counterpart_exhausted(engine):
    register_fake_provider()
    register_agent_pattern("tmp_task_exh", entry_is_end=False)

    task_id, _, err = engine.start(
        pattern_code="tmp_task_exh",
        counterpart={"mode": "scripted", "script": ["只有一条"]},
        max_turns=10,
        kickoff="开始",
        llm_override=fake_llm_config(),
    )
    assert err is None
    view = wait_terminal(engine, task_id)
    assert view["status"] == "done"
    assert view["finish_reason"] == "counterpart_exhausted"
    assert view["turn_count"] == 2  # kickoff 轮 + 对端脚本那条轮


def test_timeout(engine):
    register_fake_provider()
    register_agent_pattern("tmp_task_to", entry_is_end=False)

    task_id, _, err = engine.start(
        pattern_code="tmp_task_to",
        counterpart={"mode": "scripted",
                     "script": [f"r{i}" for i in range(20)]},
        timeout_s=0,  # 首轮后立即判超时
        kickoff="开始",
        llm_override=fake_llm_config(),
    )
    assert err is None
    view = wait_terminal(engine, task_id)
    assert view["finish_reason"] == "timeout"
    assert view["turn_count"] == 1


def test_llm_counterpart_with_fake_provider(engine):
    register_fake_provider()
    register_agent_pattern("tmp_task_llmcp", entry_is_end=False)

    task_id, _, err = engine.start(
        pattern_code="tmp_task_llmcp",
        counterpart={"mode": "llm", "role_prompt": "你是忙碌的餐厅老板",
                     "llm_override": fake_llm_config()},
        max_turns=2,
        kickoff="你好，想订位",
        llm_override=fake_llm_config(),
    )
    assert err is None
    view = wait_terminal(engine, task_id)
    assert view["status"] == "done"
    assert view["finish_reason"] == "max_turns"
    assert view["turn_count"] == 2


def test_unknown_pattern_rejected(engine):
    task_id, session_id, err = engine.start(
        pattern_code="no_such_pattern",
        counterpart={"mode": "scripted", "script": ["x"]},
    )
    assert task_id is None
    assert err is not None and "未注册" in err


def test_channel_counterpart_rejected(engine):
    register_agent_pattern("tmp_task_chan", entry_is_end=True)
    task_id, _, err = engine.start(
        pattern_code="tmp_task_chan",
        counterpart={"mode": "channel"},
    )
    assert task_id is None
    assert "未实现" in err


def test_task_store_roundtrip_and_fail_interrupted(tmp_path):
    store = TaskStore(str(tmp_path / "t.db"))
    store.create_task("t1", "s1", "tmp_x")
    store.mark_running("t1")
    assert store.get_task("t1")["status"] == "running"

    store.finish_task("t1", "done", "end_module", {"turn_count": 1})
    row = store.get_task("t1")
    assert row["status"] == "done"
    assert row["finish_reason"] == "end_module"
    assert row["result"]["turn_count"] == 1

    # 重启恢复：遗留 active 任务改判 failed
    store.create_task("t2", "s2", "tmp_x")
    store.mark_running("t2")
    interrupted = store.fail_interrupted()
    assert interrupted == ["t2"]
    assert store.get_task("t2")["status"] == "failed"
    assert store.get_task("t2")["finish_reason"] == "服务重启中断"
    store.close()


def test_engine_recover_interrupted(engine, task_store):
    task_store.create_task("legacy", "s-legacy", "tmp_old")
    assert engine.recover_interrupted() == ["legacy"]
    assert task_store.get_task("legacy")["status"] == "failed"
