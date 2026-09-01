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
    launch_epoch        INTEGER NOT NULL DEFAULT 0,
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
    launch_epoch INTEGER NOT NULL DEFAULT 0,
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

        同 session_id 重新 launch 视为新一代（launch_epoch + 1）：
        旧代 messages 审计流水原地保留，sessions 行 upsert（created_at 重置）。
        """
        now = time.time()
        request_id = (session.cxt.metadata or {}).get("request_id")
        task_info = json.dumps(session.task_info or {}, ensure_ascii=False)
        filled_slots = json.dumps(session.cxt.filled_slots or {}, ensure_ascii=False)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT launch_epoch FROM sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            epoch = (row["launch_epoch"] + 1) if row is not None else 0
            self._conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (session_id, pattern_code, launch_epoch, request_id, task_info,
                    current_module_code, current_node_code, filled_slots,
                    created_at, last_active_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.pattern_code,
                    epoch,
                    request_id,
                    task_info,
                    session.cxt.current_module_code,
                    session.cxt.current_node_code,
                    filled_slots,
                    now,
                    now,
                ),
            )

    # ------------------------------------------------------------------
    # 轮末落盘
    # ------------------------------------------------------------------

    def save_turn(self, session: Session, start_idx: int) -> None:
        """轮末落盘：追加 ``cxt.history[start_idx:]`` 新消息 + 回写状态快照。

        一个事务；``start_idx`` 为本轮开始时的 ``len(cxt.history)`` 快照。
        """
        now = time.time()
        filled_slots = json.dumps(session.cxt.filled_slots or {}, ensure_ascii=False)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT launch_epoch FROM sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            epoch = row["launch_epoch"] if row is not None else 0
            rows = [
                (
                    session.session_id,
                    epoch,
                    msg.role,
                    msg.content,
                    msg.stage,
                    json.dumps(msg.metadata or {}, ensure_ascii=False),
                    now,
                )
                for msg in session.cxt.history[start_idx:]
            ]
            if rows:
                self._conn.executemany(
                    """INSERT INTO messages
                       (session_id, launch_epoch, role, content, stage, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
            self._conn.execute(
                """UPDATE sessions
                   SET current_module_code = ?, current_node_code = ?,
                       filled_slots = ?, last_active_at = ?
                   WHERE session_id = ?""",
                (
                    session.cxt.current_module_code,
                    session.cxt.current_node_code,
                    filled_slots,
                    now,
                    session.session_id,
                ),
            )

    # ------------------------------------------------------------------
    # 重启恢复
    # ------------------------------------------------------------------

    def load_active_sessions(self, ttl_seconds: float) -> List[Tuple[Session, float]]:
        """加载 ``last_active_at`` 未过期的会话（startup 恢复用）。

        Returns:
            ``(Session, last_active_at 墙钟)`` 列表。Session.pattern 为 None、
            node_map/module_map 为空——由调用方从注册中心解析注入。
        """
        cutoff = time.time() - ttl_seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE last_active_at >= ?"
                " AND launch_epoch = (SELECT MAX(launch_epoch) FROM sessions s2"
                "                      WHERE s2.session_id = sessions.session_id)"
                " ORDER BY last_active_at DESC",
                (cutoff,),
            ).fetchall()
            restored: List[Tuple[Session, float]] = []
            for row in rows:
                msgs = self._conn.execute(
                    "SELECT role, content, stage, metadata FROM messages"
                    " WHERE session_id = ? AND launch_epoch = ? ORDER BY id",
                    (row["session_id"], row["launch_epoch"]),
                ).fetchall()
                session = Session(
                    session_id=row["session_id"],
                    pattern_code=row["pattern_code"],
                )
                session.task_info = json.loads(row["task_info"] or "{}")
                session.cxt.metadata["task_info"] = session.task_info
                session.cxt.metadata["request_id"] = row["request_id"]
                session.cxt.current_module_code = row["current_module_code"]
                session.cxt.current_node_code = row["current_node_code"]
                session.cxt.filled_slots = json.loads(row["filled_slots"] or "{}")
                session.cxt.history = [
                    SessionMessage(
                        role=m["role"],
                        content=m["content"],
                        stage=m["stage"],
                        metadata=json.loads(m["metadata"] or "{}"),
                    )
                    for m in msgs
                ]
                restored.append((session, row["last_active_at"]))
            return restored

    # ------------------------------------------------------------------
    # 审计查询（只读）
    # ------------------------------------------------------------------

    def list_sessions(
        self,
        pattern_code: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """会话列表（按 last_active_at 倒序），含消息计数。"""
        sql = """
            SELECT s.session_id, s.pattern_code, s.launch_epoch,
                   s.current_module_code,
                   s.current_node_code, s.created_at, s.last_active_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id)
                       AS message_count
            FROM sessions s
        """
        params: List[Any] = []
        if pattern_code:
            sql += " WHERE s.pattern_code = ?"
            params.append(pattern_code)
        sql += " ORDER BY s.last_active_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_messages(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        """某会话全程消息（含所有代次，带 launch_epoch；按 id 升序）；不存在返回 None。"""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if exists is None:
                return None
            rows = self._conn.execute(
                """SELECT id, launch_epoch, role, content, stage, metadata, created_at
                   FROM messages WHERE session_id = ? ORDER BY id""",
                (session_id,),
            ).fetchall()
            messages = []
            for r in rows:
                d = dict(r)
                d["metadata"] = json.loads(d["metadata"] or "{}")
                messages.append(d)
            return messages
