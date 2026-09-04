"""SQLite 会话持久化 —— 消息事实源（逐条 write-through）+ 状态快照 + 审计。

治理（TTL/逐出/存在性校验）在 main.py 内存中完成。消息经 ``attach`` 挂接
的 message_sink 逐条即时落库（``append_message``，DB 为事实源——中途 crash
不丢轮内消息）；轮末 ``save_snapshot`` 只回写 sessions 状态快照；startup
``load_active_sessions`` 恢复；压缩经 ``replace_history`` 重排。单连接 +
锁串行化（FastAPI sync 端点跑线程池，写流量极小）。
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

    def attach(self, session: Session) -> None:
        """挂接逐条 write-through：session 的每条 add_message 即时落库。

        sink 写失败在 ``DialogueContext.add_message`` 侧被吞（记日志不阻断
        对话）；launch 时 create_session 失败也 attach——DB 中途恢复时不丢
        消息。
        """
        session.cxt.message_sink = (
            lambda msg: self.append_message(session, msg))

    def save_snapshot(self, session: Session) -> None:
        """轮末回写 sessions 状态快照（模块/节点/槽位/活跃时间）。

        消息追加职责已移至 ``append_message``（attach 后逐条 write-through），
        本方法不再碰 messages 表——轮末一次事务回写状态即可。
        """
        now = time.time()
        filled_slots = json.dumps(session.cxt.filled_slots or {}, ensure_ascii=False)
        with self._lock, self._conn:
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
    # 逐条 write-through（DB 事实源）+ 压缩原语
    # ------------------------------------------------------------------

    def _current_epoch(self, session_id: str) -> int:
        """查 session 当代 epoch（无行视为 0；须持有 self._lock）。"""
        row = self._conn.execute(
            "SELECT launch_epoch FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row["launch_epoch"] if row is not None else 0

    def append_message(self, session: Session, msg: SessionMessage) -> None:
        """单条消息即时落库（message_sink 的写侧，add_message 逐条触发）。

        epoch 写时查询（与 save_turn 同 idiom）：内存逐出后重 launch 的在途轮
        会落到新代，与现状批量写的偏差同类，非本次引入。
        """
        with self._lock, self._conn:
            epoch = self._current_epoch(session.session_id)
            self._conn.execute(
                """INSERT INTO messages
                   (session_id, launch_epoch, role, content, stage, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    epoch,
                    msg.role,
                    msg.content,
                    msg.stage,
                    json.dumps(msg.metadata or {}, ensure_ascii=False),
                    time.time(),
                ),
            )

    def get_history(self, session_id: str) -> List[SessionMessage]:
        """当代 epoch 全量消息按 id 升序重建（压缩前 DB/内存对齐校验用）。

        tool 轨迹在 content/metadata 载荷里原样往返（无需专列）。
        """
        with self._lock:
            epoch = self._current_epoch(session_id)
            rows = self._conn.execute(
                """SELECT role, content, stage, metadata
                   FROM messages WHERE session_id = ? AND launch_epoch = ?
                   ORDER BY id""",
                (session_id, epoch),
            ).fetchall()
            return [
                SessionMessage(
                    role=r["role"],
                    content=r["content"],
                    stage=r["stage"],
                    metadata=json.loads(r["metadata"] or "{}"),
                )
                for r in rows
            ]

    def replace_history(
        self, session: Session, summary_text: str, keep_idx: int
    ) -> None:
        """压缩重排：删当代全部 → 插 summary 行 → 重插 ``history[keep_idx:]``。

        单事务；先做 DB 行数与 ``len(cxt.history)`` 对齐校验，不齐抛
        ``RuntimeError``（事务回滚，DB 原样），调用方捕获后放弃压缩——
        对不齐的历史绝不删。retained 行 id 会重排（AUTOINCREMENT 无法
        插到前面），summary 天然排最前。
        """
        now = time.time()
        with self._lock, self._conn:
            epoch = self._current_epoch(session.session_id)
            count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages"
                " WHERE session_id = ? AND launch_epoch = ?",
                (session.session_id, epoch),
            ).fetchone()["n"]
            if count != len(session.cxt.history):
                raise RuntimeError(
                    f"DB/内存消息数不齐，放弃压缩: session={session.session_id}"
                    f" db={count} mem={len(session.cxt.history)}"
                )
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND launch_epoch = ?",
                (session.session_id, epoch),
            )
            rows = [(
                session.session_id, epoch, "summary", summary_text, "compress",
                "{}", now,
            )]
            for msg in session.cxt.history[keep_idx:]:
                rows.append((
                    session.session_id, epoch, msg.role, msg.content, msg.stage,
                    json.dumps(msg.metadata or {}, ensure_ascii=False),
                    now,
                ))
            self._conn.executemany(
                """INSERT INTO messages
                   (session_id, launch_epoch, role, content, stage, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
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
