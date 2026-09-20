"""任务 API 路由：POST /api/v1/tasks 触发 + GET /api/v1/tasks/{task_id} 轮询。

依赖注入 TaskEngine（channel 式，不反向 import main）。会话历史查询复用
现有 GET /api/v1/sessions/{session_id}/messages，不在本路由重复。
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from tasks.engine import COUNTERPART_MODES, TaskEngine

logger = logging.getLogger(__name__)

MAX_TURNS_CEILING = 200
TIMEOUT_S_CEILING = 3600.0


class CounterpartSpec(BaseModel):
    mode: str = "llm"                       # scripted | llm | channel(预留)
    script: Optional[List[str]] = None      # scripted 用
    role_prompt: Optional[str] = None       # llm 用
    llm_override: Optional[Dict[str, Any]] = None  # llm 对端调试覆盖（离线测试）


class TaskCreateRequest(BaseModel):
    request_id: str = ""
    pattern_code: str
    task_info: Dict[str, Any] = {}
    counterpart: CounterpartSpec = CounterpartSpec()
    max_turns: int = 20
    timeout_s: float = 600.0
    kickoff: Optional[str] = None           # 首条喂给 pattern 的消息；缺省=task_info JSON
    session_id: Optional[str] = None
    llm_override: Optional[Dict[str, Any]] = None  # pattern 侧调试覆盖（离线测试）


class ApiEnvelope(BaseModel):
    code: str
    message: str
    status: bool
    data: Dict[str, Any] = {}


def build_tasks_router(get_engine: Callable[[], Optional[TaskEngine]]) -> APIRouter:
    router = APIRouter()

    def _require_engine() -> Optional[TaskEngine]:
        engine = get_engine()
        if engine is None:
            logger.warning("任务引擎未初始化，拒绝请求")
        return engine

    @router.post("/api/v1/tasks")
    def create_task(payload: TaskCreateRequest, request: Request) -> ApiEnvelope:
        """触发自主任务会话：起后台线程即返回（fire-and-forget，Q11）。"""
        logger.info("tasks/create: pattern=%s request_id=%s client=%s",
                    payload.pattern_code, payload.request_id,
                    request.client.host if request.client else "?")
        engine = _require_engine()
        if engine is None:
            return ApiEnvelope(code="500", status=False, message="任务引擎未初始化")

        if payload.counterpart.mode not in COUNTERPART_MODES:
            return ApiEnvelope(
                code="400", status=False,
                message=f"counterpart.mode 需为 {list(COUNTERPART_MODES)} 之一")

        max_turns = max(1, min(payload.max_turns, MAX_TURNS_CEILING))
        timeout_s = max(0.0, min(payload.timeout_s, TIMEOUT_S_CEILING))

        task_id, session_id, err = engine.start(
            pattern_code=payload.pattern_code,
            task_info=payload.task_info,
            counterpart=payload.counterpart.model_dump(),
            max_turns=max_turns,
            timeout_s=timeout_s,
            kickoff=payload.kickoff,
            session_id=payload.session_id,
            llm_override=payload.llm_override,
            request_id=payload.request_id,
        )
        if err is not None:
            # pattern 未注册=404；对端配置等参数问题=400（消息携带详情）
            return ApiEnvelope(
                code="404" if "未注册" in err else "400",
                status=False, message=err)

        return ApiEnvelope(
            code="0", status=True,
            message=f"任务已触发: task_id={task_id}",
            data={"task_id": task_id, "session_id": session_id})

    @router.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str) -> ApiEnvelope:
        """轮询任务：运行中带中间过程；终态带结果快照（Q5/Q14）。

        历史消息明细走现有 GET /api/v1/sessions/{session_id}/messages
        （data.session_id）。
        """
        engine = _require_engine()
        if engine is None:
            return ApiEnvelope(code="500", status=False, message="任务引擎未初始化")

        view = engine.get(task_id)
        if view is None:
            return ApiEnvelope(code="404", status=False,
                               message=f"task_id '{task_id}' 不存在")
        return ApiEnvelope(code="0", status=True, message="success", data=view)

    return router
