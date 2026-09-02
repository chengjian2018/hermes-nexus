"""闲鱼 channel —— 对接 xianyu-auto-reply 的"默认回复 API"外挂决策口。

声明式渠道（ChannelSpec）：载荷 schema、session 派生、task_info 映射、
成功响应契约；共性流程（token/过期/get-or-create/错误码）在 webhooks.py。

响应契约（由对方 parse_api_reply 决定，改动前先核对）：
- 非 200 状态码 → 对方返回 None，**不会发送任何内容**，错误路径一律走非 200
- 200 + ``reply`` 非空字符串 → 发送该文本；``reply`` 为空 → 不发送（"无需回复"）
- 200 响应体禁止携带 data/content/message 字符串键：reply 为空时对方会依次
  取这三个键，误带会把调试信息发给买家
"""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel

from src.channel.base import InboundMessage
from src.channel.register import registry

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


class XianyuChannel:
    """闲鱼渠道声明：session_key 为裸 ``account_id:chat_id``（前缀由
    handler 拼，session_id 与历史 ``xianyu:{account_id}:{chat_id}`` 字节一致）。"""

    name = "xianyu"
    payload_model = XianyuInboundMessage
    default_pattern_env = "XIANYU_CHANNEL_PATTERN"
    token_env = "XIANYU_CHANNEL_TOKEN"
    stale_seconds = 300.0

    def parse(self, payload: XianyuInboundMessage) -> InboundMessage:
        """载荷 → 归一化入站消息（task_info 映射 + msg_time 尽力解析）。"""
        task_info: Dict[str, str] = {"channel": "xianyu", "account_id": payload.account_id}
        if payload.item_id is not None:
            task_info["item_id"] = payload.item_id
        if payload.send_user_id is not None:
            task_info["buyer_user_id"] = payload.send_user_id
        if payload.send_user_name is not None:
            task_info["buyer_user_name"] = payload.send_user_name
        timestamp = _parse_msg_time(payload.msg_time) if payload.msg_time else None
        return InboundMessage(
            channel=self.name,
            text=payload.message,
            session_key=f"{payload.account_id}:{payload.chat_id}",
            timestamp=timestamp,
            task_info=task_info,
        )

    def build_reply(self, reply: str, session_id: str) -> Dict[str, Any]:
        """成功响应只含 reply/session_id 两键（契约见模块 docstring）。"""
        return {"reply": reply, "session_id": session_id}


registry.register(XianyuChannel())
