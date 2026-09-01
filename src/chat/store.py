"""SQLite 会话持久化 —— 审计流水 + 重启恢复数据源（write-through，非事实源）。

治理（TTL/逐出/存在性校验）在 main.py 内存中完成；本模块只在 launch/轮末
落盘、startup 恢复、审计查询时被调用。单连接 + 锁串行化（FastAPI sync
端点跑线程池，写流量极小）。
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.chat.session import Session
from src.dialogue.base import SessionMessage

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    pattern_code        TEXT NOT NULL,
    request_id          TEXT,
    task_info           TEXT NOT NULL DEFAULT '{}',
    current_module_code TEXT,
    current_node_code   TEXT,
    filled_slots        TEXT NOT NULL DEFAULT '{}',
    created_at          REAL NOT NULL,
    last_active_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active_at);
CREATE INDEX IF NOT EXISTS idx_sessions_pattern    ON sessions(pattern_code);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    stage      TEXT NOT NULL DEFAULT '',
    metadata   TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


class SessionStore:
    """会话审计存储：sessions（状态快照）+ messages（行级消息流水）。"""

    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # launch 落盘
    # ------------------------------------------------------------------

    def create_session(self, session: Session) -> None:
        """落盘一个新会话（launch 时调用）。

        同 session_id 重新 launch 视为新审计流水：先清旧 messages，
        再 upsert sessions 行，一个事务。
        """
        now = time.time()
        request_id = (session.cxt.metadata or {}).get("request_id")
        task_info = json.dumps(session.task_info or {}, ensure_ascii=False)
        filled_slots = json.dumps(session.cxt.filled_slots or {}, ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session.session_id,)
            )
            self._conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (session_id, pattern_code, request_id, task_info,
                    current_module_code, current_node_code, filled_slots,
                    created_at, last_active_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.pattern_code,
                    request_id,
                    task_info,
                    session.cxt.current_module_code,
                    session.cxt.current_node_code,
                    filled_slots,
                    now,
                    now,
                ),
            )
