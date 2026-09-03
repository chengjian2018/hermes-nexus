"""时间增强查询改写 stage —— 纯规则、零 LLM。

复用 ``src.augmentation.augment_time``（jionlp 时间解析）在原文时间
实体后追加可读时间标注，改写结果写入 ``ctx.rewritten_queries``：

    我下周一可以去 -> 我下周一(2026-09-07)可以去

与 LLM 版 ``QueryRewriter``（query.py）并列可选，通过 pattern/module/
node 的 ``query`` 槽位属性配置使用（stage_slots.py 三层延迟解析）。
不继承 ``BaseQueryRewriter``：该基类绑定 LLM 流（prompt_build /
_call_llm / 重试），纯规则改写只需 ``PipelineStage.execute``。
"""

import logging
from typing import Optional

from src.augmentation import augment_time
from src.dialogue.base import DialogueContext, PipelineStage

logger = logging.getLogger(__name__)


class TimeAugQueryRewriter(PipelineStage):
    """确定性查询改写：时间实体增强，无时间实体时原样返回。

    相对时间（今天/下周等）的基准时间戳取 ``ctx.metadata["time_base"]``
    （由 channel/调用方注入，如消息发生时刻）；未注入时用当前时间。
    """

    stage_name = "time_aug_query_rewrite"

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        time_base: Optional[float] = ctx.metadata.get("time_base")
        augmented = augment_time(ctx.user_query, time_base=time_base)

        ctx.rewritten_queries = [augmented]
        logger.info(
            "TimeAug Query Rewrite 完成: session=%s, augmented=%s",
            ctx.session_id, augmented != ctx.user_query,
        )
        return ctx
