"""
DialogueContext 字段生命周期管理 — 每轮必处理 / 跨轮保留 / 增量更新的唯一管理者。

cxt 跨轮存活于 Session 上，字段的生命周期政策此前散落在 chat() 开头
（pop dispatch_log / handoff_context）与各 handler 的转移逻辑里。本模块把
政策收敛为声明式集合：改集合即改政策，不再散落。

四类字段（见 TurnLifecycle 类属性）：
- PERSISTENT     ：跨轮绝不删除（会话状态本体）
- PER_TURN_RESET ：每轮开头重置（本轮临时产物；hop 之间绝不重置——
                   dispatch_log 依赖同轮累积做回弹拒绝）
- INCREMENTAL    ：增量更新（history 追加、filled_slots 合并），由本模块提供入口
- STAGE_MANAGED  ：由 stage / dispatch 机制自管理，生命周期层不触碰
"""

import logging
from typing import Any, Dict

from src.dialogue.base import DialogueContext

logger = logging.getLogger(__name__)


class TurnLifecycle:
    """DialogueContext 字段生命周期的唯一管理者。

    调用时序约定（chat 层编排器负责遵守）：
    1. ``begin_turn``   — 每轮恰好一次，hop 循环之前（含同轮 dispatch 重入 hop）
    2. ``bind_pattern`` — 每轮一次，pattern 校验通过后
    3. ``merge_slots``  — FSM/ROUTE 转移阶段按需（增量合并 nlu 槽位）
    4. ``end_turn``     — 每轮恰好一次，回复产出之后

    集合即政策：调整某字段的归属，改下方声明即可，不动流程代码。
    """

    # -- 跨轮保留：绝不删除 --------------------------------------------------
    # 文档作用为主（begin_turn 显式不触碰它们），申明不变式：
    # 会话状态本体，误删即丢状态。
    PERSISTENT_FIELDS = (
        "history",              # 增量追加（end_turn），绝不整体清空
        "current_module_code",  # 由 dispatch / handler 维护
        "current_node_code",    # 由 dispatch / handler 维护
        "filled_slots",         # 增量合并（merge_slots）
        "task_basic_info",      # launch 时注入，全程只读
        "session_id",
        "node_map",             # launch 注入的拓扑映射
        "module_map",
    )
    PERSISTENT_METADATA_KEYS = (
        "dispatch_graph",       # bind_pattern setdefault 注入
        "bargain_settings",
        "task_info",
        "llm_override",
        "pattern_code",
    )

    # -- 每轮重置：轮首归零 --------------------------------------------------
    # 本轮临时产物。现状部分字段靠 stage 覆写"恰好不残留"，这里显式归零，
    # 语义从"残留但通常被覆盖"收紧为"每轮干净"。
    PER_TURN_RESULT_FIELDS = ("nlu_result", "nlg_result", "agent_result")
    PER_TURN_LIST_FIELDS = (
        "pre_recall_results",
        "rewritten_queries",
        "post_recall_results",
        "actions",              # 动作通道每轮重建（chat 层轮末快照进 ChatResult）
    )
    PER_TURN_METADATA_KEYS = (
        "dispatch_log",         # 同轮累积做回弹拒绝 → 只在轮首清，hop 间不清
        "handoff_context",      # 承接块为"首轮条件注入"：dispatch 同轮发生、B 当轮
                                # 消费、下一轮开头清除 = 恰好只注入接手首轮
        "unified",
    )

    # -- stage / dispatch 自管理：不触碰 --------------------------------------
    # clarify 由 ClarifyStage 每轮自置自清；served_by_projection 由 dispatch() 维护。
    STAGE_MANAGED_METADATA_KEYS = ("clarify", "served_by_projection")

    # ------------------------------------------------------------------
    # 轮次边界
    # ------------------------------------------------------------------

    def begin_turn(self, cxt: DialogueContext, user_query: str) -> None:
        """轮首：覆写 user_query、重置每轮字段、清 dispatch 记账。

        必须每轮恰好调用一次（hop 循环之前）；hop 之间绝不调用——
        dispatch_log 的同轮累积是回弹拒绝的依据。
        """
        cxt.user_query = user_query

        for field_name in self.PER_TURN_RESULT_FIELDS:
            setattr(cxt, field_name, None)
        for field_name in self.PER_TURN_LIST_FIELDS:
            setattr(cxt, field_name, [])
        for key in self.PER_TURN_METADATA_KEYS:
            cxt.metadata.pop(key, None)

        logger.debug(
            "begin_turn: session=%s query=%r（每轮字段已重置）",
            cxt.session_id, user_query,
        )

    def bind_pattern(self, cxt: DialogueContext, pattern) -> None:
        """注入 dispatch_graph（setdefault：launch / store 恢复路径均安全）。

        无图时 dispatch 全拒绝、ROUTE 静默分发静默失效——与现状一致。
        """
        cxt.metadata.setdefault(
            "dispatch_graph", getattr(pattern, "dispatch_graph", {}) or {}
        )

    def end_turn(self, cxt: DialogueContext, response_text: str) -> None:
        """轮末：history 追加 assistant 消息（增量更新入口）。"""
        cxt.add_message("assistant", response_text, stage="chat")

    # ------------------------------------------------------------------
    # 增量更新
    # ------------------------------------------------------------------

    def merge_slots(self, cxt: DialogueContext, slots: Dict[str, Any]) -> None:
        """增量合并：nlu slots 并入 filled_slots（后写覆盖同键）。

        FSM/ROUTE 转移阶段共用，收敛原先 chat 层两份重复拷贝。
        """
        if slots:
            cxt.filled_slots.update(slots)
            logger.info("槽位更新: %s", slots)
