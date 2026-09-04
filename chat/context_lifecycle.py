"""
DialogueContext 字段生命周期管理 — 每轮必处理 / 跨轮保留 / 增量更新的唯一管理者。

cxt 跨轮存活于 Session 上，字段的生命周期政策此前散落在 chat() 开头
与各 handler 的转移逻辑里。本模块把政策收敛为声明式集合：改集合即改
政策，不再散落。

四类字段（见 TurnLifecycle 类属性）：
- PERSISTENT     ：跨轮绝不删除（会话状态本体）
- PER_TURN_RESET ：每轮开头重置（本轮临时产物；hop 之间绝不重置——
                   actions 里的跳转事件依赖同轮存活至 hop 循环消费）
- INCREMENTAL    ：增量更新（history 追加、filled_slots 合并），由本模块提供入口
- STAGE_MANAGED  ：由 stage 机制自管理，生命周期层不触碰
"""

import logging
from typing import Any, Dict

from dialogue.base import DialogueContext

logger = logging.getLogger(__name__)


class TurnLifecycle:
    """DialogueContext 字段生命周期的唯一管理者。

    调用时序约定（chat 层编排器负责遵守）：
    1. ``begin_turn``   — 每轮恰好一次，hop 循环之前（含同轮跳转重入 hop）
    2. ``merge_slots``  — FSM/ROUTE 转移阶段按需（增量合并 nlu 槽位）
    3. ``end_turn``     — 每轮恰好一次，回复产出之后

    集合即政策：调整某字段的归属，改下方声明即可，不动流程代码。
    """

    # -- 跨轮保留：绝不删除 --------------------------------------------------
    # 文档作用为主（begin_turn 显式不触碰它们），申明不变式：
    # 会话状态本体，误删即丢状态。
    PERSISTENT_FIELDS = (
        "history",              # 增量追加（end_turn），绝不整体清空
        "current_module_code",  # 由跳转消费（chat 层 _apply_jump）维护
        "current_node_code",    # 由跳转消费 / 节点转移维护
        "filled_slots",         # 增量合并（merge_slots）
        "task_basic_info",      # launch 时注入，全程只读
        "session_id",
        "node_map",             # launch 注入的拓扑映射
        "module_map",
    )
    PERSISTENT_METADATA_KEYS = (
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
        "actions",              # 跳转事件/动作通道每轮重建（chat 层轮末快照进
                                # ChatResult；ModuleJumpEvent 由 hop 循环消费后
                                # 移除，非跳转动作留至轮末）
    )
    PER_TURN_METADATA_KEYS = (
        "unified",
    )

    # -- stage 自管理：轮首重置 ----------------------------------------------
    # served_by_projection 由 agent 工具回执写入（借投影答轮记账）；
    # clarify 由 ClarifyStage 每轮自置——两者均不应跨轮残留，轮首清空。
    STAGE_MANAGED_METADATA_KEYS = ("clarify", "served_by_projection")

    # ------------------------------------------------------------------
    # 轮次边界
    # ------------------------------------------------------------------

    def begin_turn(self, cxt: DialogueContext, user_query: str) -> None:
        """轮首：覆写 user_query、重置每轮字段、清 stage 自管理记账。

        必须每轮恰好调用一次（hop 循环之前）；hop 之间绝不调用——
        actions 里的跳转事件需同轮存活至 hop 循环消费。

        另快照 ``turn_history_start = len(history)``（add user 之前的长度）：
        default_build_messages 以此切分跨轮历史 / 显式 query / 本轮 hop 内行。
        该标记为 begin_turn 的派生轮次标记，不入下方四类集合。
        """
        cxt.user_query = user_query
        cxt.turn_history_start = len(cxt.history)

        for field_name in self.PER_TURN_RESULT_FIELDS:
            setattr(cxt, field_name, None)
        for field_name in self.PER_TURN_LIST_FIELDS:
            setattr(cxt, field_name, [])
        for key in self.PER_TURN_METADATA_KEYS:
            cxt.metadata.pop(key, None)
        for key in self.STAGE_MANAGED_METADATA_KEYS:
            cxt.metadata.pop(key, None)

        logger.debug(
            "begin_turn: session=%s query=%r（每轮字段已重置）",
            cxt.session_id, user_query,
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
