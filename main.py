import logging
import threading
import time
from typing import Any, Dict, List, Optional

import fastapi
from pydantic import BaseModel

from config.config import get_session_db_path
from src.chat.chat import chat
from src.chat.session import Session
from src.chat.store import SessionStore
from src.dialogue.register import registry as pattern_registry
from src.dialogue.register import discover_builtin_patterns
from src.tools.register import registry as tool_registry
from src.tools.register import discover_builtin_tools

logger = logging.getLogger(__name__)




# ----init----
app = fastapi.FastAPI()
discover_builtin_tools()
discover_builtin_patterns()

# ----会话治理（常量可调）----
# 会话空闲过期时间（秒）：自最近一次活跃（launch/chat）起计算，默认 2 小时
SESSION_TTL_SECONDS = 2 * 60 * 60
# 会话数量上限：launch 新增会话时若达到上限，按最近活跃时间逐出最旧会话
MAX_SESSIONS = 10_000

all_sessions: Dict[str, Session] = {}
# session_id -> 最近活跃时间戳（time.monotonic 秒），与 all_sessions 同步增删
_session_last_active: Dict[str, float] = {}
# 保护 all_sessions / _session_last_active 的并发读写（launch 登记、chat 校验、过期与超限逐出）
_sessions_lock = threading.Lock()

# 会话持久化 store（SQLite 审计 + 重启恢复）；startup 时初始化，测试可替换。
# None = 未启用（降级：对话正常，无审计/无恢复）
store: Optional[SessionStore] = None


def _touch_session(session_id: str) -> None:
    """刷新会话最近活跃时间（滑动续期，须持有 _sessions_lock）。"""
    _session_last_active[session_id] = time.monotonic()


def _purge_expired_sessions() -> int:
    """清理超过 SESSION_TTL_SECONDS 未活跃的过期会话（须持有 _sessions_lock）。

    Returns:
        int: 本次清理的会话数
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
    """会话数达到 MAX_SESSIONS 时逐出最近活跃最早的会话（须持有 _sessions_lock）。

    在 launch 登记新会话前调用，保证插入后总数不超过上限。

    Returns:
        int: 本次逐出的会话数
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
    """初始化会话持久化 store；失败降级为 None（对话可用，审计/恢复关闭）。"""
    global store
    try:
        db_path = get_session_db_path()
        store = SessionStore(db_path)
        logger.info("会话存储已启用: %s", db_path)
    except Exception:
        logger.exception("初始化会话存储失败，审计与重启恢复降级")
        store = None


def _restore_sessions() -> int:
    """从 store 恢复未过期会话回内存（重启恢复）。

    pattern 按 pattern_code 从注册中心重新解析并注入 node_map/module_map；
    未注册的 pattern 跳过并 warning。DB 墙钟换算 monotonic 基准。

    Returns:
        int: 实际恢复的会话数
    """
    if store is None:
        return 0
    restored = 0
    now_wall = time.time()
    for session, last_active_wall in store.load_active_sessions(SESSION_TTL_SECONDS):
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
        with _sessions_lock:
            all_sessions[session.session_id] = session
            _session_last_active[session.session_id] = time.monotonic() - (
                now_wall - last_active_wall
            )
        restored += 1
    if restored:
        logger.info("重启恢复会话 %d 个", restored)
    return restored


@app.on_event("startup")
def _startup_persistence() -> None:
    """服务启动：初始化会话存储 + 恢复未过期会话。"""
    _init_store()
    _restore_sessions()


# # check aleady registried patterns and tools
# print(pattern_registry._patterns)
# print(tool_registry._tools)

# 整体说明
# 特定任务对话管理
# 对话模板（src/dialogue）：由对话模块组成，每个模块复制不同的对话任务，模块也可以多个节点组成，整体为有限状态机跳转，模块有节点code，一个模块包含0到多个节点。模版可自助注册
# 大模型提供商（src/llm）：提供大模型api请求
# 工具（src/tools）：模版对话时可请求的工具，可自助注册，在模块定义时标明使用哪些工具或在工具注册时标明哪个模版或哪个模版的哪个模块可使用
# 对话跳转（src/chat）：依据已有session，获取当前对话所处模块，若模块非节点构成，组装system_prompt与对话记录，进行多轮对话（若输出中包含[jump xx]标识，则进行跳转，主要通过system_prompt的内容进行控制）;否则，按照节点的有限状态机进行两阶段跳转，先进行意图识别，再进行回复生成。
# 调用api结束后，更新跳转状态，更新会话记录



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
    data: Dict[str, str] = {}


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


class SessionMessagesResponse(BaseModel):
    code: str
    message: str
    status: bool
    data: Dict[str, List[MessageItem]] = {}



# func1
# 外呼任务发起：根据pattern_code注册一个对话任务，并新增session
@app.post("/api/v1/launch")
def launch_dialogue(dialogue_request: DialogueRequest) -> DialogueResponse:
    pattern_code = dialogue_request.pattern_code

    # 1. 校验 pattern_code 是否已注册
    pattern = pattern_registry.get(pattern_code)
    if pattern is None:
        return DialogueResponse(
            code="404",
            status=False,
            message=f"pattern_code '{pattern_code}' 未注册，已注册: {pattern_registry.list_codes()}",
        )

    # 2. 会话治理：清理过期会话；session_id 重复时报错而非静默覆盖；达到上限逐出最旧
    with _sessions_lock:
        _purge_expired_sessions()

        if dialogue_request.session_id in all_sessions:
            return DialogueResponse(
                code="409",
                status=False,
                message=(
                    f"session_id '{dialogue_request.session_id}' 已存在，"
                    f"请更换 session_id 重新发起"
                ),
            )

        _evict_oldest_if_over_limit()

        # 3. 实例化 session，填充 pattern 与任务信息
        session = Session(session_id=dialogue_request.session_id, pattern_code=pattern_code)
        session.pattern = pattern
        session.task_info = dialogue_request.task_info

        # 4. 注入管线上下文：node_map / module_map 供 pipeline 各阶段使用
        session.cxt.module_map = pattern.module_map
        session.cxt.node_map = pattern.node_map
        session.cxt.metadata["task_info"] = dialogue_request.task_info
        session.cxt.metadata["request_id"] = dialogue_request.request_id

        # 5. 登记 session 并记录活跃时间
        all_sessions[dialogue_request.session_id] = session
        _touch_session(dialogue_request.session_id)

    # 6. 审计落盘（内存登记成功后）；失败仅记日志，不阻断 launch
    if store is not None:
        try:
            store.create_session(session)
        except Exception:
            logger.exception("会话落盘失败: session=%s", dialogue_request.session_id)

    return DialogueResponse(
        code="0",
        status=True,
        message=f"对话任务发起成功: session_id={dialogue_request.session_id}",
    )


# func2
# 对话请求：根据 session_id 获取 session，处理用户 query
@app.post("/api/v1/chat")
def chat_dialogue(chat_request: ChatRequest) -> ChatResponse:
    # 1. 清理过期会话后校验 session 是否存在，命中则刷新活跃时间（滑动续期）
    with _sessions_lock:
        _purge_expired_sessions()
        session = all_sessions.get(chat_request.session_id)
        if session is not None:
            _touch_session(chat_request.session_id)

    if session is None:
        return ChatResponse(
            code="404",
            status=False,
            message=f"session_id '{chat_request.session_id}' 不存在或已过期，请先发起对话任务",
        )

    # 2. 调用 chat 函数处理对话。
    #    锁外执行：LLM 调用耗时长，不能阻塞其他请求；chat 内部持 session 本地引用，
    #    即便本轮处理中被并发逐出也不影响本次对话
    # 3. 轮末审计落盘：追加本轮新增消息 + 状态快照（含上方异常路径；失败仅记日志）
    start_idx = len(session.cxt.history)
    error: Optional[Exception] = None
    try:
        response_text = chat(
            query=chat_request.query,
            session_id=chat_request.session_id,
            all_sessions=all_sessions,
        )
    except Exception as e:
        logger.exception("对话处理异常")
        error = e

    if store is not None:
        try:
            store.save_turn(session, start_idx)
        except Exception:
            logger.exception("会话轮末落盘失败: session=%s", chat_request.session_id)

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


# func3（只读审计）
# 会话列表：按 last_active_at 倒序，可按 pattern_code 过滤、分页
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


# func4（只读审计）
# 某会话全程消息（含 NLU/NLG 等中间 stage 消息），按 id 升序
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
