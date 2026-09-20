import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import fastapi
from pydantic import BaseModel

from config.config import DEFAULT_TEMPLATES_DIR, get_session_db_path, get_templates_dir
from channel.base import EngineOps
from channel.register import discover_builtin_channels
from channel.webhooks import build_channel_routers
from chat.chat import chat
from chat.session import Session
from chat.store import SessionStore
from dialogue.register import registry as pattern_registry
from dialogue.register import discover_builtin_patterns
from templates.api import build_templates_router
from templates.store import TemplateStore, replay_templates
from tools.register import registry as tool_registry
from tools.register import discover_builtin_tools

logger = logging.getLogger(__name__)




# ----init----
app = fastapi.FastAPI()
discover_builtin_tools()
discover_builtin_patterns()

# ----Session governance (tunable constants)----
# Session idle expiry (seconds), counted from last activity (launch/chat)
SESSION_TTL_SECONDS = 2 * 60 * 60
# Session cap: when a launch hits it, evict the oldest sessions by
# last-active time
MAX_SESSIONS = 10_000

all_sessions: Dict[str, Session] = {}
# session_id -> last-active timestamp (time.monotonic seconds); kept in sync
# with all_sessions on insert/remove
_session_last_active: Dict[str, float] = {}
# Serializes concurrent access to all_sessions / _session_last_active
# (launch registration, chat lookup, TTL and over-limit eviction)
_sessions_lock = threading.Lock()

# Session persistence store (SQLite audit + restart restore); initialized at
# startup, replaceable in tests.
# None = not enabled (degraded: dialogue works, no audit / no restore)
store: Optional[SessionStore] = None

# Dialogue-template store (data/templates/*.json + startup replay into the
# pattern registry); initialized at startup, replaceable in tests.
template_store: Optional[TemplateStore] = None


def _touch_session(session_id: str) -> None:
    """Refresh the session's last-active time (sliding renewal; requires _sessions_lock held)."""
    _session_last_active[session_id] = time.monotonic()


def _touch_session_threadsafe(session_id: str) -> None:
    """Lock-wrapped touch for background threads (task runner keeps its session
    hot against TTL eviction)."""
    with _sessions_lock:
        if session_id in _session_last_active:
            _touch_session(session_id)


def _purge_expired_sessions() -> int:
    """Purge sessions idle beyond SESSION_TTL_SECONDS (requires _sessions_lock
    held).

    Returns:
        int: number of sessions purged in this call
    """
    now = time.monotonic()
    expired = [
        sid
        for sid, ts in _session_last_active.items()
        if now - ts > SESSION_TTL_SECONDS
    ]
    for sid in expired:
        all_sessions.pop(sid, None)
        _session_last_active.pop(sid, None)
    if expired:
        logger.info("清理过期会话 %d 个: %s", len(expired), expired)
    return len(expired)


def _evict_oldest_if_over_limit() -> int:
    """Evict the least-recently-active sessions once the count reaches
    MAX_SESSIONS (requires _sessions_lock held).

    Called before registering a new session at launch so the total stays
    within the cap after insertion.

    Returns:
        int: number of sessions evicted in this call
    """
    evicted = []
    while len(all_sessions) >= MAX_SESSIONS and _session_last_active:
        oldest_sid = min(_session_last_active, key=_session_last_active.get)
        all_sessions.pop(oldest_sid, None)
        _session_last_active.pop(oldest_sid, None)
        evicted.append(oldest_sid)
    if evicted:
        logger.info(
            "会话数达到上限 %d，逐出最旧会话 %d 个: %s",
            MAX_SESSIONS,
            len(evicted),
            evicted,
        )
    return len(evicted)


def _init_store() -> None:
    """Initialize the session persistence store; on failure degrade to None (dialogue works, audit/restore disabled)."""
    global store
    try:
        db_path = get_session_db_path()
        store = SessionStore(db_path)
        logger.info("会话存储已启用: %s", db_path)
    except Exception:
        logger.exception("初始化会话存储失败，审计与重启恢复降级")
        store = None


def _init_knowledge_store() -> None:
    """Warm up the knowledge-base connection (moves the tools' lazy-init fallback to startup); failure does not block the service."""
    try:
        from database.knowledge_store import get_knowledge_store
        kb = get_knowledge_store()
        logger.info("知识库已启用: %s", kb._conn and "ok")
    except Exception:
        logger.exception("初始化知识库失败，知识工具将在首次调用时重试")


def _init_template_store() -> None:
    """Initialize the template store and replay persisted templates into the
    pattern registry (must run before session restore — restored sessions
    re-resolve their pattern from the registry). Failure degrades to None."""
    global template_store
    try:
        dir_path = get_templates_dir()
    except Exception:
        logger.exception("读取 templates_dir 配置失败，回退默认目录")
        dir_path = DEFAULT_TEMPLATES_DIR
    try:
        template_store = TemplateStore(dir_path)
        replay_templates(template_store, pattern_registry)
    except Exception:
        logger.exception("初始化模版存储失败，模版注册/查询降级")
        template_store = None


def _restore_sessions() -> int:
    """Restore non-expired sessions from the store back into memory (restart
    restore).

    Patterns are re-resolved from the registry by pattern_code and injected
    into node_map/module_map; unregistered patterns are skipped with a
    warning. DB wall-clock times are converted onto the monotonic base.

    Returns:
        int: number of sessions actually restored
    """
    if store is None:
        return 0
    restored = 0
    now_wall = time.time()
    try:
        active_sessions = store.load_active_sessions(SESSION_TTL_SECONDS)
    except Exception:
        logger.exception("加载未过期会话失败，跳过恢复")
        return 0
    for session, last_active_wall in active_sessions:
        try:
            pattern = pattern_registry.get(session.pattern_code)
            if pattern is None:
                logger.warning(
                    "恢复跳过会话 %s: pattern '%s' 未注册",
                    session.session_id,
                    session.pattern_code,
                )
                continue
            session.pattern = pattern
            session.cxt.module_map = pattern.module_map
            session.cxt.node_map = pattern.node_map
            store.attach(session)  # re-attach write-through for the restored session (before it enters memory)
            with _sessions_lock:
                all_sessions[session.session_id] = session
                _session_last_active[session.session_id] = time.monotonic() - (
                    now_wall - last_active_wall
                )
            restored += 1
        except Exception:
            logger.exception("恢复会话失败，跳过: session=%s", session.session_id)
            continue
    if restored:
        logger.info("重启恢复会话 %d 个", restored)
    return restored


def _cross_check_pattern_llm(config_path: str = "") -> None:
    """Cross-check that pattern_llm codes exist (spec §5): unknown ones only warn, never block."""
    from config.config import load_config
    try:
        pattern_llm = load_config(config_path).get("pattern_llm", {})
    except Exception:
        logger.exception("加载配置失败，跳过 pattern_llm 交叉校验")
        return
    for pcode, pcfg in pattern_llm.items():
        pattern = pattern_registry.get(pcode)
        if pattern is None:
            logger.warning("pattern_llm 配置了未注册的 pattern '%s'", pcode)
            continue
        for mcode in (pcfg.get("modules") or {}):
            if mcode not in pattern.module_map:
                logger.warning(
                    "pattern '%s' 的 pattern_llm.modules 配置了未注册 module '%s'",
                    pcode, mcode)
        for ncode in (pcfg.get("nodes") or {}):
            if ncode not in pattern.node_map:
                logger.warning(
                    "pattern '%s' 的 pattern_llm.nodes 配置了未注册 node '%s'",
                    pcode, ncode)


@app.on_event("startup")
def _startup_persistence() -> None:
    """Service startup: session store + template replay + session restore + knowledge store + pattern_llm cross-check."""
    _init_store()
    _init_template_store()  # 模版重放须先于会话恢复（恢复时按 code 从 registry 重解析 pattern）
    try:
        _restore_sessions()
    except Exception:
        logger.exception("重启恢复失败，跳过恢复")
    _init_knowledge_store()
    _cross_check_pattern_llm()


@app.on_event("shutdown")
def _shutdown_stores() -> None:
    """Service shutdown: release the knowledge-base / session-store connections."""
    global store
    try:
        from database.knowledge_store import close_knowledge_store
        close_knowledge_store()
    except Exception:
        logger.exception("关闭知识库失败")
    if store is not None:
        try:
            store.close()
        except Exception:
            logger.exception("关闭会话存储失败")
        store = None


# # check aleady registried patterns and tools
# print(pattern_registry._patterns)
# print(tool_registry._tools)

# Overall design
# Task-specific dialogue management
# Dialogue templates (dialogue): composed of dialogue modules, each module
# covering a different dialogue task; a module may in turn consist of several
# nodes, the whole behaving as a finite-state machine of node jumps. Modules
# have node codes; one module contains 0..n nodes. Templates are
# self-registering.
# LLM providers (llm): issue LLM API requests.
# Tools (tools): callable during template dialogue, self-registering; a module
# declares which tools it uses at definition time, or a tool registration
# declares which template — or which module of which template — may use it.
# Dialogue jumps (chat): based on the existing session, locate the module the
# dialogue is currently in. AGENT-type module: assemble the system_prompt and
# conversation history and run a multi-round tool dialogue (when the LLM calls
# a transfer_to_XX tool, a ModuleJumpEvent is written to cxt.actions and
# consumed within the same turn by the chat layer's hop loop, which reroutes
# so the taking-over module continues speaking directly). FSM-type module:
# follow the node finite-state machine's two-phase jump — intent recognition
# first, then reply generation. ROUTE-type module: when intent-menu matching
# hits a node carrying jump_module (or the NLU directly outputs jump_module),
# the stage loop detects it and writes a jump event; the chat layer consumes
# it and jumps to the target module within the same turn.
# After the API call completes, update the jump state and the session record.



class DialogueRequest(BaseModel):
    request_id: str
    session_id: str
    pattern_code: str
    task_info: Dict[str, str]


class DialogueResponse(BaseModel):
    code: str
    message: str
    status: bool


class ChatRequest(BaseModel):
    request_id: str
    session_id: str
    query: str


class ChatResponse(BaseModel):
    code: str
    message: str
    status: bool
    data: Dict[str, Any] = {}


class SessionSummary(BaseModel):
    session_id: str
    pattern_code: str
    current_module_code: Optional[str] = None
    current_node_code: Optional[str] = None
    message_count: int
    created_at: float
    last_active_at: float


class SessionListResponse(BaseModel):
    code: str
    message: str
    status: bool
    data: Dict[str, List[SessionSummary]] = {}


class MessageItem(BaseModel):
    id: int
    role: str
    content: str
    stage: str
    metadata: Dict[str, Any] = {}
    created_at: float
    action: Dict[str, str] = {}


class SessionMessagesResponse(BaseModel):
    code: str
    message: str
    status: bool
    data: Dict[str, List[MessageItem]] = {}



# ----Engine-op core (shared by endpoints and channels; main injects these functions into channels)----

def _launch_session_core(
    pattern_code: str,
    session_id: str,
    task_info: Dict[str, str],
    request_id: str,
    exist_ok: bool = False,
) -> Tuple[Optional[Session], str, str]:
    """Launch core: pattern validation + session governance
    (purge/duplicate-check/eviction) + audit persistence.

    Args:
        exist_ok: when True, an already-existing session_id counts as success
            and returns the existing session (channel get-or-create
            semantics) without overwriting it.

    Returns:
        (session, code, message): code == "0" means success; on failure
        session is None and code/message carry the same semantics as the
        /api/v1/launch response.
    """
    pattern = pattern_registry.get(pattern_code)
    if pattern is None:
        return None, "404", (
            f"pattern_code '{pattern_code}' 未注册，已注册: {pattern_registry.list_codes()}"
        )

    with _sessions_lock:
        _purge_expired_sessions()

        existing = all_sessions.get(session_id)
        if existing is not None:
            if exist_ok:
                _touch_session(session_id)
                return existing, "0", f"session_id '{session_id}' 已存在，复用既有会话"
            return None, "409", (
                f"session_id '{session_id}' 已存在，请更换 session_id 重新发起"
            )

        _evict_oldest_if_over_limit()

        session = Session(session_id=session_id, pattern_code=pattern_code)
        session.pattern = pattern
        session.task_info = task_info
        session.cxt.module_map = pattern.module_map
        session.cxt.node_map = pattern.node_map
        session.cxt.metadata["task_info"] = task_info
        session.cxt.metadata["request_id"] = request_id

        all_sessions[session_id] = session
        _touch_session(session_id)

    # Audit persistence (after in-memory registration succeeded); failure is
    # only logged and never blocks launch.
    # attach runs unconditionally — if create_session failed, sink write
    # failures are swallowed anyway, so no messages are lost once the DB
    # recovers mid-way.
    if store is not None:
        try:
            store.create_session(session)
        except Exception:
            logger.exception("会话落盘失败: session=%s", session_id)
        store.attach(session)

    return session, "0", f"对话任务发起成功: session_id={session_id}"


def _get_session(session_id: str) -> Optional[Session]:
    """Governance-aware session lookup: purge expired, and on a hit refresh
    the last-active time (sliding renewal).
    """
    with _sessions_lock:
        _purge_expired_sessions()
        session = all_sessions.get(session_id)
        if session is not None:
            _touch_session(session_id)
        return session


def _run_chat_turn_core(
    session: Session, query: str
) -> Tuple[Optional[str], Optional[Exception]]:
    """Single chat-turn core: run chat + end-of-turn audit persistence.

    Runs outside the lock (LLM calls are slow and must not block other
    requests); chat re-fetches the session by session_id internally, and the
    local session reference here exists only for persisting the snapshot —
    even a concurrent eviction mid-turn does not affect this dialogue. The
    exception path persists too (same as the success path). Messages were
    already persisted per-message by the sink; end of turn only writes back
    the state snapshot.

    Returns:
        (reply, error): error is None on success; reply is None only on the
        exception path.
    """
    error: Optional[Exception] = None
    try:
        response_text = chat(
            query=query,
            session_id=session.session_id,
            all_sessions=all_sessions,
            store=store,
        )
    except Exception as e:
        logger.exception("对话处理异常")
        error = e

    if store is not None:
        try:
            store.save_snapshot(session)
        except Exception:
            logger.exception("会话轮末快照失败: session=%s", session.session_id)

    if error is not None:
        return None, error
    return response_text, None


# func0 (liveness probe: for clients/skills to detect the service before use)
@app.get("/api/v1/health")
def health() -> Dict[str, Any]:
    return {
        "code": "0",
        "message": "ok",
        "status": True,
        "data": {"status": "ok", "patterns": pattern_registry.list_codes()},
    }


# func1
@app.post("/api/v1/launch")
def launch_dialogue(dialogue_request: DialogueRequest) -> DialogueResponse:
    _session, code, message = _launch_session_core(
        pattern_code=dialogue_request.pattern_code,
        session_id=dialogue_request.session_id,
        task_info=dialogue_request.task_info,
        request_id=dialogue_request.request_id,
    )
    return DialogueResponse(code=code, status=(code == "0"), message=message)


# func2
@app.post("/api/v1/chat")
def chat_dialogue(chat_request: ChatRequest) -> ChatResponse:
    session = _get_session(chat_request.session_id)
    if session is None:
        return ChatResponse(
            code="404",
            status=False,
            message=f"session_id '{chat_request.session_id}' 不存在或已过期，请先发起对话任务",
        )

    response_text, error = _run_chat_turn_core(session, chat_request.query)

    if error is not None:
        return ChatResponse(
            code="500",
            status=False,
            message=f"对话处理异常: {error}",
        )

    return ChatResponse(
        code="0",
        status=True,
        message="success",
        data={
            "request_id": chat_request.request_id,
            "session_id": chat_request.session_id,
            "response": response_text,
        },
    )


# ----Channel wiring (external message sources -> engine ops)----
# AST-discovers the declarative channels in channel/*.py (token / default
# pattern come from each channel's declared env vars, re-read on every request
# and thus hot-reloadable); a generic handler generates the routers
discover_builtin_channels()
for _router in build_channel_routers(EngineOps(
    get_session=_get_session,
    launch_session=_launch_session_core,
    run_chat_turn=_run_chat_turn_core,
)):
    app.include_router(_router)

# ----Template registry wiring (dialogue-template register/validate/query)----
app.include_router(build_templates_router(lambda: template_store))


# func3 (read-only audit)
@app.get("/api/v1/sessions")
def list_sessions(
    pattern_code: str = "", limit: int = 50, offset: int = 0
) -> SessionListResponse:
    if store is None:
        return SessionListResponse(code="500", status=False, message="会话存储未启用")

    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    try:
        sessions = store.list_sessions(
            pattern_code=pattern_code or None, limit=limit, offset=offset
        )
    except Exception as e:
        logger.exception("查询会话列表失败")
        return SessionListResponse(code="500", status=False, message=f"查询会话列表失败: {e}")

    return SessionListResponse(
        code="0", status=True, message="success", data={"sessions": sessions}
    )


# func4 (read-only audit)
@app.get("/api/v1/sessions/{session_id}/messages")
def get_session_messages(session_id: str) -> SessionMessagesResponse:
    if store is None:
        return SessionMessagesResponse(code="500", status=False, message="会话存储未启用")

    try:
        messages = store.get_messages(session_id)
    except Exception as e:
        logger.exception("查询会话消息失败")
        return SessionMessagesResponse(code="500", status=False, message=f"查询会话消息失败: {e}")

    if messages is None:
        return SessionMessagesResponse(
            code="404", status=False, message=f"session_id '{session_id}' 不存在"
        )

    return SessionMessagesResponse(
        code="0", status=True, message="success", data={"messages": messages}
    )


# ----Entrypoint (binds 127.0.0.1 only: local mock service, no auth by design)----
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
