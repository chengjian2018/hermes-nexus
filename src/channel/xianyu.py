"""闲鱼 channel 适配器 —— 对接 xianyu-auto-reply 的"默认回复 API"外挂决策口。

对方（websocket 服务的 default_reply_api.call_reply_api）把每条买家消息 POST 过来::

    {"account_id": 卖家账号, "message": 买家消息,
     "chat_id": 会话ID, "item_id": 商品ID,
     "send_user_id": 买家ID, "send_user_name": 买家昵称, "msg_time": 消息时间}

本适配器将其翻译为引擎调用：
1. 从 ``(account_id, chat_id)`` 派生稳定 session_id（同一买家同一商品跨重启不变）
2. 会话不存在时用默认 pattern 自动 launch（get-or-create；过期被治理回收则重建）
3. 走引擎单轮对话出回复，按 ``{"reply": ...}`` 契约返回

响应契约（由对方 parse_api_reply 决定，改动前先核对）：
- 非 200 状态码 → 对方返回 None，**不会发送任何内容**，错误路径一律走非 200
- 200 + ``reply`` 非空字符串 → 发送该文本；``reply`` 为空 → 不发送（用于"无需回复"）
- 200 响应体禁止携带 data/content/message 字符串键：reply 为空时对方会依次
  取这三个键，误带会把调试信息发给买家
"""

import logging
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.chat.session import Session

logger = logging.getLogger(__name__)

# msg_time 的尽力解析格式（对方未承诺格式：可能是毫秒时间戳或常见日期字符串）
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S")


class XianyuInboundMessage(BaseModel):
    """xianyu-auto-reply 默认回复 API 的入站消息载荷。"""

    account_id: str  # 闲鱼卖家账号标识
    message: str  # 买家消息原文
    chat_id: str  # 闲鱼会话 ID（买家 × 商品维度）
    item_id: Optional[str] = None
    send_user_id: Optional[str] = None
    send_user_name: Optional[str] = None
    msg_time: Optional[str] = None


def _parse_msg_time(msg_time: str) -> Optional[float]:
    """尽力把 msg_time 解析为 epoch 秒；无法识别返回 None（不做过期过滤）。"""
    text = msg_time.strip()
    if not text:
        return None
    try:
        value = float(text)
        return value / 1000.0 if value > 1e12 else value
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _build_task_info(msg: XianyuInboundMessage) -> Dict[str, str]:
    """从入站消息提取 task_info（launch 时注入 cxt.metadata，供 prompt 组装用）。"""
    info = {"channel": "xianyu", "account_id": msg.account_id}
    if msg.item_id is not None:
        info["item_id"] = msg.item_id
    if msg.send_user_id is not None:
        info["buyer_user_id"] = msg.send_user_id
    if msg.send_user_name is not None:
        info["buyer_user_name"] = msg.send_user_name
    return info


def build_xianyu_router(
    *,
    get_session: Callable[[str], Optional[Session]],
    launch_session: Callable[..., Tuple[Optional[Session], str, str]],
    run_chat_turn: Callable[[Session, str], Tuple[Optional[str], Optional[Exception]]],
    pattern_code_lookup: Callable[[], Optional[str]],
    token_lookup: Optional[Callable[[], Optional[str]]] = None,
    stale_seconds: float = 300.0,
) -> APIRouter:
    """构建闲鱼 channel 的 APIRouter。

    引擎操作全部由 main.py 注入（不引全局单例，channel 模块可离线单测）：
    - get_session: 治理感知的会话查询（含过期清理与活跃续期）
    - launch_session: (pattern_code, session_id, task_info, request_id, exist_ok)
      -> (session|None, code, message)；exist_ok=True 时已存在视为成功并返回既有会话
    - run_chat_turn: (session, query) -> (reply|None, error|None)，含轮末审计落盘
    - pattern_code_lookup / token_lookup: 每次请求时读取（环境变量可热改）
    """

    router = APIRouter()

    @router.post("/api/v1/channel/xianyu")
    def xianyu_reply(
        msg: XianyuInboundMessage,
        token: str = Query(default=""),
    ) -> Dict[str, Any]:
        # 可选共享密钥（api_url 上拼 ?token=... 即可携带）
        if token_lookup is not None:
            expected = token_lookup()
            if expected and token != expected:
                raise HTTPException(status_code=403, detail="channel token 校验失败")

        # 过期消息直接吞掉（重连重放防护）：空 reply 契约上等于"不发送"
        if msg.msg_time is not None:
            msg_ts = _parse_msg_time(msg.msg_time)
            if msg_ts is not None and time.time() - msg_ts > stale_seconds:
                logger.info(
                    "闲鱼 channel 丢弃过期消息: account=%s chat=%s msg_time=%s",
                    msg.account_id,
                    msg.chat_id,
                    msg.msg_time,
                )
                return {"reply": "", "session_id": ""}

        session_id = f"xianyu:{msg.account_id}:{msg.chat_id}"
        request_id = f"xianyu-{uuid.uuid4().hex[:12]}"

        session = get_session(session_id)
        if session is None:
            pattern_code = pattern_code_lookup()
            if not pattern_code:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"会话 '{session_id}' 不存在且未配置默认 pattern"
                        "（设置环境变量 XIANYU_CHANNEL_PATTERN 后重试）"
                    ),
                )
            session, _code, message = launch_session(
                pattern_code,
                session_id,
                _build_task_info(msg),
                request_id,
                exist_ok=True,
            )
            if session is None:
                raise HTTPException(status_code=500, detail=f"自动 launch 失败: {message}")
            logger.info("闲鱼 channel 自动 launch: session=%s pattern=%s", session_id, pattern_code)

        reply, error = run_chat_turn(session, msg.message)
        if error is not None:
            raise HTTPException(status_code=500, detail=f"对话处理异常: {error}")

        logger.info(
            "闲鱼 channel 回复: session=%s account=%s chat=%s reply_len=%d",
            session_id,
            msg.account_id,
            msg.chat_id,
            len(reply or ""),
        )
        return {"reply": reply or "", "session_id": session_id}

    return router
