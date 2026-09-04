"""
轮次产出封装 — 回复不只是文本：text + actions。

chat() 兼容入口继续返回 str（.text）；chat_turn() 返回本模块的 ChatResult，
为 API 层后续消费 actions（发送卡片 / 转人工 / 外呼等回复之外的动作）预留通道。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from dialogue.base import DialogueContext, ModuleJumpEvent


@dataclass
class ChatResult:
    """一轮对话的完整产出。

    - text    ：出口回复文本（兼容 chat() 的 str 返回）
    - actions ：动作通道（轮末从 cxt.actions 快照；ModuleJumpEvent 已被
                hop 循环消费，此处仅剩 dict 形态动作如 conversation_end，
                以及因超跳数未消费的残留跳转事件——均转 dict 观测形态）
    """

    text: str
    actions: List[Dict[str, Any]] = field(default_factory=list)


def _snapshot_action(item: Any) -> Dict[str, Any]:
    """动作条目转观测 dict：ModuleJumpEvent 走 to_dict，dict 原样。"""
    if isinstance(item, ModuleJumpEvent):
        return item.to_dict()
    return item


def build_chat_result(text: str, cxt: DialogueContext) -> ChatResult:
    """轮末从 cxt 构建 ChatResult：快照 actions。"""
    return ChatResult(
        text=text,
        actions=[_snapshot_action(item) for item in (cxt.actions or [])],
    )
