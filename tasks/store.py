"""任务持久化——与 sessions/messages 同库文件的独立连接（零触碰 chat/store.py）。

SQLite WAL 支持多连接并发；本表只在任务创建与终态两个时刻写库，
运行中状态走引擎内存（轮询读内存，不查库）。启动恢复：上一进程遗留的
pending/running 任务标记为 failed（后台线程随进程消亡，不可能续跑）。
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 非终态：进程重启即失联，恢复时统一改 failed
_ACTIVE_STATUSES = ("pending", "running")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    pattern_code  TEXT NOT NULL,
    status        TEXT NOT NULL,
    finish_reason TEXT,
    result_json   TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


class TaskStore:
    """tasks 表：task_id 主键，终态结果 JSON 快照。"""

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

    def create_task(self, task_id: str, session_id: str, pattern_code: str) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks"
                " (task_id, session_id, pattern_code, status, finish_reason,"
                "  result_json, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', NULL, '{}', ?, ?)",
                (task_id, session_id, pattern_code, now, now),
            )

    def mark_running(self, task_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET status = 'running', updated_at = ? WHERE task_id = ?",
                (time.time(), task_id),
            )

    def finish_task(
        self, task_id: str, status: str, finish_reason: Optional[str],
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """终态写库：status ∈ {done, failed} + 结果快照。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET status = ?, finish_reason = ?, result_json = ?,"
                " updated_at = ? WHERE task_id = ?",
                (
                    status,
                    finish_reason,
                    json.dumps(result or {}, ensure_ascii=False),
                    time.time(),
                    task_id,
                ),
            )

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["result"] = json.loads(d.pop("result_json") or "{}")
        except Exception:
            d["result"] = {}
        return d

    def fail_interrupted(self, reason: str = "服务重启中断") -> List[str]:
        """启动恢复：把遗留的 pending/running 任务改判 failed，返回其 task_id。"""
        now = time.time()
        with self._lock, self._conn:
            rows = self._conn.execute(
                f"SELECT task_id FROM tasks WHERE status IN (?, ?)",
                _ACTIVE_STATUSES,
            ).fetchall()
            ids = [r["task_id"] for r in rows]
            if ids:
                self._conn.execute(
                    f"UPDATE tasks SET status = 'failed', finish_reason = ?,"
                    f" updated_at = ? WHERE status IN (?, ?)",
                    (reason, now, *_ACTIVE_STATUSES),
                )
        if ids:
            logger.warning("恢复：上一进程遗留任务 %d 个改判 failed（%s）", len(ids), ids)
        return ids
