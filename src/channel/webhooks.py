"""通用 webhook handler —— 所有渠道共用的请求处理线。

流程（渠道无法跳过任何一步）：
    token 校验 → 载荷校验（pydantic 422）→ parse → 过期过滤 →
    session 前缀拼接 → get-or-create（pattern env）→ 单轮对话 → build_reply

错误码契约（全渠道固定）：403 token / 422 载荷 / 200 过期吞掉 /
503 无默认 pattern / 500 launch 或对话异常。过期是正常业务路径，走成功
契约（空 reply = 渠道侧"不发送"），不走错误码。
"""

import logging
import os
import time
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from src.channel.base import EngineOps

logger = logging.getLogger(__name__)


def build_channel_router(spec: Any, ops: EngineOps) -> APIRouter:
    """为单个 ChannelSpec 生成 router：POST /api/v1/channel/{spec.name}。"""
    router = APIRouter()
    payload_model = spec.payload_model

    @router.post(f"/api/v1/channel/{spec.name}")
    def handle(
        payload: payload_model,  # type: ignore[valid-type]
        token: str = Query(default=""),
    ) -> Dict[str, Any]:
        # 1. 可选共享密钥：env 配置了非空值才启用校验
        if spec.token_env:
            expected = os.getenv(spec.token_env)
            if expected and token != expected:
                raise HTTPException(status_code=403, detail="channel token 校验失败")

        # 2. 渠道差异点①：载荷 → 归一化消息
        msg = spec.parse(payload)

        # 3. 过期过滤（重连重放防护）：正常业务路径，空 reply 吞掉
        session_id = f"{spec.name}:{msg.session_key}"
        if msg.timestamp is not None and time.time() - msg.timestamp > spec.stale_seconds:
            logger.info(
                "[%s] 丢弃过期消息: session=%s stale_seconds=%.0f",
                spec.name, session_id, spec.stale_seconds,
            )
            return spec.build_reply("", session_id)

        # 4. get-or-create：无会话时用默认 pattern 自动 launch
        session = ops.get_session(session_id)
        if session is None:
            pattern_code = os.getenv(spec.default_pattern_env)
            if not pattern_code:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"会话 '{session_id}' 不存在且未配置默认 pattern"
                        f"（设置环境变量 {spec.default_pattern_env} 后重试）"
                    ),
                )
            request_id = f"{spec.name}-{uuid.uuid4().hex[:12]}"
            session, _code, message = ops.launch_session(
                pattern_code, session_id, msg.task_info, request_id, exist_ok=True,
            )
            if session is None:
                raise HTTPException(status_code=500, detail=f"自动 launch 失败: {message}")
            logger.info("[%s] 自动 launch: session=%s pattern=%s",
                        spec.name, session_id, pattern_code)

        # 5. 单轮对话 + 渠道差异点②：成功响应契约
        reply, error = ops.run_chat_turn(session, msg.text)
        if error is not None:
            raise HTTPException(status_code=500, detail=f"对话处理异常: {error}")

        logger.info("[%s] 回复: session=%s reply_len=%d",
                    spec.name, session_id, len(reply or ""))
        return spec.build_reply(reply or "", session_id)

    return router


def build_channel_routers(ops: EngineOps) -> List[APIRouter]:
    """遍历注册表为每个渠道生成 router；单渠道失败 warning 跳过。"""
    from src.channel.register import registry

    routers: List[APIRouter] = []
    for name in registry.list_names():
        spec = registry.get(name)
        try:
            routers.append(build_channel_router(spec, ops))
        except Exception:
            logger.exception("生成渠道 '%s' router 失败，跳过", name)
    return routers
