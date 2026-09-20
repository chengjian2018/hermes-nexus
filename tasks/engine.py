"""自主任务执行引擎（Q11 fire-and-forget，仓库首个后台线程）。

每任务一条守护线程：kickoff → chat_turn（LLM 慢调用锁外执行，复刻
_run_chat_turn_core 的"锁外跑 + 轮末 save_snapshot"纪律）→ 终态检测
（conversation_end action / 模块 is_end / 节点 is_end，覆盖 Agent/FSM/混排）
→ 对端产下一条 query → 循环，直到终态 / max_turns / timeout / 异常。

依赖全部注入（channel 式，不反向 import main）：launch_session /
touch_session / all_sessions / get_session_store / get_task_store。
"""

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from chat.chat import chat_turn

logger = logging.getLogger(__name__)

COUNTERPART_MODES = ("scripted", "llm", "channel")
FINISH_REASONS = ("end_module", "max_turns", "timeout",
                  "counterpart_exhausted", "error")

_DEFAULT_ROLE_PROMPT = "你是这项任务的对方当事人，请结合身份与对话自然应对，不要跳出角色。"


# ---------------------------------------------------------------------------
# 对端扮演（Q15：scripted / llm 实现，channel 预留）
# ---------------------------------------------------------------------------

class ScriptedCounterpart:
    """脚本式对端：逐轮弹出预置回复，耗尽返回 None（→ counterpart_exhausted）。"""

    def __init__(self, script: List[str]):
        self._script = [str(item) for item in script]
        self._cursor = 0

    def next_reply(self, agent_reply: str) -> Optional[str]:
        if self._cursor >= len(self._script):
            return None
        reply = self._script[self._cursor]
        self._cursor += 1
        return reply


class LlmCounterpart:
    """LLM 角色扮演对端：role_prompt 为 system + 双方转录，每轮现场生成回复。"""

    def __init__(self, role_prompt: str, llm_config: Dict[str, Any]):
        from llm.resolve import build_provider

        self._llm_config = llm_config
        self._provider = build_provider(llm_config)
        self._system = role_prompt
        # 对端视角转录：pattern 的话是 assistant，自己的话是 user
        self._transcript: List[Dict[str, str]] = []

    def next_reply(self, agent_reply: str) -> Optional[str]:
        self._transcript.append({"role": "assistant", "content": agent_reply})
        messages = ([{"role": "system", "content": self._system}]
                    + self._transcript)
        resp = self._provider.chat_completion(
            messages=messages,
            model=self._llm_config.get("model"),
            temperature=self._llm_config.get("temperature", 0.7),
            max_tokens=self._llm_config.get("max_tokens", 2048),
        )
        text = (resp or {}).get("content") or ""
        self._transcript.append({"role": "user", "content": text})
        return text


def _build_counterpart(
    cfg: Dict[str, Any], pattern: Any,
) -> Tuple[Optional[Any], Optional[str]]:
    """解析对端配置；返回 (counterpart, error_message)。"""
    mode = cfg.get("mode", "llm")
    if mode not in COUNTERPART_MODES:
        return None, f"counterpart.mode 需为 {list(COUNTERPART_MODES)} 之一，实际为 {mode!r}"
    if mode == "channel":
        return None, "counterpart.mode=channel 本轮未实现（仅支持 scripted / llm）"
    if mode == "scripted":
        script = cfg.get("script") or []
        if not isinstance(script, list) or not script:
            return None, "scripted 对端需要非空 script 数组"
        return ScriptedCounterpart(script), None

    # llm：role_prompt 回退链——请求 > 模版 counterpart_hint > 通用角色
    hint = getattr(pattern, "counterpart_hint", None) or {}
    role_prompt = cfg.get("role_prompt") or hint.get("role_prompt") or _DEFAULT_ROLE_PROMPT
    try:
        if cfg.get("llm_override"):
            from config.config import get_llm_config
            llm_config = get_llm_config(override=dict(cfg["llm_override"]))
        else:
            from config.config import get_llm_config
            llm_config = get_llm_config()
        return LlmCounterpart(role_prompt, llm_config), None
    except Exception as e:
        return None, f"构建 llm 对端失败（LLM 配置解析）: {e}"


# ---------------------------------------------------------------------------
# 任务记录与引擎
# ---------------------------------------------------------------------------

@dataclass
class _TaskRecord:
    task_id: str
    session_id: str
    pattern_code: str
    status: str = "pending"           # pending / running / done / failed
    finish_reason: Optional[str] = None
    turn_count: int = 0
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class EngineDeps:
    """main 注入的引擎依赖（不反向 import main）。"""
    launch_session: Callable[..., Tuple[Optional[Any], str, str]]
    touch_session: Callable[[str], None]
    all_sessions: Dict[str, Any]
    get_session_store: Callable[[], Optional[Any]]
    get_task_store: Callable[[], Optional[Any]]


class TaskEngine:
    """自主任务引擎：start() 起线程即返回；get() 供轮询。"""

    def __init__(self, deps: EngineDeps):
        self._deps = deps
        self._tasks: Dict[str, _TaskRecord] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------

    def start(
        self,
        pattern_code: str,
        task_info: Optional[Dict[str, Any]] = None,
        counterpart: Optional[Dict[str, Any]] = None,
        max_turns: int = 20,
        timeout_s: float = 600.0,
        kickoff: Optional[str] = None,
        session_id: Optional[str] = None,
        llm_override: Optional[Dict[str, Any]] = None,
        request_id: str = "",
    ) -> Tuple[Optional[str], str, Optional[str]]:
        """触发任务；返回 (task_id, session_id, error)——error 非 None 即失败。

        fire-and-forget：起线程后立即返回，调用方轮询 get(task_id)。
        """
        from dialogue.register import registry as pattern_registry

        task_info = task_info or {}
        task_id = uuid.uuid4().hex
        session_id = session_id or f"task-{task_id[:16]}"

        pattern = pattern_registry.get(pattern_code)
        if pattern is None:
            return (None, session_id,
                    f"pattern_code '{pattern_code}' 未注册，已注册: {pattern_registry.list_codes()}")

        # 对端合法性先于 launch 校验（避免起会话后又放弃）
        counterpart_obj, err = _build_counterpart(counterpart or {}, pattern)
        if err is not None:
            return None, session_id, err

        session, code, message = self._deps.launch_session(
            pattern_code=pattern_code,
            session_id=session_id,
            task_info=task_info,
            request_id=request_id or task_id,
        )
        if session is None:
            return None, session_id, f"{code}: {message}"

        # 调试/离线测试：pattern 侧 LLM 走 override（与 chat 侧 llm_override 同路径）
        if llm_override:
            session.cxt.metadata["llm_override"] = llm_override

        if kickoff is None:
            kickoff = json.dumps(task_info, ensure_ascii=False)

        task_store = self._deps.get_task_store()
        if task_store is not None:
            try:
                task_store.create_task(task_id, session_id, pattern_code)
            except Exception:
                logger.exception("任务落库失败（继续内存执行）: %s", task_id)

        record = _TaskRecord(task_id=task_id, session_id=session_id,
                             pattern_code=pattern_code)
        with self._lock:
            self._tasks[task_id] = record

        thread = threading.Thread(
            target=self._run_task,
            args=(record, session, counterpart_obj, max_turns, timeout_s, kickoff),
            daemon=True, name=f"task-{task_id[:8]}",
        )
        thread.start()
        return task_id, session_id, None

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """任务视图：运行中带中间过程（Q5），终态带结果快照（Q14）。

        内存 miss 时回退 TaskStore（服务重启后历史任务仍可查）。
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is not None:
                snapshot = (record.status, record.finish_reason, record.turn_count,
                            record.result, record.error, record.session_id,
                            record.pattern_code)
        if record is None:
            task_store = self._deps.get_task_store()
            if task_store is None:
                return None
            row = None
            try:
                row = task_store.get_task(task_id)
            except Exception:
                logger.exception("查询任务落库记录失败: %s", task_id)
            if row is None:
                return None
            return {
                "task_id": row["task_id"], "session_id": row["session_id"],
                "pattern_code": row["pattern_code"], "status": row["status"],
                "finish_reason": row.get("finish_reason"),
                "turn_count": 0, "current_module": None, "current_node": None,
                "recent_messages": [], "result": row.get("result") or None,
            }

        (status, finish_reason, turn_count, result, error,
         session_id, pattern_code) = snapshot
        view: Dict[str, Any] = {
            "task_id": task_id, "session_id": session_id,
            "pattern_code": pattern_code, "status": status,
            "finish_reason": finish_reason, "turn_count": turn_count,
            "error": error,
        }
        session = self._deps.all_sessions.get(session_id)
        if status in ("done", "failed"):
            view["result"] = result
        else:
            # 运行中：中间过程（当前模块/节点 + 最近消息尾 5 条）
            cxt = getattr(session, "cxt", None) if session is not None else None
            view["current_module"] = getattr(cxt, "current_module_code", None)
            view["current_node"] = getattr(cxt, "current_node_code", None)
            tail = list(getattr(cxt, "history", None) or [])[-5:]
            view["recent_messages"] = [
                {"role": m.role, "content": m.content} for m in tail]
        return view

    # ------------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------------

    def recover_interrupted(self) -> List[str]:
        """服务重启后调用：上一进程遗留的 pending/running 任务改判 failed。"""
        task_store = self._deps.get_task_store()
        if task_store is None:
            return []
        try:
            return task_store.fail_interrupted()
        except Exception:
            logger.exception("恢复遗留任务失败")
            return []

    # ------------------------------------------------------------------
    # 后台执行
    # ------------------------------------------------------------------

    def _run_task(self, record: _TaskRecord, session: Any, counterpart: Any,
                  max_turns: int, timeout_s: float, kickoff: str) -> None:
        task_store = self._deps.get_task_store()
        session_store = self._deps.get_session_store()
        self._update(record, status="running")
        if task_store is not None:
            try:
                task_store.mark_running(record.task_id)
            except Exception:
                logger.exception("任务标记 running 失败: %s", record.task_id)

        deadline = time.monotonic() + timeout_s
        query = kickoff
        status, finish_reason, error = "done", None, None

        # 整个循环体兜底：对端 LLM 失败 / 终态检测等任何未预期异常都不允许
        # 让守护线程带着 running 状态死掉——必须落到一个终态，轮询方才能感知
        try:
            while True:
                if self._deps.all_sessions.get(session.session_id) is None:
                    status, finish_reason, error = "failed", "error", \
                        "会话已从内存逐出（TTL/上限），任务中止"
                    break
                try:
                    result = chat_turn(
                        query=query,
                        session_id=session.session_id,
                        all_sessions=self._deps.all_sessions,
                        store=session_store,
                    )
                except Exception as e:
                    logger.exception("任务轮次异常: task=%s", record.task_id)
                    status, finish_reason, error = "failed", "error", str(e)
                    break

                record.turn_count += 1
                try:
                    self._deps.touch_session(session.session_id)  # 防 TTL 误逐出
                except Exception:
                    pass
                if session_store is not None:
                    try:
                        session_store.save_snapshot(session)
                    except Exception:
                        logger.exception("轮末快照失败: session=%s", session.session_id)

                end_reason = self._detect_end(session, result)
                if end_reason:
                    finish_reason = end_reason
                    break
                if record.turn_count >= max_turns:
                    finish_reason = "max_turns"
                    break
                if time.monotonic() >= deadline:
                    finish_reason = "timeout"
                    break
                next_query = counterpart.next_reply(result.text)
                if next_query is None:
                    finish_reason = "counterpart_exhausted"
                    break
                query = next_query
        except Exception as e:
            logger.exception("任务执行异常（兜底终态）: task=%s", record.task_id)
            status, finish_reason, error = "failed", "error", str(e)

        try:
            result_snapshot = self._build_result(
                session, finish_reason, record.turn_count, error)
        except Exception:
            logger.exception("结果快照构建失败: task=%s", record.task_id)
            result_snapshot = {
                "finish_reason": finish_reason, "turn_count": record.turn_count,
                "error": error, "messages": [],
            }
        self._update(record, status=status, finish_reason=finish_reason,
                     result=result_snapshot, error=error)
        if task_store is not None:
            try:
                task_store.finish_task(record.task_id, status, finish_reason,
                                       result_snapshot)
            except Exception:
                logger.exception("任务终态落库失败: %s", record.task_id)
        logger.info("任务结束: task=%s status=%s reason=%s turns=%d",
                    record.task_id, status, finish_reason, record.turn_count)

    @staticmethod
    def _detect_end(session: Any, result: Any) -> Optional[str]:
        """终态检测（零内核改动覆盖三形态）：

        - FSM 管线落到 is_end 节点时产出 conversation_end action
          （chat/chat.py 的既有信号）
        - 模块级 / 节点级 is_end 由本引擎自查（Agent 型转移后的收尾模块
          运行时无人消费该标志，runner 侧补上）
        """
        for action in result.actions or []:
            if isinstance(action, dict) and action.get("conversation_end"):
                return "end_module"
        pattern = getattr(session, "pattern", None)
        if pattern is None:
            return None
        cxt = session.cxt
        module = pattern.module_map.get(getattr(cxt, "current_module_code", None))
        if module is not None and getattr(module, "is_end", False):
            return "end_module"
        node = pattern.node_map.get(getattr(cxt, "current_node_code", None))
        if node is not None and getattr(node, "is_end", False):
            return "end_module"
        return None

    @staticmethod
    def _build_result(session: Any, finish_reason: Optional[str],
                      turn_count: int, error: Optional[str]) -> Dict[str, Any]:
        """结果快照（Q14）：结构化状态 + 原始消息；语义后置判断留给子 skill。"""
        cxt = session.cxt
        history = list(getattr(cxt, "history", None) or [])
        return {
            "finish_reason": finish_reason,
            "end_module_code": getattr(cxt, "current_module_code", None),
            "end_node_code": getattr(cxt, "current_node_code", None),
            "filled_slots": dict(getattr(cxt, "filled_slots", None) or {}),
            "turn_count": turn_count,
            "error": error,
            "messages": [
                {"role": m.role, "content": m.content, "stage": m.stage}
                for m in history
            ],
        }

    def _update(self, record: _TaskRecord, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(record, key, value)
