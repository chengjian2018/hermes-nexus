"""Module dispatch —— 全 pattern 通用的唯一模块转移原语（spec §3）。

三种跳转源（agent transfer 工具 / ROUTE jump_module 菜单分发）
都构造 ModuleDispatch 走本函数；它只做纯状态转移 + 记账，
不决定轮次边界（same-turn 重入由 chat() 层消费 dispatch 事件实现）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.dialogue.base import DialogueContext

logger = logging.getLogger(__name__)


@dataclass
class ModuleDispatch:
    """一次模块转移事件。"""

    target_module_code: str
    reason: str = ""      # 移交上下文：供目标模块承接（注入 prompt）
    source: str = ""      # handoff_tool / route_menu / jump_tag


def dispatch(ctx: DialogueContext, event: ModuleDispatch) -> bool:
    """执行模块转移：邻接校验 → 状态转移 → 记账。

    Returns:
        True 转移成功；False 目标非法（不邻接/回弹/无图），状态不变。
    """
    graph = ctx.metadata.get("dispatch_graph") or {}
    edges = graph.get(ctx.current_module_code or "", set())
    target = event.target_module_code

    if target not in edges:
        logger.warning(
            "[dispatch] 目标 '%s' 不在 '%s' 的邻接表中，拒绝转移",
            target, ctx.current_module_code,
        )
        return False

    # 防环：同轮 A→B→A 回弹拒绝（dispatch_log 为本轮累积，chat() 每轮开头清空；
    # 跨轮 dispatch_log 已清，返回原模块合法——sticky 逃生语义 spec §5）
    log = ctx.metadata.setdefault("dispatch_log", [])
    if log:
        first_from = log[0].get("from")
        if target == first_from:
            logger.warning("[dispatch] 同轮回弹 %s → %s，拒绝（移交环）",
                           ctx.current_module_code, target)
            return False

    log.append({
        "from": ctx.current_module_code,
        "to": target,
        "source": event.source,
        "reason": event.reason,
    })
    # 离开借答方时清除回看记账（回看块仅在借方自身轮次有效，防跨模块泄漏）
    served = ctx.metadata.get("served_by_projection")
    if isinstance(served, dict) and served.get("module") != target:
        ctx.metadata.pop("served_by_projection", None)
    ctx.metadata["handoff_context"] = {
        "from": ctx.current_module_code,
        "reason": event.reason,
    }
    ctx.current_module_code = target
    ctx.current_node_code = None
    logger.info("[dispatch] %s → %s (source=%s)",
                log[-1]["from"], target, event.source)
    return True
