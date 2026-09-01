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


def test_create_session_keeps_old_trail(tmp_path):
    """同 session_id 重新 launch：旧 messages 保留（代次方案），sessions 行 epoch+1。"""
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

    assert fetch_one(db, "SELECT COUNT(*) FROM messages")[0] == 1
    row = fetch_one(db, "SELECT launch_epoch FROM sessions WHERE session_id = 's1'")
    assert row["launch_epoch"] == 1


def test_save_turn_writes_current_epoch(tmp_path):
    """重 launch 后：新消息带当代 epoch=1，旧消息 epoch=0。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    session = make_session()
    store.create_session(session)
    session.cxt.add_message("user", "第一代", stage="chat")
    store.save_turn(session, 0)

    store.create_session(make_session())  # 重新 launch，epoch=1
    session.cxt.add_message("user", "第二代", stage="chat")
    store.save_turn(session, 1)
    store.close()

    msgs = fetch_all(db, "SELECT content, launch_epoch FROM messages ORDER BY id")
    assert [(m["content"], m["launch_epoch"]) for m in msgs] == [
        ("第一代", 0),
        ("第二代", 1),
    ]


def test_load_active_sessions_restores_current_epoch_only(tmp_path):
    """恢复只取当代消息：旧代消息保留在 DB 但不进 history。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    session = make_session("alive")
    store.create_session(session)
    session.cxt.add_message("user", "旧代消息", stage="chat")
    store.save_turn(session, 0)

    store.create_session(make_session("alive"))  # epoch=1
    session.cxt.add_message("user", "当代消息", stage="chat")
    store.save_turn(session, 1)

    restored = store.load_active_sessions(ttl_seconds=3600)
    store.close()

    r, _ = restored[0]
    assert [m.content for m in r.cxt.history] == ["当代消息"]


def test_get_messages_includes_all_epochs(tmp_path):
    """审计：get_messages 返回全部代次消息，带 launch_epoch 键，id 升序。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    session = make_session()
    store.create_session(session)
    session.cxt.add_message("user", "第一代", stage="chat")
    store.save_turn(session, 0)
    store.create_session(make_session())  # epoch=1
    session.cxt.add_message("user", "第二代", stage="chat")
    store.save_turn(session, 1)

    msgs = store.get_messages("s1")
    store.close()
    assert [m["content"] for m in msgs] == ["第一代", "第二代"]
    assert [m["launch_epoch"] for m in msgs] == [0, 1]
    assert msgs[0]["id"] < msgs[1]["id"]


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


def _seed_two_sessions(store):
    """造两个会话各一轮对话，返回 (ids)。"""
    for sid in ("sa", "sb"):
        session = make_session(sid, pattern_code="car_sales_route" if sid == "sa" else "other")
        store.create_session(session)
        session.cxt.add_message("user", f"q-{sid}", stage="chat")
        session.cxt.add_message("assistant", f"a-{sid}", stage="chat")
        store.save_turn(session, 0)


def test_list_sessions_filter_and_order(tmp_path):
    """按 pattern_code 过滤、按 last_active_at 倒序、含 message_count。"""
    import time as _time

    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    _seed_two_sessions(store)
    # 把 sa 回拨为较旧
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE session_id = 'sa'",
            (_time.time() - 100,),
        )
    conn.close()

    all_rows = store.list_sessions(pattern_code=None, limit=50, offset=0)
    assert [r["session_id"] for r in all_rows] == ["sb", "sa"]  # 新的在前

    filtered = store.list_sessions(pattern_code="other", limit=50, offset=0)
    assert [r["session_id"] for r in filtered] == ["sb"]

    assert all_rows[0]["message_count"] == 2
    assert "pattern_code" in all_rows[0]
    store.close()


def test_list_sessions_pagination(tmp_path):
    """limit/offset 分页。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    _seed_two_sessions(store)

    page = store.list_sessions(pattern_code=None, limit=1, offset=1)
    assert len(page) == 1
    assert page[0]["session_id"] in ("sa", "sb")
    store.close()


def test_get_messages_ordered_and_typed(tmp_path):
    """消息按 id 升序、metadata 反序列化为 dict。"""
    db = str(tmp_path / "t.db")
    store = SessionStore(db)
    _seed_two_sessions(store)

    msgs = store.get_messages("sa")
    assert msgs is not None
    assert [m["content"] for m in msgs] == ["q-sa", "a-sa"]
    assert msgs[0]["id"] < msgs[1]["id"]
    assert isinstance(msgs[0]["metadata"], dict)
    assert msgs[0]["stage"] == "chat"
    store.close()


def test_get_messages_missing_session(tmp_path):
    """会话不存在返回 None（端点转 404 信封）。"""
    store = SessionStore(str(tmp_path / "t.db"))
    assert store.get_messages("nope") is None
    store.close()
