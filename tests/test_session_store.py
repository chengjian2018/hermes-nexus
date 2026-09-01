"""SessionStore 单元测试 —— 建表/落盘/增量追加/恢复/查询。

全部使用 tmp 文件 DB + 原生 sqlite3 断言（不经过被测读 API），
fake session 手工构造，不依赖 FastAPI 与 LLM。
"""

import json
import sqlite3

from src.chat.session import Session
from src.chat.store import SessionStore


def make_session(session_id="s1", pattern_code="car_sales_route"):
    """构造一个带任务信息的最小 Session。"""
    session = Session(session_id=session_id, pattern_code=pattern_code)
    session.task_info = {"caller": "pytest"}
    session.cxt.metadata["request_id"] = f"req-{session_id}"
    return session


def fetch_one(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def fetch_all(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def test_init_idempotent(tmp_path):
    """重复对同一文件建 store（幂等 DDL）不抛异常。"""
    db = str(tmp_path / "t.db")
    store1 = SessionStore(db)
    store1.close()
    store2 = SessionStore(db)  # 不抛即通过
    store2.close()


def test_create_session_roundtrip(tmp_path):
    """launch 落盘：sessions 行字段与 JSON 列往返一致。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    session = make_session()
    session.cxt.current_module_code = "car_sales_root"
    session.cxt.current_node_code = "route_root"
    session.cxt.filled_slots = {"brand": "特斯拉"}
    store.create_session(session)
    store.close()

    row = fetch_one(db, "SELECT * FROM sessions WHERE session_id = 's1'")
    assert row is not None
    assert row["pattern_code"] == "car_sales_route"
    assert row["request_id"] == "req-s1"
    assert json.loads(row["task_info"]) == {"caller": "pytest"}
    assert row["current_module_code"] == "car_sales_root"
    assert row["current_node_code"] == "route_root"
    assert json.loads(row["filled_slots"]) == {"brand": "特斯拉"}
    assert row["created_at"] > 0 and row["last_active_at"] > 0


def test_create_session_replaces_old_trail(tmp_path):
    """同 session_id 重新 launch：旧 messages 被清，sessions 行被替换。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    store.create_session(make_session())
    # 手工塞一条旧消息模拟上一轮流水
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, stage, metadata, created_at)"
            " VALUES ('s1', 'user', '旧消息', 'chat', '{}', 1.0)"
        )
    conn.close()

    store.create_session(make_session())  # 重新 launch
    store.close()

    assert fetch_one(db, "SELECT COUNT(*) FROM messages")[0] == 0


def test_save_turn_appends_incrementally(tmp_path):
    """两轮对话：save_turn 只追加本轮新增消息，状态快照整体回写。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    session = make_session()
    store.create_session(session)

    # 第一轮：user + assistant
    session.cxt.add_message("user", "你好", stage="chat")
    session.cxt.add_message("assistant", "您好", stage="chat")
    store.save_turn(session, 0)

    # 第二轮前状态变化 + 新消息（start_idx=2 只追加第二轮）
    session.cxt.filled_slots["brand"] = "特斯拉"
    session.cxt.current_node_code = "buy_ask_budget"
    session.cxt.add_message("user", "我想买车", stage="chat")
    session.cxt.add_message("assistant", "回复", stage="chat")
    store.save_turn(session, 2)
    store.close()

    msgs = fetch_all(db, "SELECT * FROM messages ORDER BY id")
    assert [m["content"] for m in msgs] == ["你好", "您好", "我想买车", "回复"]
    assert all(m["session_id"] == "s1" for m in msgs)
    assert msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant"

    row = fetch_one(db, "SELECT * FROM sessions WHERE session_id = 's1'")
    assert row["current_node_code"] == "buy_ask_budget"
    assert json.loads(row["filled_slots"]) == {"brand": "特斯拉"}
    # 第二轮后 last_active_at 被刷新（大于 created_at）
    assert row["last_active_at"] >= row["created_at"]


def test_save_turn_message_metadata_json(tmp_path):
    """消息 metadata 列 JSON 往返。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    session = make_session()
    store.create_session(session)
    session.cxt.add_message(
        "tool", "tool_result", stage="agent", metadata={"tool": "calculator"}
    )
    store.save_turn(session, 0)
    store.close()

    row = fetch_one(db, "SELECT metadata FROM messages")
    assert json.loads(row[0]) == {"tool": "calculator"}


def test_save_turn_empty_increment_no_rows(tmp_path):
    """start_idx 之后无新消息时不插入行，仅刷新状态（不抛异常）。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    session = make_session()
    store.create_session(session)
    session.cxt.add_message("user", "q", stage="chat")
    store.save_turn(session, 0)
    store.save_turn(session, 1)  # 无新增
    store.close()

    assert fetch_one(db, "SELECT COUNT(*) FROM messages")[0] == 1


def test_load_active_sessions_restores_fields(tmp_path):
    """恢复：history/filled_slots/当前节点/任务信息还原；pattern 留空由调用方解析。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    session = make_session("alive")
    session.cxt.current_module_code = "car_sales_root"
    session.cxt.current_node_code = "menu_sales"
    session.cxt.filled_slots = {"brand": "特斯拉"}
    store.create_session(session)
    session.cxt.add_message("user", "你好", stage="chat")
    session.cxt.add_message("assistant", "您好", stage="chat")
    store.save_turn(session, 0)

    restored = store.load_active_sessions(ttl_seconds=3600)
    store.close()

    assert len(restored) == 1
    r, last_active = restored[0]
    assert isinstance(last_active, float) and last_active > 0
    assert r.session_id == "alive"
    assert r.pattern_code == "car_sales_route"
    assert r.pattern is None
    assert r.task_info == {"caller": "pytest"}
    assert r.cxt.metadata["request_id"] == "req-alive"
    assert r.cxt.current_module_code == "car_sales_root"
    assert r.cxt.current_node_code == "menu_sales"
    assert r.cxt.filled_slots == {"brand": "特斯拉"}
    assert [(m.role, m.content) for m in r.cxt.history] == [
        ("user", "你好"),
        ("assistant", "您好"),
    ]


def test_load_active_sessions_filters_expired(tmp_path):
    """超过 ttl 未活跃的会话不恢复。"""
    import time as _time

    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    store.create_session(make_session("alive"))
    store.create_session(make_session("dead"))
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE session_id = 'dead'",
            (_time.time() - 9999,),
        )
    conn.close()

    restored = store.load_active_sessions(ttl_seconds=3600)
    store.close()

    assert [s.session_id for s, _ in restored] == ["alive"]
